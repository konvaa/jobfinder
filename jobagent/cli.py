"""Příkazová řádka: `jobagent run [--dry-run]`, `jobagent sample <url>`, `jobagent vysvetli <url>`.

Vrací návratový kód (0 = OK, 1 = chyba, 2 = chyba v nastavení). Proč na tom
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
from pathlib import Path
from urllib.parse import urlsplit

from jobagent import __version__, collectors, emailer, explain, paths, pipeline
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

    sample = sub.add_parser("sample", help="uložit HTML stránky do samples/ (podklad pro parser a testy)")
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


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(paths.config_file(), paths.secrets_file())
    for warning in cfg.warnings:
        log.warning("%s", warning)
    if not args.dry_run:
        # Fáze 4 doplní odesílání. Do té doby radši jasná hláška než tichý „úspěch“.
        log.error("Odesílání e-mailu zatím není hotové (fáze 4). Spusť: jobagent run --dry-run")
        return 1

    storage = Storage(paths.db_file())
    try:
        report = pipeline.run(
            cfg, storage, _make_fetcher(cfg), dry_run=True, unknown_samples_dir=paths.unknown_samples_dir()
        )
    finally:
        storage.close()

    html = emailer.render(report, paths.templates_dir(), threshold=cfg.summary.score_threshold)
    preview = emailer.save_preview(html, paths.output_dir())
    log.info("Náhled e-mailu: %s", preview)
    for warning in report.warnings:
        log.warning("%s", warning)
    if not args.no_open:
        emailer.open_in_browser(preview)
    # Selhala nebo se přeskočila všechna hledání (výpadek sítě, blokace)?
    # Návratový kód 1, aby to bylo vidět v Plánovači úloh, i když e-mail
    # s upozorněním vznikl.
    failed = report.errors + report.skipped
    return 1 if report.stats and failed == len(report.stats) else 0


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
    folder = paths.samples_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.html"
    # Na první řádek zdroj a datum: za měsíc nikdo neví, odkud soubor je.
    header = f"<!-- Zdroj: {args.url} | konečná URL: {response.url} | uloženo {datetime.now():%Y-%m-%d %H:%M} -->\n"
    path.write_text(header + response.text, encoding="utf-8")
    log.info("Uloženo: %s (%d znaků)", path, len(response.text))
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
