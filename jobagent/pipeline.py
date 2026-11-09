"""Orchestrace jednoho běhu: sběr → filtry → detail → hodnocení → výsledek.

Pipeline nic neposílá ani nezapisuje „odesláno“. Vrací RunReport a o
odeslání a označení rozhoduje volající (cli). Proč: `--dry-run` a ostrý
běh pak sdílejí přesně stejnou logiku a liší se jen posledním krokem,
takže náhled e-mailu ukazuje přesně to, co by šlo kamarádovi.

Chyba jednoho hledání nebo jednoho detailu běh nezastaví. Zapíše se do
logu i do e-mailu a pokračuje se dál. Je lepší dostat 80 % nabídek s
varováním než nic.

Nabídky, které prošly filtry, se dělí podle toho, jestli máme text detailu:
  1. s textem          → hlavní tabulka / „Ostatní“ podle hranice skóre
                         (jen tyhle půjdou ve fázi 5 do AI),
  2. trvale bez textu  → „Zajímavé podle názvu“ (kladné body v názvu)
                         nebo „Ostatní“ (bez popisu). Trvale = JS stránka,
                         nebo stažení selhalo v MAX_DETAIL_ATTEMPTS bězích,
  3. dočasně bez textu → odloženo: do e-mailu nejde a neoznačí se jako
                         odeslané. Zařadí se, až se detail stáhne.
Proč: skóre bez popisu se se skóre s popisem nedá srovnat (chybí body
z popisu). Smíchat je v jedné tabulce by bylo zavádějící.

Skrytá mzda: portály jobs.cz a prace.cz filtrují i podle mzdy, kterou firma
zadala a nezveřejnila. Každé hledání se proto u nich projde dvakrát (bez
filtru mzdy a s min_mzda, viz _salary_pass). Druhý průchod čte jen výpisy,
detaily se nestahují. Inzerát bez viditelné mzdy, který vrátilo jen hledání
bez filtru, dostane srážku a poznámku; vyřazený kvůli tomu není.

Účetnictví počtů (stavový řádek musí sedět beze zbytku):
    nalezeno = unikátní + opakované (stejné ID v dalším hledání)
    unikátní = nové (1 + 2) + odložené (3) + vyřazené + dříve vyřazené
               + duplicity + už odeslané
RunReport.balance_error() to hlídá a test na to spoléhá.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from jobagent import collectors, filters, salary, scoring
from jobagent.collectors.base import Collector
from jobagent.config import Config
from jobagent.dedup import dedup_key, group_duplicates, has_cross_id, merge_into
from jobagent.fetch import FetchError, Fetcher, RobotsUnavailable
from jobagent.models import Job
from jobagent.storage import FAILED_NOTE, NO_TEXT_NOTE, Storage

log = logging.getLogger(__name__)

MAX_UNKNOWN_SAMPLES_PER_RUN = 5
MAX_DETAIL_ATTEMPTS = 3  # neúspěšné běhy, po kterých se detail bere jako trvale nedostupný
BACKLOG_RUNS = 3  # „čeká na detail“ roste tolik ostrých běhů po sobě → varování


@dataclass
class SearchStat:
    name: str
    url: str
    portal: str | None
    found: int = 0
    error: str | None = None
    skipped: str | None = None  # robots.txt nedostupný → v tomto běhu přeskočeno
    first_run: bool = False  # ještě nemělo ostrý běh → delší limit stáří


@dataclass
class RunReport:
    started: datetime
    dry_run: bool
    run_id: int | None = None  # řádek v tabulce runs; po odeslání se označí jako doručený
    stats: list[SearchStat] = field(default_factory=list)
    unique: int = 0
    new_jobs: list[Job] = field(default_factory=list)  # skupina 1: s textem, seřazené podle skóre
    title_jobs: list[Job] = field(default_factory=list)  # skupina 2: bez textu, kladné body v názvu
    no_text_other: list[Job] = field(default_factory=list)  # skupina 2: bez textu, ostatní
    pending: list[Job] = field(default_factory=list)  # skupina 3: odloženo, čeká na detail
    pending_outside_first_run: int = 0  # odložené mimo první běh hledání (pro varování o limitu)
    rejected: list[Job] = field(default_factory=list)  # nově vyřazené, oříznuté na max_vyrazenych
    rejected_total: int = 0  # nově vyřazené celkem (před oříznutím)
    rejected_known: int = 0  # vyřazené znovu, už dříve ukázané
    already_sent: int = 0
    duplicates: int = 0  # stejná pozice pod jiným ID (jiný portál/agentura)
    details_fetched: int = 0
    details_no_text: int = 0
    details_over_limit: int = 0
    warnings: list[str] = field(default_factory=list)
    scoring_method: str = "klíčová slova"

    @property
    def found_total(self) -> int:
        return sum(s.found for s in self.stats)

    @property
    def repeated(self) -> int:
        return self.found_total - self.unique

    @property
    def shown_total(self) -> int:
        """Všechny nové nabídky v e-mailu (s textem i bez)."""
        return len(self.new_jobs) + len(self.title_jobs) + len(self.no_text_other)

    @property
    def shown_jobs(self) -> list[Job]:
        """Nové nabídky, které e-mail ukazuje (v těle nebo v příloze) – po odeslání se označí."""
        return self.new_jobs + self.title_jobs + self.no_text_other

    @property
    def listing_only(self) -> int:
        """Nové nabídky v e-mailu hodnocené jen podle výpisu (trvale bez textu detailu).

        Když jich je hodně, je to signál pro nový collector nebo jiný zdroj.
        """
        return len(self.title_jobs) + len(self.no_text_other)

    def only_on_secondary(self) -> dict[str, tuple[int, int]]:
        """Kolik nových nabídek přinesl jen portál s nižší předností.

        {portál: (počet, z toho s ID hlavního portálu)}. Hlavní verze je z nižšího
        portálu jen tehdy, když stejný inzerát nenašlo žádné hledání na portálu
        s vyšší předností – ani podle ID, ani podle názvu + firmy + obce.
        Druhé číslo říká, kolik z nich na hlavním portálu existuje (výpis nese
        jeho ID), jen ho tvá hledání tam nenašla. Rozliší se tak „portál přidává
        nové inzeráty“ od „portál jen jinak vyhledává“.
        """
        top = collectors.COLLECTORS[0].name if collectors.COLLECTORS else ""
        counts: dict[str, tuple[int, int]] = {}
        for job in self.new_jobs + self.title_jobs + self.no_text_other:
            if job.portal == top:
                continue
            total, with_top_id = counts.get(job.portal, (0, 0))
            counts[job.portal] = (total + 1, with_top_id + (1 if job.alt_key.startswith(f"{top}:") else 0))
        return counts

    def hidden_salary_counts(self) -> tuple[int, int]:
        """(pravděpodobně pod minimem, nad minimem) mezi novými nabídkami v e-mailu."""
        shown = [j for j in self.new_jobs + self.title_jobs + self.no_text_other if j.hidden_salary]
        below = sum(1 for j in shown if j.hidden_salary.below)
        return below, len(shown) - below

    @property
    def errors(self) -> int:
        return sum(1 for s in self.stats if s.error)

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.stats if s.skipped)

    def balance_error(self) -> str | None:
        parts = (self.shown_total + len(self.pending) + self.rejected_total + self.rejected_known
                 + self.duplicates + self.already_sent)
        if parts != self.unique:
            return f"počty nesedí: unikátních {self.unique}, součet kategorií {parts}"
        return None

    def summary(self) -> dict:
        return {
            "nalezeno": self.found_total,
            "unikatnich": self.unique,
            "opakovanych": self.repeated,
            "novych": self.shown_total,
            "s_textem": len(self.new_jobs),
            "podle_nazvu": len(self.title_jobs),
            "jen_z_vypisu": self.listing_only,
            "jen_na_portalu": {p: c for p, (c, _) in self.only_on_secondary().items()},
            "ceka_na_detail": len(self.pending),
            "ceka_mimo_prvni_beh": self.pending_outside_first_run,
            "vyrazeno": self.rejected_total,
            "drive_vyrazeno": self.rejected_known,
            "duplicit": self.duplicates,
            "uz_odeslano": self.already_sent,
            "skryta_mzda_pod": self.hidden_salary_counts()[0],
            "skryta_mzda_nad": self.hidden_salary_counts()[1],
            "detailu": self.details_fetched,
            "chyb": self.errors,
            "preskoceno": self.skipped,
        }


def run(
    cfg: Config,
    storage: Storage,
    fetcher: Fetcher,
    *,
    dry_run: bool,
    now: datetime | None = None,
    unknown_samples_dir: Path | None = None,
) -> RunReport:
    now = now or datetime.now()
    today = now.date()
    report = RunReport(started=now, dry_run=dry_run)
    report.warnings.extend(cfg.warnings)
    run_id = storage.start_run(now, dry_run)
    report.run_id = run_id

    # 1) Sběr výpisů ---------------------------------------------------------
    jobs: dict[str, Job] = {}
    salary_checks: dict[str, bool] = {}  # klíč inzerátu → prošel filtrem mzdy portálu (jen jisté odpovědi)
    first_run_jobs: set[str] = set()  # klíče inzerátů z hledání, která ještě neměla ostrý běh
    for search in cfg.searches:
        collector = collectors.for_url(search.url, search.portal)
        stat = SearchStat(
            name=search.name,
            url=search.url,
            portal=collector.name if collector else None,
            first_run=not storage.had_live_run(search.url, exclude_run=run_id),
        )
        report.stats.append(stat)
        if collector is None:
            stat.error = (
                f"Nepodporovaný portál ({urlsplit(search.url).netloc or search.portal}). "
                f"Umím: {', '.join(collectors.supported_portals())}."
            )
        else:
            url = _search_url(collector, search.name, search.url, cfg, report)
            max_pages = search.max_pages or cfg.general.max_pages
            try:
                found, notes = collector.collect(
                    fetcher, url, max_pages, search.name, today, debug_dir=unknown_samples_dir,
                )
                report.warnings.extend(f"POZOR – {search.name}: {note}" for note in notes)
                stat.found = len(found)
                for job in found:
                    jobs.setdefault(job.key, job)  # stejné ID ze dvou hledání = jeden inzerát
                    if stat.first_run:
                        first_run_jobs.add(job.key)
                if found and collector.salary_param and cfg.filters.min_salary:
                    _salary_pass(collector, fetcher, url, max_pages, search.name, found, cfg, today,
                                 unknown_samples_dir, report, salary_checks)
            except RobotsUnavailable as exc:
                stat.skipped = str(exc)
            except FetchError as exc:
                stat.error = str(exc)
            except Exception as exc:  # chyba parseru = portál nejspíš změnil web
                log.exception("Hledání %s selhalo", search.name)
                stat.error = f"Chyba při zpracování výpisu: {exc!r}"
        storage.record_search(run_id, stat.name, stat.url, stat.portal, stat.found, stat.error or stat.skipped)
        _warn_about_search(stat, storage, run_id, report)
    report.unique = len(jobs)
    for key, passed in salary_checks.items():
        jobs[key].salary_checks[jobs[key].portal] = passed

    def age_limit(job: Job) -> int | None:
        if job.key in first_run_jobs and cfg.filters.max_age_first_run:
            return cfg.filters.max_age_first_run
        return cfg.filters.max_age_days

    # 2) Spojení s DB, deduplikace (PŘED detaily) a první kolo filtrů --------
    for job in jobs.values():
        # Do DB jdou všechny verze včetně duplicit: každá si nese vlastní detail,
        # který může posloužit jako záloha (viz _borrow_text).
        storage.merge_seen(job, dedup_key(job.title, job.company, job.location), now)
    candidates: list[Job] = []
    for primary, duplicates in group_duplicates(list(jobs.values()), collectors.priority):
        for dup in duplicates:
            merge_into(primary, dup)
            if dup.key in first_run_jobs:  # nové hledání našlo i starší inzerát → jeho okno platí
                first_run_jobs.add(primary.key)
        primary.duplicates = duplicates
        report.duplicates += len(duplicates)
        if storage.is_sent(primary.key) or storage.identity_sent(primary.identity, primary.key):
            report.already_sent += 1
            continue
        title_key = dedup_key(primary.title, primary.company, primary.location)
        if not has_cross_id(primary) and storage.dedup_sent_elsewhere(title_key, primary.key):
            report.duplicates += 1  # stejný inzerát (název+firma+obec) už odešel pod jiným ID
            continue
        # Už teď (podle mzdy z výpisu), aby srážka působila i na pořadí stahování detailů.
        primary.hidden_salary = salary.resolve_hidden(primary, cfg.filters.min_salary)
        primary.reject_reason = filters.check(primary, cfg.filters, today, age_limit(primary))
        candidates.append(primary)

    # 3) Detaily – jen pro neodmítnuté a dosud nestažené, nejslibnější první --
    _fetch_details(cfg, storage, fetcher, candidates, report, today, unknown_samples_dir)
    _borrow_text(cfg, storage, fetcher, candidates, report, today)

    # 4) Druhé kolo filtrů, hodnocení, rozdělení do skupin, deduplikace ------
    accepted: list[Job] = []
    for job in candidates:
        if job.reject_reason is None:
            job.reject_reason = filters.check(job, cfg.filters, today, age_limit(job))
        # Znovu: detail mohl doplnit viditelnou mzdu, pak skrytá mzda neplatí.
        job.hidden_salary = salary.resolve_hidden(job, cfg.filters.min_salary)
        job.score, job.score_reasons = scoring.score(job, cfg.scoring)
        job.title_score = scoring.title_points(job, cfg.scoring)
        # min_skore jen u nabídek s textem: bez popisu je skóre nesrovnatelně nízké
        # a vyřadit je kvůli tomu by šlo proti smyslu sekce „podle názvu“.
        if (job.reject_reason is None and job.text and cfg.scoring.min_score is not None
                and job.score < cfg.scoring.min_score):
            job.reject_reason = f"nízké skóre ({job.score} < {cfg.scoring.min_score})"
        storage.save_evaluation(job)
        if job.reject_reason is not None:
            if storage.rejection_reported(job.key):
                report.rejected_known += 1
            else:
                report.rejected.append(job)
        elif classify(job) == "pending":
            report.pending.append(job)  # skupina 3: detail teprve přijde
            if job.key not in first_run_jobs:
                report.pending_outside_first_run += 1
        else:
            accepted.append(job)

    accepted.sort(key=lambda j: (j.score if j.text else j.title_score), reverse=True)
    for job in accepted:
        group = classify(job)
        if group == "text":
            report.new_jobs.append(job)
        elif group == "title":
            report.title_jobs.append(job)
        else:
            report.no_text_other.append(job)
    _warn_about_backlog(storage, run_id, report)

    report.rejected_total = len(report.rejected)
    report.rejected = report.rejected[: cfg.summary.max_rejected] if cfg.summary.show_rejected else []

    problem = report.balance_error()
    if problem:  # nemělo by nastat; kdyby ano, chceme o tom vědět, ne tiše poslat špatná čísla
        log.error("Interní chyba: %s", problem)
        report.warnings.append(f"Interní chyba programu: {problem}. Pošli prosím log autorovi.")

    storage.finish_run(run_id, datetime.now(), report.summary())
    storage.commit()
    log.info("Běh hotov: %s", report.summary())
    return report


def _search_url(collector: Collector, name: str, url: str, cfg: Config, report: RunReport) -> str:
    """URL hledání bez parametru mzdy. Minimum se bere jen z [filtry] min_mzda.

    Proč: filtr mzdy v URL vyřadí i inzeráty se skrytou mzdou, a to natrvalo
    a bez upozornění. Program místo toho hledá dvakrát (_salary_pass).
    """
    clean_url, in_url = collector.without_salary(url)
    if in_url is not None:
        then = (f"Minimum bere z [filtry] min_mzda ({cfg.filters.min_salary} Kč) a hledá dvakrát, bez filtru "
                "mzdy i s ním, aby nepřišel o inzeráty se skrytou mzdou."
                if cfg.filters.min_salary else
                "[filtry] min_mzda není nastavené, takže se hledá bez filtru mzdy.")
        report.warnings.append(
            f"POZOR – {name}: URL obsahuje {collector.salary_param}={in_url}, program ho ignoruje. {then} "
            f"Smaž ho prosím z URL v configu."
        )
    return clean_url


def _salary_pass(collector: Collector, fetcher: Fetcher, url: str, max_pages: int, name: str,
                 found: list[Job], cfg: Config, today: date, debug_dir: Path | None,
                 report: RunReport, checks: dict[str, bool]) -> None:
    """Druhé hledání se mzdou ≥ min_mzda: kdo z nalezených filtrem portálu prošel.

    Pravidla nejistoty (odpověď se zapíše jen tehdy, když je jistá):
      * prošel = je ve výsledcích. Platí i z neúplného průchodu,
      * neprošel = chybí ve výsledcích ÚPLNÉHO průchodu (bez chyby, bez
        oříznutí limitem stránek). Z neúplného průchodu se nic neusuzuje,
      * víc hledání: „prošel“ v kterémkoli z nich vyhrává nad „neprošel“.
    Detaily se tu nestahují, takže druhý průchod nečerpá limit detailů.
    """
    amount = cfg.filters.min_salary
    result = collector.collect_salary_pass(fetcher, url, amount, max_pages, today, debug_dir)
    passed = 0
    for job in found:
        if job.key in result.keys:
            checks[job.key] = True
            passed += 1
        elif result.complete:
            checks.setdefault(job.key, False)
    log.info("%s (%s): hledání se mzdou ≥ %s Kč vrátilo %d z %d nalezených%s", name, collector.name,
             amount, passed, len(found), "" if result.complete else " (neúplné)")
    if not result.complete:
        label = f"POZOR – {name}: hledání se mzdou ≥ {amount} Kč"
        report.warnings.extend(f"{label}: {note}" for note in result.notes)
        report.warnings.append(
            f"{label} neproběhlo celé, takže inzeráty, které v něm chybí, se jako "
            "„skrytá mzda pravděpodobně pod minimem“ neoznačí."
        )


def classify(job: Job) -> str:
    """Skupina nabídky, která prošla filtry: "text" | "title" | "no_text_other" | "pending".

    Jedno místo pro pravidla skupin (viz hlavička modulu), aby pipeline
    a `jobagent vysvetli` vždy řekly totéž.
    """
    if job.text:
        return "text"
    if not job.detail_fetched:
        return "pending"
    return "title" if job.title_score > 0 else "no_text_other"


def _detail_priority(job: Job) -> tuple:
    """Pořadí pro stahování detailů: skóre z výpisu, při shodě novější a s uvedenou mzdou.

    Proč tiebreak: jobs.cz dává štítek „Možnost občasné práce z domova“
    většině inzerátů, takže desítky z nich mají stejné předběžné skóre.
    Bez dalšího kritéria by o zbytku limitu rozhodovalo pořadí ve výpisu.
    """
    return (job.score, job.posted or date.min, bool(job.salary_text))


def _fetch_details(cfg, storage, fetcher, candidates, report, today, unknown_samples_dir) -> None:
    need_detail = [j for j in candidates if j.reject_reason is None and not j.detail_fetched]
    for job in need_detail:
        job.score, _ = scoring.score(job, cfg.scoring)  # předběžné skóre jen z názvu a štítků
    need_detail.sort(key=_detail_priority, reverse=True)
    budget = cfg.general.detail_limit
    saved_unknown = 0
    for job in need_detail:
        if report.details_fetched >= budget:
            report.details_over_limit += 1
            job.detail_note = "detail zatím nestažen (limit běhu) – hodnoceno jen podle výpisu"
            continue
        collector = collectors.by_name(job.portal)
        try:
            # fetch_page: beta verze webu se zopakuje, jinak by se detail
            # z bety omylem uložil jako „trvale bez textu“.
            response = collector.fetch_page(fetcher, collector.detail_url(job))
            detail = collector.parse_detail(response.text, today, job.title)
        except Exception as exc:
            if isinstance(exc, FetchError):
                log.warning("Detail %s se nepodařilo stáhnout: %s", job.url, exc)
            else:
                log.exception("Detail %s se nepodařilo zpracovat", job.url)
            attempts = storage.record_detail_failure(job, MAX_DETAIL_ATTEMPTS)
            if attempts >= MAX_DETAIL_ATTEMPTS:
                job.detail_fetched = True  # dál se nezkouší, jde do skupiny 2
                job.detail_note = FAILED_NOTE
            continue
        report.details_fetched += 1
        _apply_detail(job, detail)
        if detail.method == "bez-textu":
            report.details_no_text += 1
            job.detail_note = NO_TEXT_NOTE
        storage.save_detail(job, detail.method)
        log.debug("Detail %s (priorita %s): metoda %s, %d znaků",
                  job.key, _detail_priority(job), detail.method, len(detail.description))
        if detail.method in ("hlavni-text", "bez-textu") and unknown_samples_dir and saved_unknown < MAX_UNKNOWN_SAMPLES_PER_RUN:
            if _save_unknown_sample(unknown_samples_dir, response.url, job, response.text):
                saved_unknown += 1
    # Vyčerpaný limit sám o sobě není problém (první běh, nové hledání).
    # Varuje se až tehdy, když fronta odložených roste víc běhů po sobě (_warn_about_backlog).
    if report.details_over_limit:
        log.info("Limit detailů (%d) vyčerpán, %d inzerátů odloženo na další běh.", budget, report.details_over_limit)


def _borrow_text(cfg, storage, fetcher, candidates, report, today) -> None:
    """Hlavní verze skončila bez textu → vezmi text z duplicity na jiném portálu.

    Typicky inzerát z jobs.cz vede na kariérní web s JavaScriptem, ale na
    prace.cz má vlastní stránku s JSON-LD. Odkaz a historie zůstávají
    z hlavní verze, převezme se jen text (ten se pak čistí a boduje jako jiný).
    Duplicita se stahuje jen tehdy, když vede na vlastní web svého portálu.
    Kariérní web firmy by dopadl stejně jako hlavní verze, tak se vynechá.
    """
    for job in candidates:
        if job.reject_reason or job.text or not job.detail_fetched:
            continue
        for dup in job.duplicates:
            collector = collectors.by_name(dup.portal)
            if collector is None or not collector.handles(dup.url):
                continue
            if not dup.detail_fetched:
                if report.details_fetched >= cfg.general.detail_limit:
                    continue
                try:
                    response = collector.fetch_page(fetcher, collector.detail_url(dup))
                    detail = collector.parse_detail(response.text, today, dup.title)
                except Exception as exc:  # záloha je bonus: chyba nesmí shodit běh
                    log.warning("Záložní detail %s se nepodařilo získat: %s", dup.url, exc)
                    continue
                report.details_fetched += 1
                _apply_detail(dup, detail)
                storage.save_detail(dup, detail.method)
            if dup.text:
                job.description = dup.description
                job.detail_note = f"popis převzat z {dup.portal} (na {job.portal} stránka bez textu)"
                log.info("%s: text převzat z duplicity %s", job.key, dup.key)
                break


def _apply_detail(job: Job, detail) -> None:
    job.detail_fetched = True
    job.description = detail.description
    # Detail doplňuje jen to, co výpis neměl. Výpis je aktuálnější u mzdy
    # a portál ji tam ukazuje přímo, takže ho nepřepisujeme.
    job.salary_text = job.salary_text or detail.salary_text
    job.company = job.company or detail.company
    job.location = job.location or detail.location
    job.posted = job.posted or detail.posted


def _warn_about_backlog(storage: Storage, run_id: int, report: RunReport) -> None:
    """Varování, když „čeká na detail“ (mimo první běh hledání) roste BACKLOG_RUNS ostrých běhů po sobě.

    Mimo první běh: tam je velká fronta normální (okno 30 dní). Jen ostré
    běhy: dry-run nic neodesílá, takže by frontu zkresloval. Aktuální běh se
    počítá vždy, aby šel problém vidět už v náhledu.
    """
    history = storage.recent_live_summaries(run_id, BACKLOG_RUNS - 1)
    values = [h.get("ceka_mimo_prvni_beh", 0) for h in reversed(history)] + [report.pending_outside_first_run]
    if len(values) == BACKLOG_RUNS and values[-1] > 0 and all(a < b for a, b in zip(values, values[1:])):
        report.warnings.append(
            f"Nabídek čekajících na detail přibývá už {BACKLOG_RUNS} běhy po sobě "
            f"({' → '.join(map(str, values))}). Na tenhle objem je obecne.limit_detailu "
            "moc nízký – zvyš ho, jinak některé nabídky přijdou pozdě nebo vůbec."
        )


def _warn_about_search(stat: SearchStat, storage: Storage, run_id: int, report: RunReport) -> None:
    label = f"{stat.name} ({stat.portal or 'neznámý portál'})"
    if stat.error:
        report.warnings.append(f"CHYBA – {label}: {stat.error}")
        return
    if stat.skipped:
        report.warnings.append(f"PŘESKOČENO – {label}: {stat.skipped}")
        return
    if stat.found == 0:
        previous = storage.recent_max_found(stat.url, exclude_run=run_id)
        if previous:
            report.warnings.append(
                f"POZOR – {label} vrátilo 0 inzerátů, v předchozích bězích až {previous}. "
                "Portál možná změnil web a collector je potřeba opravit (viz log)."
            )


def _save_unknown_sample(folder: Path, url: str, job: Job, html: str) -> bool:
    """Uloží HTML detailu, který neuměl JSON-LD ani selektory portálu.

    Jeden soubor na doménu: stačí jako podklad pro selektory nebo test
    a složka se nebude každý den plnit dalšími kopiemi.
    """
    host = urlsplit(url).hostname or "neznamy"
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.glob(f"{host}_*.html")):
        return False
    safe_id = re.sub(r"[^\w-]", "_", job.native_id)[:40]
    path = folder / f"{host}_{safe_id}.html"
    path.write_text(f"<!-- Zdroj: {url} -->\n" + html, encoding="utf-8")
    log.info("Neznámé rozložení detailu uloženo do %s", path)
    return True
