"""Collector pro jobs.cz.

Co jsme zjistili v reálném HTML (listopad 2026):
  * výpis se renderuje na serveru, urllib ho dostane celý (žádný JavaScript),
  * karta = article.SearchResultCard, stabilní ID v atributu data-jobad-id,
  * mzda (když je uvedená) je štítek .Tag--success, ostatní štítky jsou .Tag,
  * datum je jen relativní text („Přidáno dnes“, „Aktualizováno před 3 dny“).
    Pozor, stejné místo ukazuje i „Končí zítra“ (konec inzerátu) nebo
    „Příležitost dne“. To datum zveřejnění NENÍ a ignorujeme ho,
  * JSON-LD nemá výpis ani detail,
  * detail /rpd/{id}/ má stabilní atributy data-test="jd-…" → stačí selektory,
  * parametr salary=<Kč> filtruje i podle SKRYTÉ mzdy zadané firmou (python:
    20 → 10, všech 10 vyřazených mzdu neukazuje, mezi nimi VertiFlex). Program
    proto hledá dvakrát (bez a se salary=min_mzda), viz pipeline._salary_pass.
    Odkaz „další stránka“ parametr salary zachovává (ověřeno 8. 11. 2026).

robots.txt zakazuje /api/, /muj/, /nabidky-podle-cv/ a pár dalších. /prace/
a /rpd/ povolené jsou.

Pozor: jobs.cz občas přesměruje na beta.jobs.cz (A/B test). Řeší
Collector.fetch_page (zopakování) a cookies ve Fetcheru (zůstat v jedné verzi).
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from jobagent.collectors.base import Collector, ListingPage
from jobagent.dates import parse_czech_date
from jobagent.detail import DetailSelectors
from jobagent.models import Job
from jobagent.text import clean, fold

_RPD_PATH = re.compile(r"^/rpd/(\d+)/?")
# Všechny podoby adresy detailu, které jsme na jobs.cz viděli (ID = 20014…):
#   /rpd/ID/   /fp/firma-slug-123/ID/   firma.jobs.cz/pd/ID   …/detail-pozice?…&id=ID
_ID_IN_URL = re.compile(r"/(?:rpd|pd)/(\d{6,})|/fp/[^/]+/(\d{6,})|[?&]id=(\d{6,})")


class JobsCzCollector(Collector):
    name = "jobs.cz"
    domains = ("jobs.cz",)
    # beta.jobs.cz (A/B test, listopad 2026): Next.js, inzeráty dotahuje JS,
    # v HTML jen pár karet a přesměrování zahodí okruh i stránkování.
    unsupported_hosts = ("beta.jobs.cz",)
    salary_param = "salary"
    detail_selectors = DetailSelectors(
        text=(
            '[data-test="jd-header-text"]',
            '[data-test="jd-body-richtext"]',
            '[data-test="jd-info-item"]',
            '[data-test="jd-benefits"]',
        ),
        salary='[data-test="jd-salary"]',
    )

    def empty_listing_reason(self, html: str) -> str | None:
        # Klasická šablona hlásí prázdný výsledek touto větou (uvnitř je <strong>,
        # proto až v textu stránky). Když ji dostaneme na stránce, na kterou vedl
        # odkaz „další“, jde o chybu na straně portálu.
        if "nenasli jsme zadne nabidky" in fold(clean(BeautifulSoup(html, "html.parser").get_text(" "))):
            return ("jobs.cz hlásí „Nenašli jsme žádné nabídky“, přestože tuto stránku uvádí jeho vlastní "
                    "stránkování (dočasná chyba portálu, při dalším běhu se obvykle srovná)")
        return None

    def canonical_url(self, url: str) -> str:
        return _canonical_url(url)

    def native_id_from_url(self, url: str) -> str | None:
        m = _ID_IN_URL.search(url)
        return next(g for g in m.groups() if g) if m else None

    def parse_listing(self, html: str, page_url: str, today: date) -> ListingPage:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        for card in soup.select("article.SearchResultCard"):
            link = card.select_one("a[data-jobad-id]")
            if link is None or not link.get("href"):
                continue  # reklamní/doporučující bloky bez ID nejsou inzeráty
            job_id = link["data-jobad-id"]

            title_el = card.select_one("[data-test-ad-title]")
            title = clean(title_el["data-test-ad-title"]) if title_el else clean(link.get_text(" "))

            company_el = card.select_one(".SearchResultCard__footerItem [translate='no']")
            location_el = card.select_one('[data-test="serp-locality"]')
            salary_el = card.select_one(".SearchResultCard__body .Tag--success")
            tags = [
                clean(t.get_text(" "))
                for t in card.select(".SearchResultCard__body .Tag")
                if t is not salary_el
            ]

            status_el = card.select_one(".SearchResultCard__status")
            status = clean(status_el.get_text(" ")) if status_el else ""
            posted = parse_czech_date(status, today) if fold(status).startswith(("pridano", "aktualizovano")) else None

            jobs.append(
                Job(
                    portal=self.name,
                    native_id=job_id,
                    url=_canonical_url(urljoin(page_url, link["href"])),
                    title=title,
                    company=clean(company_el.get_text(" ")) if company_el else "",
                    location=clean(location_el.get_text(" ")) if location_el else "",
                    salary_text=clean(salary_el.get_text(" ")) if salary_el else "",
                    posted=posted,
                    tags=[t for t in tags if t],
                )
            )

        next_link = soup.select_one("a.Pagination__button--next[href]")
        next_url = urljoin(page_url, next_link["href"]) if next_link else None
        return ListingPage(jobs=jobs, next_url=next_url)


def _canonical_url(url: str) -> str:
    """Odstraní sledovací parametry (?searchId=…&rps=…) z odkazu na detail.

    Proč: odkaz v e-mailu je kratší a stejný inzerát má vždy stejnou URL.
    Mimo tvar /rpd/{id}/ adresu necháváme být, nevíme, co parametry dělají.
    """
    parts = urlsplit(url)
    m = _RPD_PATH.match(parts.path)
    if m and parts.hostname and parts.hostname.endswith("jobs.cz"):
        return f"{parts.scheme}://{parts.netloc}/rpd/{m.group(1)}/"
    return url
