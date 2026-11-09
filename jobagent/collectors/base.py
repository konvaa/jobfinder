"""Společné rozhraní collectoru a smyčka přes stránky výpisu.

Rozdělení práce:
  * konkrétní portál implementuje jen parse_listing() (čistá funkce
    HTML → inzeráty + odkaz na další stránku) a volitelně detail_selectors,
  * stahování, stránkování, limity a detail řeší tahle základní třída.

Proč parse_listing dostává HTML a ne URL: parser pak jde testovat offline
nad uloženými ukázkami v samples/, bez sítě a bez čekání.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from jobagent.detail import DetailResult, DetailSelectors, extract_detail
from jobagent.fetch import FetchError, Fetcher, Response
from jobagent.models import Job

log = logging.getLogger(__name__)


@dataclass
class ListingPage:
    jobs: list[Job] = field(default_factory=list)
    next_url: str | None = None


@dataclass
class SalaryPass:
    """Výsledek hledání se mzdou: klíče inzerátů, které filtrem portálu prošly.

    complete=False: průchod skončil chybou nebo na limitu stránek. Pak víme
    jen, kdo prošel, ne kdo neprošel (mohl být na nepřečtené stránce).
    """

    keys: set[str] = field(default_factory=set)
    complete: bool = False
    notes: list[str] = field(default_factory=list)


class Collector(ABC):
    # Pozn. k Pythonu: ABC + @abstractmethod způsobí, že collector bez
    # parse_listing nejde ani vytvořit. Chyba se tak ukáže hned při startu,
    # ne až uprostřed běhu.

    name: str = ""  # zobrazovaný název, zároveň prefix klíče v DB („jobs.cz“)
    domains: tuple[str, ...] = ()  # domény, podle kterých se pozná URL z configu
    detail_selectors: DetailSelectors | None = None
    # Hostitelé, na které portál občas přesměruje a jejichž HTML neumíme číst
    # (typicky betaverze webu). Odpověď odtud se zahodí a požadavek se zopakuje.
    unsupported_hosts: tuple[str, ...] = ()
    VARIANT_RETRIES = 3
    # Název parametru minimální mzdy v URL hledání (jobs.cz „salary“). Vyplní
    # se jen u portálu, který filtruje i podle SKRYTÉ mzdy (zadané firmou, ale
    # nezveřejněné). Pipeline pak hledá dvakrát, viz pipeline._salary_pass.
    salary_param: str | None = None

    @abstractmethod
    def parse_listing(self, html: str, page_url: str, today: date) -> ListingPage:
        """Z HTML jedné stránky výpisu vytáhne inzeráty a URL další stránky."""

    def handles(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def detail_url(self, job: Job) -> str:
        return job.url

    def unsupported_variant(self, response: Response) -> str | None:
        host = (urlsplit(response.url).hostname or "").lower()
        if host in self.unsupported_hosts:
            return f"portál přesměroval na {host} (jiná verze webu, kterou program neumí číst)"
        return None

    def fetch_page(self, fetcher: Fetcher, url: str) -> Response:
        """Stáhne stránku portálu. Když přijde nepodporovaná verze webu, zkusí to
        znovu s čistými cookies (nový los A/B testu), nejvýš VARIANT_RETRIES krát."""
        reason = None
        for attempt in range(1, self.VARIANT_RETRIES + 1):
            response = fetcher.get(url)
            reason = self.unsupported_variant(response)
            if reason is None:
                return response
            log.info("%s: %s (pokus %d), zkouším znovu s novou session.", self.name, reason, attempt)
            if hasattr(fetcher, "reset_session"):
                fetcher.reset_session()
        raise FetchError(f"{reason} ani po {self.VARIANT_RETRIES} pokusech: {url}")

    def empty_listing_reason(self, html: str) -> str | None:
        """Vysvětlení prázdné stránky výpisu, pokud ho portál sám uvádí (jinak None).

        Jen pro text upozornění: rozliší „portál hlásí nic nenalezeno“ od
        „parser HTML nepoznal“. Na chování (opakování, neúplný průchod) nemá vliv.
        """
        return None

    def native_id_from_url(self, url: str) -> str | None:
        """ID inzerátu z libovolné podoby jeho adresy (kvůli dohledání v DB).

        Proč ne celá URL: stejný inzerát má na portálu víc adres (jobs.cz:
        /rpd/ID/, /fp/firma/ID/, firma.jobs.cz/pd/ID, ?searchId=…).
        """
        return None

    def canonical_url(self, url: str) -> str:
        """Stejná podoba URL inzerátu, jakou ukládá výpis (kvůli dohledání v DB)."""
        return url

    def parse_detail(self, html: str, today: date, title: str | None = None) -> DetailResult:
        """Výchozí cesta je obecná vrstva. Portál ji přepíše jen ve výjimečném případě."""
        return extract_detail(html, self.detail_selectors, today, title)

    def without_salary(self, url: str) -> tuple[str, str | None]:
        """URL bez parametru mzdy a jeho původní hodnota (None = nebyl tam)."""
        if not self.salary_param:
            return url, None
        return query_without(url, self.salary_param)

    def with_salary(self, url: str, amount: int) -> str:
        """URL hledání s minimální mzdou (případná původní hodnota se nahradí)."""
        return query_with(url, self.salary_param, str(int(amount)))

    def first_page_url(self, url: str) -> str:
        """Adresa první stránky výpisu. Portál ji může upravit (víc inzerátů
        na stránku = méně požadavků). Výchozí: beze změny."""
        return url

    def collect(
        self, fetcher: Fetcher, url: str, max_pages: int, search_name: str, today: date,
        debug_dir: Path | None = None,
    ) -> tuple[list[Job], list[str]]:
        """Projde stránky výpisu. Vrací (inzeráty, upozornění pro e-mail)."""
        jobs, notes, _ = self._walk(fetcher, url, max_pages, search_name, today, debug_dir)
        return jobs, notes

    def collect_salary_pass(
        self, fetcher: Fetcher, url: str, amount: int, max_pages: int, today: date,
        debug_dir: Path | None = None,
    ) -> SalaryPass:
        """Druhé hledání se mzdou ≥ amount. Jen stránky výpisu, žádné detaily.

        Chyba hledání nesmí shodit běh: hlavní výsledky už máme z prvního
        průchodu, tady jde jen o doplňující informaci. Proto se chyba vrátí
        jako upozornění a průchod jako neúplný.
        """
        try:
            jobs, notes, complete = self._walk(fetcher, self.with_salary(url, amount), max_pages, "", today, debug_dir)
        except FetchError as exc:  # včetně RobotsUnavailable
            return SalaryPass(complete=False, notes=[f"stránku 1 se nepodařilo načíst: {exc}"])
        except Exception as exc:  # chyba parseru: stejné ošetření jako u hlavního hledání, jen bez pádu
            log.exception("%s: hledání se mzdou selhalo", self.name)
            return SalaryPass(complete=False, notes=[f"chyba při zpracování výpisu: {exc!r}"])
        return SalaryPass(keys={j.key for j in jobs}, complete=complete, notes=notes)

    def _walk(
        self, fetcher: Fetcher, url: str, max_pages: int, search_name: str, today: date,
        debug_dir: Path | None,
    ) -> tuple[list[Job], list[str], bool]:
        """Smyčka přes stránky. Třetí hodnota: došli jsme na konec výsledků bez chyby?"""
        jobs: list[Job] = []
        notes: list[str] = []
        complete = True
        seen_pages: set[str] = set()
        page_url: str | None = self.first_page_url(url)
        for page_no in range(1, max_pages + 1):
            if not page_url or page_url in seen_pages:
                break  # ochrana proti zacyklení, kdyby „další“ ukazovala na už viděnou stránku
            seen_pages.add(page_url)
            try:
                response = self.fetch_page(fetcher, page_url)
            except FetchError as exc:
                if page_no == 1:
                    raise
                # Inzeráty z předchozích stránek si necháme, jen upozorníme.
                notes.append(f"stránku {page_no} výpisu se nepodařilo načíst: {exc}")
                complete = False
                break
            page = self.parse_listing(response.text, response.url, today)
            if not page.jobs and page_no > 1:
                # Stránka, na kterou vedl odkaz „další“, nemá být prázdná. jobs.cz
                # občas jednorázově vrátí „Nenašli jsme žádné nabídky“, přestože
                # jeho vlastní stránkování stránku uvádí (8. 11. 2026, IT se mzdou,
                # stránka 3; o chvíli později 23 karet). Jeden pokus navíc je levný
                # a rozdíl mezi „neúplné hledání“ a „úplné“ je pro skrytou mzdu podstatný.
                log.info("%s: stránka %d je prázdná, zkouším ji stáhnout znovu.", self.name, page_no)
                try:
                    retry = self.fetch_page(fetcher, page_url)
                except FetchError:
                    retry = None  # zůstaneme u první odpovědi, níže se uloží a ohlásí
                if retry is not None:
                    retried = self.parse_listing(retry.text, retry.url, today)
                    if retried.jobs:
                        log.info("%s: stránka %d napodruhé v pořádku (%d inzerátů).",
                                 self.name, page_no, len(retried.jobs))
                    response, page = retry, retried
            for job in page.jobs:
                job.search_name = search_name
            log.info("%s: stránka %d → %d inzerátů", self.name, page_no, len(page.jobs))
            jobs.extend(page.jobs)
            if not page.jobs:
                if page_no > 1:
                    # Na stránku jsme došli přes odkaz „další“, takže inzeráty mít měla.
                    # Typicky jiná podoba HTML, kterou parser nezná → uložit pro rozbor.
                    saved = _save_listing(debug_dir, self.name, page_no, response.url, response.text)
                    reason = self.empty_listing_reason(response.text)
                    what = (f"stránka {page_no} výpisu je i napodruhé prázdná: {reason}" if reason else
                            f"stránka {page_no} výpisu ({len(response.text) // 1000} kB HTML) neobsahuje ani "
                            "napodruhé žádný inzerát, který by parser poznal")
                    notes.append(what + (f" – uloženo do {saved}" if saved else "") + ".")
                    complete = False
                break
            page_url = page.next_url
        else:
            # Smyčka doběhla na limit stránek, ne na konec výsledků. Kritérium je
            # existující odkaz na další stránku: přesnější než „poslední stránka je
            # plná“ (plná může být i úplně poslední, když počet vyjde na celé stránky).
            if page_url and page_url not in seen_pages:
                complete = False
                notes.append(
                    f"hledání je pravděpodobně oříznuté limitem stránek ({max_pages}): portál nabízí další "
                    f"stránku, ale program dál nečte. Zvyš max_stranek u tohoto hledání nebo v [obecne]."
                )
        return jobs, notes, complete


def query_without(url: str, name: str) -> tuple[str, str | None]:
    """URL bez parametru `name` a jeho původní hodnota (None = nebyl tam).

    Úpravy jsou na úrovni textu dotazu: ostatní parametry zůstanou přesně
    tak, jak je uživatel zkopíroval z prohlížeče (žádné překódování).
    """
    parts = urlsplit(url)
    kept, found = [], None
    for item in parts.query.split("&") if parts.query else []:
        key, _, value = item.partition("=")
        if unquote_plus(key) == name:
            found = unquote_plus(value)
        else:
            kept.append(item)
    return urlunsplit(parts._replace(query="&".join(kept))), found


def query_with(url: str, name: str, value: str) -> str:
    """URL s parametrem `name`=`value` na konci dotazu (původní hodnota se nahradí)."""
    base, _ = query_without(url, name)
    parts = urlsplit(base)
    query = "&".join(q for q in (parts.query, f"{name}={value}") if q)
    return urlunsplit(parts._replace(query=query))


def _save_listing(folder: Path | None, portal: str, page_no: int, url: str, html: str) -> Path | None:
    if folder is None:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"vypis_{portal}_str{page_no}_{datetime.now():%Y%m%d_%H%M%S}.html"
    path.write_text(f"<!-- Zdroj: {url} -->\n" + html, encoding="utf-8")
    log.info("Stránka výpisu bez inzerátů uložena do %s", path)  # upozornění jde do e-mailu přes report
    return path
