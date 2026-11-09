"""Collector pro prace.cz (nová platforma Alma Career, stejná jako beta.jobs.cz).

Co jsme zjistili (listopad 2026):
  * hledání: /nabidky/?q=<slovo>&workLocationIds[]=<ID obce>;<km>
    (Brno = M265747, stejné ID obce jako na jobs.cz),
  * filtr mzdy salaryMin filtruje i podle SKRYTÉ mzdy (python: 7 → 3, všechny
    4 vyřazené mzdu neukazují, mezi nimi VertiFlex). Do URL v configu ho
    nedávat: program hledá sám dvakrát (bez a se salaryMin=min_mzda) a skrytou
    mzdu jen označí, viz pipeline._salary_pass,
  * výpis: 40 karet na stránku, stránkování &page=N, parser je v alma_next.py,
  * detail /nabidka/<uuid>/ i /firma/<slug>/nabidka/<uuid>/ má JSON-LD
    JobPosting → stačí obecná vrstva, žádné selektory,
  * asi třetina karet vede na kariérní weby firma.jobs.cz/pd/<ID> (JavaScript,
    bez textu) – stejně jako na jobs.cz,
  * přesměrování na betu (A/B test jako u jobs.cz) jsme v 18 pokusech neviděli.
    Kdyby se objevilo, stačí doplnit unsupported_hosts.

robots.txt zakazuje /hledat/, /auth/, /muj/, /asmt/ a cesty formuláře
odpovědi. /nabidky/ a /nabidka/ povolené jsou.
"""

from __future__ import annotations

import re
from datetime import date

from jobagent.collectors import alma_next
from jobagent.collectors.base import Collector, ListingPage

_UUID_IN_URL = re.compile(r"/nabidka/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                          r"|originJobAdUuid=([0-9a-f-]{36})")


class PraceCzCollector(Collector):
    name = "prace.cz"
    domains = ("prace.cz",)
    salary_param = "salaryMin"

    def parse_listing(self, html: str, page_url: str, today: date) -> ListingPage:
        return alma_next.parse_listing(html, page_url, self.name, today)

    def native_id_from_url(self, url: str) -> str | None:
        m = _UUID_IN_URL.search(url)
        return next(g for g in m.groups() if g) if m else None
