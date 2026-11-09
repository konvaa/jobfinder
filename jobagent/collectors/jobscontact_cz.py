"""Collector pro jobscontact.cz (vlastní web personální agentury Jobs Contact).

Ověřeno 8. 11. 2026 na hledání IT s okruhem 40 km:
  * hledání = SEO adresa /prace/{obor}/{místo}. Okruh je parametr
    jobFilterRadius (bez něj 50 km). Filtr funguje i bez cookies, stav se
    nedrží v session,
  * výpis: karty div.job-box (název h2, mzda p.pay, místo p.place, štítky
    .best-ico). FIRMA ani DATUM na kartě nejsou, datum chybí i v detailu,
  * stránkování po 11. Parametr itemsPerPage=100 vrátí až 100 karet najednou
    (stejná množina ID jako po stránkách), viz first_page_url. Odkaz dál:
    ul.uk-pagination a[aria-label="⯈"], na poslední stránce chybí,
  * detail /prace/{slug}_id{číslo}: číslo je jejich vlastní ID (ne ID jobs.cz).
    Bez správného slugu vrací 404, proto se ukládá přesná adresa z výpisu,
  * detail nemá JSON-LD. Obecná vrstva by se mohla splést (<article> je
    upoutávka na blog, v boku „Další pozice v oboru“), proto selektory.
    Blok „Doplňující informace“ (pošlete CV + GDPR) selektory nevezmou,
  * místo je OKRES, ne obec („Ostrava-město“, „Přerov“ místo Hranic). Víc míst:
    karta ukazuje „více lokalit (N)“, seznam je v tooltipu,
  * filtr mzdy jobFilterSalary filtruje jen podle VIDITELNÉ mzdy (všech 120
    inzerátů s „Neuvedeno“ ponechá), skrytou mzdu nezná → salary_param = None,
    druhý průchod by nic nepřidal.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from jobagent.collectors.base import Collector, ListingPage, query_with, query_without
from jobagent.detail import DetailResult, DetailSelectors
from jobagent.models import Job
from jobagent.text import clean

# Jobs Contact je agentura a klienta neuvádí. Pevná firma kvůli párování
# s jejich inzeráty na jobs.cz („Jobs Contact Personal, s.r.o.“): bez firmy by
# dedup_key neexistoval a stejný inzerát by přišel dvakrát.
COMPANY = "Jobs Contact Personal, s.r.o."
ITEMS_PER_PAGE = "100"

_ID_IN_URL = re.compile(r"_id(\d+)(?:[/?#]|$)")
_NO_SALARY = re.compile(r"^\s*neuvedeno\s*$", re.IGNORECASE)


class JobsContactCzCollector(Collector):
    name = "jobscontact.cz"
    domains = ("jobscontact.cz",)
    detail_selectors = DetailSelectors(
        text=(
            # Úvodní odstavec a sloupce Náplň práce / Požadavky / Nabízíme.
            # Přímí potomci .content: „Doplňující informace“ (.next-info),
            # tlačítka a formulář jsou vedle, ne uvnitř.
            ".new-detail .job-content .content > p",
            ".new-detail .job-content .content > .uk-grid",
        ),
        salary=".new-detail .job-title .pay",
    )

    def first_page_url(self, url: str) -> str:
        # Jeden požadavek místo pěti (45 nabídek po 11). Uživatelova hodnota má přednost.
        if query_without(url, "itemsPerPage")[1] is not None:
            return url
        return query_with(url, "itemsPerPage", ITEMS_PER_PAGE)

    def native_id_from_url(self, url: str) -> str | None:
        m = _ID_IN_URL.search(urlsplit(url).path + "/")
        return m.group(1) if m else None

    def canonical_url(self, url: str) -> str:
        # Bez #form-detail a parametrů (?do=favorite…): stejná podoba jako ve výpisu.
        parts = urlsplit(url)
        return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))

    def parse_listing(self, html: str, page_url: str, today: date) -> ListingPage:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        seen: set[str] = set()
        for box in soup.select("div.job-box"):
            heading = box.select_one("h2")
            link = heading.find_parent("a", href=True) if heading else None
            if link is None:
                continue
            url = self.canonical_url(urljoin(page_url, link["href"]))
            native_id = self.native_id_from_url(url)
            if not native_id or native_id in seen:
                continue
            seen.add(native_id)
            pay = box.select_one(".pay")
            salary = clean(pay.get_text(" ")) if pay else ""
            jobs.append(Job(
                portal=self.name,
                native_id=native_id,
                url=url,
                title=clean(heading.get_text(" ")),
                company=COMPANY,
                location=_listing_place(box),
                salary_text="" if _NO_SALARY.match(salary) else salary,
                posted=None,  # web datum neuvádí (výpis ani detail)
                tags=[t for t in (clean(d.get_text(" ")) for d in box.select(".best-ico > div")) if t],
            ))
        nxt = soup.select_one('ul.uk-pagination a[aria-label="⯈"][href]')
        return ListingPage(jobs=jobs, next_url=urljoin(page_url, nxt["href"]) if nxt else None)

    def parse_detail(self, html: str, today: date, title: str | None = None) -> DetailResult:
        result = super().parse_detail(html, today, title)
        # Místa jsou samostatné <span>. Obecná vrstva by je slepila mezerou
        # („Ostrava-město Nový Jičín“) a z toho by vyšla obec „Ostrava“.
        places = [clean(s.get_text(" ")) for s in
                  BeautifulSoup(html, "html.parser").select(".new-detail .job-title .place span")]
        result.location = ", ".join(p for p in places if p)
        if _NO_SALARY.match(result.salary_text or ""):
            result.salary_text = ""
        return result


def _listing_place(box) -> str:
    """Místo z karty. U více míst je karta „více lokalit (N)“ a seznam v tooltipu.

    Víc míst se spojí čárkou: dedup.town() pak správně vrátí „nevíme“ a
    neslučuje se (stejně jako u „+ 2 další lokality“ na jobs.cz).
    """
    span = box.select_one(".place span")
    if span is None:
        return ""
    text = clean(span.get_text(" "))
    tooltip = span.get("uk-tooltip") or ""
    if text.lower().startswith("více lokalit") and tooltip:
        places = [clean(BeautifulSoup(p, "html.parser").get_text(" ")) for p in re.split(r"<br\s*/?>", tooltip)]
        places = [p for p in places if p]
        if places:
            return ", ".join(places)
    return text
