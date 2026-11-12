"""Příkazová řádka: `jobagent run [--dry-run]`, `test-email [--nahled]`, `sablony`, `sample <url>`, `vysvetli <url>`.

Bez parametrů (typicky dvojklik na JobAgent.exe) ukáže krátkou nápovědu
a při prvním spuštění vytvoří config.toml, viz firstrun.py. Když program
běží ve vlastním okně (dvojklik), před koncem počká na Enter, viz console.py.

Vrací návratový kód (0 = OK, 1 = chyba, 2 = chyba v nastavení, 3 = e-mail se
nepodařilo odeslat). Proč na tom
záleží: Plánovač úloh Windows ho ukazuje ve sloupci „Výsledek posledního
spuštění“, takže selhání je vidět i bez otevírání logu.

Každá neošetřená výjimka skončí v logu i s tracebackem. Z Plánovače by
jinak zmizela beze stopy.
"""

from __future__ import annotations

import argparse
import email
import getpass
import logging
import re
import sys
from datetime import datetime
from email import policy
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from jobagent import (
    __version__, collectors, console, credentials, emailer, explain, firstrun, mailer, outputs, paths, pipeline,
    secrets_file,
)
from jobagent.config import ConfigError, load_config
from jobagent.fetch import FetchError, Fetcher
from jobagent.log import setup_logging
from jobagent.storage import Storage
from jobagent.templating import Templates, export_defaults

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
                      help="místo krátké zprávy znovu poslat poslední uloženou zprávu z vystup/ (předmět „TEST – …“)")

    password = sub.add_parser(
        "nastav-heslo",
        help="uložit heslo k e-mailu do Správce pověření Windows (ověří přihlášení, nic neodešle)",
    )
    password.add_argument("--bez-overeni", action="store_true",
                          help="uložit bez zkušebního přihlášení (např. bez internetu)")
    sub.add_parser("smaz-heslo", help="smazat heslo k e-mailu ze Správce pověření Windows")

    sub.add_parser(
        "sablony",
        help="vytvořit složku templates/ s šablonami e-mailu k úpravě (existující soubory nepřepíše)",
    )

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


# Příkazy, které bez config.toml nemají smysl: u nich první spuštění vytvoří config.
NEEDS_CONFIG = {"run", "test-email", "nastav-heslo", "smaz-heslo"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    own_window = console.owns_console()
    try:
        return _main(argv)
    finally:
        # I po --help nebo chybě v parametrech (argparse volá sys.exit):
        # finally proběhne i při SystemExit, takže okno nezmizí dřív, než se dá číst.
        console.pause_if_own_window(own_window)


def _main(argv: list[str]) -> int:
    if not argv:
        setup_logging(paths.log_dir())
        print(_start_help())
        return _first_run_if_needed() or EXIT_OK

    args = build_parser().parse_args(argv)
    log_path = setup_logging(paths.log_dir(), verbose=args.verbose)
    log.info("jobagent %s, příkaz %s, složka %s", __version__, args.command, paths.app_dir())
    try:
        if args.command in NEEDS_CONFIG and (code := _first_run_if_needed()):
            return code
        if args.command == "run":
            return cmd_run(args)
        if args.command == "test-email":
            return cmd_test_email(args)
        if args.command == "nastav-heslo":
            return cmd_set_password(args)
        if args.command == "smaz-heslo":
            return cmd_delete_password(args)
        if args.command == "sablony":
            return cmd_templates(args)
        if args.command == "sample":
            return cmd_sample(args)
        if args.command == "vysvetli":
            return cmd_explain(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        log.warning("Přerušeno uživatelem.")
        return EXIT_ERROR
    except Exception:
        log.exception("Neočekávaná chyba – podrobnosti jsou v %s", log_path)
        return EXIT_ERROR
    return EXIT_OK


def _cmd() -> str:
    return firstrun.command_name(getattr(sys, "frozen", False))


def _start_help() -> str:
    cmd = _cmd()
    return f"""Job Agent {__version__} – denní přehled nabídek práce e-mailem.

Příkazy (příkazový řádek v této složce otevřeš tak, že do adresního řádku
Průzkumníka napíšeš  cmd  a stiskneš Enter):
  {cmd} run --dry-run    ukázat, co by přišlo e-mailem (nic neodešle)
  {cmd} run              stáhnout nabídky a poslat souhrn
  {cmd} nastav-heslo     uložit heslo k e-mailu (bezpečně, do Správce pověření)
  {cmd} test-email       poslat zkušební e-mail
  {cmd} sablony          vytvořit složku templates s šablonami e-mailu k úpravě
  {cmd} --help           všechny příkazy

Návod krok za krokem je v souboru NAVOD.txt."""


def _first_run_if_needed() -> int | None:
    """Chybí config.toml → vytvořit ze vzoru, vysvětlit, otevřít k úpravě. Vrací 2, nebo None (config je)."""
    config_path = paths.config_file()
    if not firstrun.ensure_config(config_path, paths.example_config()):
        return None
    log.info("První spuštění: vytvořen %s ze vzoru.", config_path)
    # print, ne log: uvítání je pro člověka u obrazovky, do logu stačí řádek výše.
    # V exe bez okna (Plánovač) print nic nedělá (sys.stdout je None).
    print(firstrun.welcome_text(config_path, _cmd()))
    if console.can_ask() and console.open_for_edit(config_path):
        print("Soubor config.toml jsem otevřel v Poznámkovém bloku. Po úpravě ho ulož (Ctrl+S).")
    return EXIT_CONFIG


def _load_config_with_password():
    """config + secrets.toml, heslo k e-mailu přednostně ze Správce pověření Windows."""
    cfg = load_config(paths.config_file(), paths.secrets_file())
    e = cfg.email
    try:
        stored = credentials.stored_password(credentials.default_store(), e.user, e.smtp_server)
    except credentials.CredentialError as exc:
        cfg.warnings.append(f"{exc} Použiju heslo ze secrets.toml, pokud tam je.")
        return cfg
    if stored is None:
        return cfg
    if cfg.secrets.email_password:
        cfg.warnings.append(
            "Heslo k e-mailu je ve Správci pověření i v secrets.toml. Používám Správce pověření; ze "
            f"secrets.toml ho odstraň (znovu spusť {_cmd()} nastav-heslo, nabídne to, nebo vyprázdni řádek heslo)."
        )
    cfg.secrets.email_password = stored
    cfg.secrets.email_password_source = "Správce pověření Windows"
    return cfg


def _ask_yes(question: str, default: bool) -> bool:
    hint = "[A/n]" if default else "[a/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer[0] in ("a", "y")  # ano / yes


def _password_cfg():
    """Společná příprava pro nastav-heslo / smaz-heslo. Vrací (cfg, store) nebo kód chyby."""
    if not credentials.available():
        log.error("Správce pověření je jen ve Windows. Tady dej heslo do secrets.toml ([email] heslo = \"…\").")
        return EXIT_CONFIG
    cfg = load_config(paths.config_file(), paths.secrets_file())
    if not cfg.email.user or not cfg.email.smtp_server:
        log.error("V config.toml v sekci [email] nejdřív vyplň uzivatel (celou adresu) a smtp_server. "
                  "Heslo se ukládá k téhle adrese a serveru.")
        return EXIT_CONFIG
    return cfg, credentials.default_store()


def cmd_set_password(args: argparse.Namespace) -> int:
    prepared = _password_cfg()
    if isinstance(prepared, int):
        return prepared
    cfg, store = prepared
    e = cfg.email
    if not console.can_ask():
        log.error("nastav-heslo potřebuje okno, kde jde heslo napsat. Spusť z příkazového řádku: %s nastav-heslo", _cmd())
        return EXIT_ERROR

    print(f"\nHeslo k e-mailu {e.user} (server {e.smtp_server}).")
    print("U Seznamu se zapnutým dvoufázovým ověřením a u Gmailu zadej HESLO PRO APLIKACE, ne heslo k účtu.")
    print("Heslo se při psaní ani vložení (Ctrl+V nebo pravé tlačítko myši) nezobrazuje – to je v pořádku.")
    # strip: při vložení se často přibalí konec řádku. Mezery uvnitř zůstávají (Gmail je ukazuje po čtveřicích).
    password = getpass.getpass("Heslo: ").strip()
    if not password:
        log.warning("Nic nezadáno, nic se neuložilo.")
        return EXIT_ERROR

    if not args.bez_overeni:
        try:
            mailer.verify_login(e, password)
        except mailer.MailError as exc:
            log.error("Přihlášení se nepovedlo: %s", exc)
            if exc.technical:
                log.error("Technicky: %s", exc.technical)
            # Špatné heslo neukládat. Výpadek sítě ale o heslu nic neříká – nechat na uživateli.
            if not (exc.transient and _ask_yes("Server teď není dostupný. Uložit heslo i bez ověření?", default=False)):
                log.error("Heslo jsem neuložil.")
                return EXIT_MAIL

    target = credentials.target_name(e.user, e.smtp_server)
    try:
        store.write(target, e.user, password)
    except credentials.CredentialError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    log.info("Heslo uloženo do Správce pověření Windows jako „%s“ (jen pro tento počítač a tvůj účet Windows).", target)

    secrets_path = paths.secrets_file()
    if secrets_file.has_email_password(secrets_path):
        if _ask_yes("Soubor secrets.toml pořád obsahuje heslo v čistém textu. Odstranit ho odtud?", default=True):
            if secrets_file.clear_email_password(secrets_path):
                log.info("Heslo ze secrets.toml odstraněno (zbytek souboru beze změny).")
            else:
                log.warning("Heslo ze secrets.toml se nepodařilo bezpečně odstranit. Smaž ho ručně: "
                            "v sekci [email] nech řádek heslo = \"\".")
        else:
            log.warning("secrets.toml heslo dál obsahuje; program použije to ze Správce pověření.")
    log.info("Hotovo. Vyzkoušej: %s test-email", _cmd())
    return EXIT_OK


def cmd_delete_password(args: argparse.Namespace) -> int:
    prepared = _password_cfg()
    if isinstance(prepared, int):
        return prepared
    cfg, store = prepared
    target = credentials.target_name(cfg.email.user, cfg.email.smtp_server)
    try:
        deleted = store.delete(target)
    except credentials.CredentialError as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    if deleted:
        log.info("Heslo „%s“ smazáno ze Správce pověření. Bez hesla se souhrn neodešle.", target)
    else:
        log.info("Ve Správci pověření žádné heslo „%s“ není.", target)
    return EXIT_OK


def cmd_templates(args: argparse.Namespace) -> int:
    target = paths.user_templates_dir()
    created, skipped = export_defaults(target)
    for path in created:
        log.info("Vytvořeno: %s", path)
    for path in skipped:
        log.info("Už existuje, nechávám beze změny: %s", path)
    log.info("Šablony ve složce %s se teď používají místo výchozích. Úpravy dělej v kopii; když nějaký soubor "
             "smažeš, použije se zase výchozí. Po aktualizaci programu tě běh upozorní, kdyby šablona něco "
             "nového postrádala.", target)
    return EXIT_OK


def _make_fetcher(cfg) -> Fetcher:
    return Fetcher(delay_min=cfg.general.delay_min, delay_max=cfg.general.delay_max, timeout=cfg.general.timeout)


# Návratové kódy (Plánovač úloh je ukazuje ve sloupci „Výsledek posledního spuštění“).
EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_MAIL = 0, 1, 2, 3


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config_with_password()
    for warning in cfg.warnings:
        log.warning("%s", warning)
    if not args.dry_run:
        # Chyby v [email] odhalit hned, ne až po deseti minutách stahování.
        try:
            mailer.check_settings(cfg.email, cfg.secrets.email_password)
        except mailer.MailError as exc:
            log.error("%s Ověření nastavení: jobagent test-email", exc)
            return EXIT_CONFIG

    # Úklid na začátku každého běhu: Plánovač úloh běží denně, takže složka
    # se udržuje sama, bez dalšího příkazu nebo naplánované úlohy.
    outputs.cleanup(paths.output_dir(), cfg.general.keep_output_days)

    threshold = cfg.summary.score_threshold
    storage = Storage(paths.db_file())
    try:
        report = pipeline.run(
            cfg, storage, _make_fetcher(cfg), dry_run=args.dry_run, unknown_samples_dir=paths.unknown_samples_dir()
        )
        now = datetime.now()
        templates = Templates(paths.user_templates_dir())
        # Varování k upraveným šablonám jdou do logu i do e-mailu: v e-mailu je
        # uvidí i kamarád, který log neotvírá.
        report.warnings.extend(templates.check())
        html_body, text_body, attachment = emailer.build_bodies(report, templates, now, threshold)
        # Zpráva se sestaví i u --dry-run: uložený .eml je pak přesně to, co by
        # odešlo, a `test-email --nahled` ho umí poslat beze změny obsahu.
        msg = mailer.build_message(
            cfg.email, emailer.subject(report, cfg.email.subject, threshold), text_body, html_body,
            attachment_html=attachment, attachment_name=emailer.ATTACHMENT_NAME, now=now,
        )
        summary_html = attachment or html_body  # celý souhrn pro prohlížeč
        for warning in report.warnings:
            log.warning("%s", warning)

        if args.dry_run:
            html_path, eml_path = outputs.save_summary(msg, summary_html, paths.output_dir(), now)
            log.info("Náhled uložen: %s (zpráva tak, jak by odešla: %s)", html_path, eml_path.name)
            if not args.no_open:
                emailer.open_in_browser(html_path)
            mail_failed = False
        else:
            mail_failed = not _deliver(cfg, storage, report, now, msg, summary_html)
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
             msg: EmailMessage, summary_html: str) -> bool:
    """Odeslat souhrn; jen při úspěchu označit nabídky jako odeslané. True = odesláno."""
    has_attachment = msg.is_multipart() and msg.get_content_type() == "multipart/mixed"
    log.info("Odesílám souhrn (%.0f kB%s) na %s.", len(msg.as_bytes()) / 1024,
             ", celý v příloze" if has_attachment else "", msg["To"])
    try:
        mailer.send(msg, cfg.email, cfg.secrets.email_password)
    except mailer.MailError as exc:
        html_path, eml_path = outputs.save_summary(msg, summary_html, paths.output_dir(), now, unsent=True)
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
    try:
        html_path, eml_path = outputs.save_summary(msg, summary_html, paths.output_dir(), now)
        log.info("Kopie uložena: %s a %s", html_path, eml_path.name)
    except OSError as exc:
        # E-mail odešel a nabídky jsou označené; chybějící kopie nesmí běh shodit.
        log.warning("Kopii souhrnu se nepodařilo uložit do %s: %s", paths.output_dir(), exc)
    return True


def cmd_test_email(args: argparse.Namespace) -> int:
    cfg = _load_config_with_password()
    for warning in cfg.warnings:
        log.warning("%s", warning)
    e = cfg.email
    # Heslo se nikdy nevypisuje, ani jeho délka: stačí vědět, jestli je vyplněné.
    log.info("Server: %s:%s (%s), uživatel: %s, odesílatel: %s, příjemci: %s, heslo: %s",
             e.smtp_server, e.port, e.security, e.user or "—", e.sender or e.user or "—",
             ", ".join(e.recipients) or "—",
             (f"uložené ({cfg.secrets.email_password_source or 'vyplněné'})"
              if cfg.secrets.email_password else "CHYBÍ"))
    now = datetime.now()

    if args.nahled:
        msg = _saved_preview(e, now)
        if msg is None:
            return EXIT_ERROR
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


def _saved_preview(e, now: datetime) -> EmailMessage | None:
    """Pro `test-email --nahled`: poslední uložený .eml, jinak poslední HTML (starší běhy)."""
    out = paths.output_dir()
    eml = outputs.latest(out, ".eml")
    if eml is not None:
        msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
        html_part = msg.get_body(("html",))
        body_size = len(html_part.get_content().encode("utf-8")) if html_part is not None else 0
        has_attachment = any(True for _ in msg.iter_attachments())
        log.info("Posílám znovu uloženou zprávu %s (celkem %.0f kB, tělo %.0f kB, příloha: %s).",
                 eml.name, len(eml.read_bytes()) / 1024, body_size / 1024, "ano" if has_attachment else "ne")
        _warn_big_body(body_size)
        return mailer.as_test(msg, e, now)

    html_file = outputs.latest(out, ".html")
    if html_file is None:
        log.error("Ve složce %s není žádný uložený souhrn. Nejdřív spusť: jobagent run --dry-run --no-open", out)
        return None
    html_body = html_file.read_text(encoding="utf-8")
    size = len(html_body.encode("utf-8"))
    log.info("Uložená zpráva (.eml) není, posílám HTML %s (%.0f kB) jako tělo.", html_file.name, size / 1024)
    _warn_big_body(size)
    text_body = (f"Zkušební odeslání náhledu {html_file.name} ({size / 1024:.0f} kB).\n"
                 "Souhrn je v HTML části zprávy. Nabídky se neoznačily jako odeslané.\n")
    subject = f"{e.subject}: náhled {html_file.stem.removeprefix(outputs.PREFIX)}"
    return mailer.as_test(mailer.build_message(e, subject, text_body, html_body, now=now), e, now)


def _warn_big_body(size: int) -> None:
    if size > emailer.BODY_LIMIT_BYTES:
        # Záměrně se nezkracuje: test má ukázat, jak pošta zachází s velkým e-mailem.
        log.warning("Tělo má přes %d kB. Gmail takové tělo usekne („Zpráva byla oříznuta“); ostrý běh "
                    "by v těle nechal jen prvních %d ostatních a celý souhrn dal do přílohy.",
                    emailer.BODY_LIMIT_BYTES // 1000, emailer.BODY_OTHER_ROWS)


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
