"""Příkazová řádka: `jobagent run [--dry-run]`, `test-email [--nahled]`, `sample <url>`, `vysvetli <url>`.

Vrací návratový kód (0 = OK, 1 = chyba, 2 = chyba v nastavení, 3 = e-mail se
nepodařilo odeslat). Proč na tom
záleží: Plánovač úloh Windows ho ukazuje ve sloupci „Výsledek posledního
spuštění“, takže selhání je vidět i bez otevírání logu.

Každá neošetřená výjimka skončí v logu i s tracebackem. Z Plánovače by
jinak zmizela beze stopy.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from jobagent import __version__, collectors, emailer, explain, mailer, paths, pipeline
from jobagent.config import ConfigError, load_config
from jobagent.fetch import FetchError, Fetcher
from jobagent.log import setup_logging
from jobagent.storage import Storage

log = logging.getLogger("jobagent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobagent", description="Denní přehled nabídek práce z inzertních portálů.")
    parser.add_argument("--version", action="version", version=f"jobagent {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="podrobný výpis do konzole")
    sub = parser.add_subparsers(dest="command", required=True, metavar="PŘÍKAZ")

    run = sub.add_parser("run", help="celý běh: sběr → filtry → hodnocení → e-mail")
    run.add_argument("--dry-run", action="store_true", help="nic neposílat, uložit e-mail jako HTML a otevřít ho")
    run.add_argument("--no-open", action="store_true", help="u --dry-run neotvírat prohlížeč")

    test = sub.add_parser(
        "test-email",
        help="poslat zkušební e-mail a ověřit nastavení [email] (nic nesbírá, nic neoznačuje)",
    )
    test.add_argument("--nahled", action="store_true",
                      help="místo krátké zprávy poslat poslední náhled z vystup/ (jak bude vypadat v poště)")

    sample = sub.add_parser(
        "sample",
        help="uložit HTML stránky do samples/private/ (podklad pro parser a testy, není v gitu)",
    )
    sample.add_argument("url")
    sample.add_argument("--name", help="název souboru bez přípony (jinak se odvodí z URL)")

    vysvetli = sub.add_parser(
        "vysvetli",
        help="ukázat, odkud se vzal text inzerátu a za co dostal body (ladění configu)",
        description="Zdroj textu, odstraněné řádky, text, filtry, nalezená slova s místem a body, výsledné skóre.",
    )
    vysvetli.add_argument("cil", metavar="URL_NEBO_SOUBOR",
                          help="adresa detailu inzerátu, nebo HTML uložené příkazem sample")
    vysvetli.add_argument("--url", help="u souboru: adresa inzerátu (jinak se vezme z hlavičky souboru)")
    vysvetli.add_argument("--cely-text", action="store_true", help="vypsat celý text, ne jen začátek")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = setup_logging(paths.log_dir(), verbose=args.verbose)
    log.info("jobagent %s, příkaz %s, složka %s", __version__, args.command, paths.app_dir())
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "test-email":
            return cmd_test_email(args)
        if args.command == "sample":
            return cmd_sample(args)
        if args.command == "vysvetli":
            return cmd_explain(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Přerušeno uživatelem.")
        return 1
    except Exception:
        log.exception("Neočekávaná chyba – podrobnosti jsou v %s", log_path)
        return 1
    return 0


def _make_fetcher(cfg) -> Fetcher:
    return Fetcher(delay_min=cfg.general.delay_min, delay_max=cfg.general.delay_max, timeout=cfg.general.timeout)


# Návratové kódy (Plánovač úloh je ukazuje ve sloupci „Výsledek posledního spuštění“).
EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_MAIL = 0, 1, 2, 3


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(paths.config_file(), paths.secrets_file())
    for warning in cfg.warnings:
        log.warning("%s", warning)
    if not args.dry_run:
        # Chyby v [email] odhalit hned, ne až po deseti minutách stahování.
        try:
            mailer.check_settings(cfg.email, cfg.secrets.email_password)
        except mailer.MailError as exc:
            log.error("%s Ověření nastavení: jobagent test-email", exc)
            return EXIT_CONFIG

    threshold = cfg.summary.score_threshold
    storage = Storage(paths.db_file())
    try:
        report = pipeline.run(
            cfg, storage, _make_fetcher(cfg), dry_run=args.dry_run, unknown_samples_dir=paths.unknown_samples_dir()
        )
        now = datetime.now()
        html_body, text_body, attachment = emailer.build_bodies(report, paths.templates_dir(), now, threshold)
        # Celý souhrn se uloží vždy: u --dry-run je to náhled, u ostrého běhu
        # záznam toho, co odešlo (a podklad pro `test-email --nahled`).
        preview = emailer.save_preview(attachment or html_body, paths.output_dir(), now)
        log.info("Souhrn uložen: %s", preview)
        for warning in report.warnings:
            log.warning("%s", warning)

        if args.dry_run:
            if not args.no_open:
                emailer.open_in_browser(preview)
            mail_failed = False
        else:
            mail_failed = not _deliver(cfg, storage, report, now, html_body, text_body, attachment, threshold)
    finally:
        storage.close()

    if mail_failed:
        return EXIT_MAIL  # má přednost: neodeslaný e-mail je to, co uživatel musí řešit
    # Selhala nebo se přeskočila všechna hledání (výpadek sítě, blokace)?
    # Návratový kód 1, aby to bylo vidět v Plánovači úloh, i když e-mail
    # s upozorněním vznikl.
    failed = report.errors + report.skipped
    return EXIT_ERROR if report.stats and failed == len(report.stats) else EXIT_OK


def _deliver(cfg, storage: Storage, report: pipeline.RunReport, now: datetime,
             html_body: str, text_body: str, attachment: str | None, threshold: int | None) -> bool:
    """Odeslat souhrn; jen při úspěchu označit nabídky jako odeslané. True = odesláno."""
    msg = mailer.build_message(
        cfg.email, emailer.subject(report, cfg.email.subject, threshold), text_body, html_body,
        attachment_html=attachment, attachment_name=emailer.ATTACHMENT_NAME, now=now,
    )
    size_kb = len(msg.as_bytes()) / 1024
    log.info("Odesílám souhrn (%.0f kB%s) na %s.", size_kb, ", celý v příloze" if attachment else "", msg["To"])
    try:
        mailer.send(msg, cfg.email, cfg.secrets.email_password)
    except mailer.MailError as exc:
        html_path, eml_path = mailer.save_failed(msg, attachment or html_body, paths.output_dir(), now)
        log.error("E-mail se nepodařilo odeslat: %s", exc)
        if exc.technical:
            log.error("Technicky: %s", exc.technical)
        log.error("Nic se neoznačilo jako odeslané, příští běh pošle tytéž nabídky znovu. "
                  "Souhrn je uložený v %s (a k otevření v poštovním programu %s). "
                  "Nastavení ověříš příkazem: jobagent test-email", html_path, eml_path.name)
        return False
    # Pořadí „odeslat → označit“ je záměrné: pád mezi tím = duplicita, ne ztráta.
    storage.mark_sent(report.shown_jobs, report.rejected, now, run_id=report.run_id)
    log.info("Odesláno, %d nabídek označeno jako odeslané.", len(report.shown_jobs))
    return True


def cmd_test_email(args: argparse.Namespace) -> int:
    cfg = load_config(paths.config_file(), paths.secrets_file())
    for warning in cfg.warnings:
        log.warning("%s", warning)
    e = cfg.email
    # Heslo se nikdy nevypisuje, ani jeho délka: stačí vědět, jestli je vyplněné.
    log.info("Server: %s:%s (%s), uživatel: %s, odesílatel: %s, příjemci: %s, heslo: %s",
             e.smtp_server, e.port, e.security, e.user or "—", e.sender or e.user or "—",
             ", ".join(e.recipients) or "—", "vyplněné" if cfg.secrets.email_password else "CHYBÍ")
    now = datetime.now()

    if args.nahled:
        previews = sorted(paths.output_dir().glob("email_*.html"), key=lambda path: path.stat().st_mtime)
        if not previews:
            log.error("Ve složce %s není žádný náhled. Nejdřív spusť: jobagent run --dry-run --no-open",
                      paths.output_dir())
            return EXIT_ERROR
        source = previews[-1]
        html_body = source.read_text(encoding="utf-8")
        size = len(html_body.encode("utf-8"))
        log.info("Posílám náhled %s (%.0f kB).", source.name, size / 1024)
        if size > emailer.BODY_LIMIT_BYTES:
            # Záměrně se nezkracuje: test má ukázat, jak pošta zachází s velkým e-mailem.
            log.warning("Náhled má přes %d kB. Gmail takové tělo usekne („Zpráva byla oříznuta“); ostrý běh "
                        "by v těle nechal jen prvních %d ostatních a celý souhrn dal do přílohy.",
                        emailer.BODY_LIMIT_BYTES // 1000, emailer.BODY_OTHER_ROWS)
        subject = f"{e.subject}: test – náhled {source.stem.removeprefix('email_')}"
        text_body = (f"Zkušební odeslání náhledu {source.name} ({size / 1024:.0f} kB).\n"
                     "Souhrn je v HTML části zprávy. Nabídky se neoznačily jako odeslané.\n")
    else:
        subject = f"{e.subject}: zkušební e-mail"
        lines = [
            "Tohle je zkušební e-mail z Job Agenta. Když ho čteš, odesílání funguje.",
            "",
            f"Odesláno: {now:%d. %m. %Y %H:%M}",
            f"Server: {e.smtp_server}:{e.port} ({e.security})",
            f"Uživatel: {e.user}",
            f"Příjemci: {', '.join(e.recipients)}",
            "",
            "Když zpráva skončila ve spamu, označ ji jako „není spam“.",
        ]
        text_body = "\n".join(lines) + "\n"
        html_body = ("<!DOCTYPE html><html lang=\"cs\"><meta charset=\"utf-8\"><body style=\"font-family:sans-serif\">"
                     + "".join(f"<p>{escape(line)}</p>" if line else "" for line in lines)
                     + "</body></html>")

    msg = mailer.build_message(e, subject, text_body, html_body, now=now)
    try:
        # Bez opakování: u testu chceš chybu hned, ne za minutu.
        mailer.send(msg, e, cfg.secrets.email_password, retries=0)
    except mailer.MailError as exc:
        log.error("Odeslání selhalo: %s", exc)
        if exc.technical:
            log.error("Technicky: %s", exc.technical)
        return EXIT_MAIL
    log.info("Hotovo. Zkontroluj schránku %s (případně složku spam).", ", ".join(e.recipients))
    return EXIT_OK


def cmd_sample(args: argparse.Namespace) -> int:
    # Sample nepotřebuje platný config. Funguje i před prvním nastavením,
    # takže když config chybí nebo má chyby, použijí se výchozí prodlevy.
    try:
        cfg = load_config(paths.config_file())
        fetcher = _make_fetcher(cfg)
    except ConfigError:
        fetcher = Fetcher()
    try:
        response = fetcher.get(args.url)
    except FetchError as exc:
        log.error("%s", exc)
        return 1
    name = args.name or _sample_name(args.url)
    folder = paths.private_samples_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.html"
    # Na první řádek zdroj a datum: za měsíc nikdo neví, odkud soubor je.
    header = f"<!-- Zdroj: {args.url} | konečná URL: {response.url} | uloženo {datetime.now():%Y-%m-%d %H:%M} -->\n"
    path.write_text(header + response.text, encoding="utf-8")
    log.info("Uloženo: %s (%d znaků)", path, len(response.text))
    log.info("Skutečná stránka, do gitu nepatří. Pro testy z ní udělej anonymizovanou kopii: "
             "tools/anonymize_samples.py (viz samples/README.md).")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    cfg = load_config(paths.config_file())
    target = args.cil
    if Path(target).is_file():
        html = Path(target).read_text(encoding="utf-8", errors="replace")
        url = args.url or _url_from_sample(html)
        if not url:
            log.error("Soubor nemá v hlavičce „Zdroj: …“, zadej adresu inzerátu přes --url.")
            return 2
    else:
        url = target
        collector = collectors.for_url(url)
        try:
            fetcher = _make_fetcher(cfg)
            html = (collector.fetch_page(fetcher, url) if collector else fetcher.get(url)).text
        except FetchError as exc:
            log.error("%s", exc)
            return 1

    collector = collectors.for_url(url)
    listing = None
    if paths.db_file().exists():
        storage = Storage(paths.db_file())
        try:
            listing = storage.find_listing(
                collector.name if collector else None,
                collector.native_id_from_url(url) if collector else None,
                collector.canonical_url(url) if collector else url,
            )
        finally:
            storage.close()

    text = explain.explain(html, url, cfg, datetime.now().date(), listing, full_text=args.cely_text)
    # Konzole na Windows nemusí umět všechny znaky (emoji v názvech inzerátů);
    # radši otazník než pád programu uprostřed výpisu.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print(text)
    return 0


def _url_from_sample(html: str) -> str | None:
    m = re.search(r"<!--\s*Zdroj:\s*(\S+)", html[:2000])
    return m.group(1) if m else None


def _sample_name(url: str) -> str:
    parts = urlsplit(url)
    raw = f"{parts.hostname or 'stranka'}{parts.path}"
    slug = re.sub(r"[^\w]+", "_", raw).strip("_")[:70]
    return f"{slug}_{datetime.now():%Y%m%d_%H%M%S}"


if __name__ == "__main__":
    sys.exit(main())
