"""Fáze 2: collector prace.cz (nová platforma Alma Career) a deduplikace jobs.cz × prace.cz.

Ukázky prace_cz_*.html jsou skutečné stránky z 6. 11. 2026. Inzerát VertiFlex
(Fullstack vývojář/ka) je na obou portálech: jobs.cz ID 2001502919, prace.cz
UUID e188232c-… – na něm se testuje párování podle ID.
"""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from jobagent import collectors, emailer, pipeline
from jobagent.collectors import alma_next
from jobagent.collectors.prace_cz import PraceCzCollector
from jobagent.config import Search, load_config
from jobagent.dates import parse_czech_date
from jobagent.dedup import group_duplicates
from jobagent.detail import extract_detail
from jobagent.models import Job
from jobagent.storage import Storage
from jobagent.templating import Templates

from tests.helpers import SAMPLES, FakeFetcher, sample
from tests.test_pipeline import LISTING_URL as JOBS_URL, make_web

ROOT = SAMPLES.parent
TODAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 10, 0)
PRACE_URL = "https://www.prace.cz/nabidky/?q=python&workLocationIds%5B%5D=M265747%3B40"
VERTIFLEX_UUID = "e188232c-0ed7-402c-82dc-7a6b5795c034"
VERTIFLEX_PRACE = f"https://www.prace.cz/nabidka/{VERTIFLEX_UUID}/"


class PraceListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = PraceCzCollector().parse_listing(sample("prace_cz_vypis_python.html"), PRACE_URL, TODAY)
        cls.by_id = {j.native_id: j for j in cls.page.jobs}

    def test_cards(self):
        self.assertEqual(len(self.page.jobs), 7)
        self.assertIsNone(self.page.next_url)  # 7 inzerátů = jedna stránka

    def test_fields_and_cross_id(self):
        job = self.by_id[VERTIFLEX_UUID]
        self.assertEqual(job.portal, "prace.cz")
        self.assertEqual(job.title, "Fullstack vývojář/ka (Python/Django + React/TypeScript)")
        self.assertEqual(job.company, "VertiFlex s.r.o.")
        self.assertEqual(job.location, "Ostrava-Poruba")
        self.assertEqual(job.url, VERTIFLEX_PRACE)  # bez ?rps=
        self.assertEqual(job.alt_key, "jobs.cz:2001502919")  # z vloženého JSON
        self.assertEqual(job.posted, date(2026, 11, 4))  # „updated“ z JSON, ne štítek „Včerejší“

    def test_career_site_card(self):
        job = self.by_id["e80957f4-0b4c-423e-9a3d-943ed85d2392"]
        self.assertTrue(job.url.startswith("https://opmobility.jobs.cz/pd/2001433654"))
        self.assertEqual(job.alt_key, "jobs.cz:2001433654")

    def test_page_two(self):
        url = "https://www.prace.cz/nabidky/?q=it&workLocationIds%5B%5D=M265747%3B40&page=2"
        page = PraceCzCollector().parse_listing(sample("prace_cz_vypis_it_strana2.html"), url, TODAY)
        self.assertEqual(len(page.jobs), 8)
        self.assertIsNone(page.next_url)  # poslední stránka – odkaz jen na sebe sama

    def test_next_page_by_number(self):
        html = '<a href="?q=it&page=1">1</a><a href="?q=it&page=2">2</a><a href="?q=it&page=3">3</a>'
        self.assertEqual(alma_next._next_page(alma_next.BeautifulSoup(html, "html.parser"),
                                              "https://www.prace.cz/nabidky/?q=it&page=2"),
                         "https://www.prace.cz/nabidky/?q=it&page=3")

    def test_registry_and_ids_from_url(self):
        c = collectors.for_url(PRACE_URL)
        self.assertIsInstance(c, PraceCzCollector)
        self.assertEqual(c.native_id_from_url(VERTIFLEX_PRACE + "?rps=2077"), VERTIFLEX_UUID)
        self.assertEqual(c.native_id_from_url(
            "https://www.prace.cz/firma/hjjnbs-onsemi/nabidka/7048c582-de91-4139-a442-e8fd39d300ce/"),
            "7048c582-de91-4139-a442-e8fd39d300ce")

    def test_yesterday_label(self):
        self.assertEqual(parse_czech_date("Včerejší", TODAY), date(2026, 11, 5))
        self.assertEqual(parse_czech_date("Dnešní", TODAY), TODAY)
        self.assertIsNone(parse_czech_date("Méně než týden", TODAY))  # nepřesné → nevíme


class PraceDetailTests(unittest.TestCase):
    def test_json_ld(self):
        detail = PraceCzCollector().parse_detail(sample("prace_cz_detail_vertiflex.html"), TODAY,
                                                 "Fullstack vývojář/ka (Python/Django + React/TypeScript)")
        self.assertEqual(detail.method, "json-ld")
        self.assertIn("warehouse control system", detail.description)
        self.assertEqual(detail.company, "VertiFlex s.r.o.")

    def test_json_ld_salary(self):
        detail = extract_detail(sample("prace_cz_detail_firma_onsemi.html"), None, TODAY)
        self.assertEqual(detail.salary_text, "20000 – 40000 CZK/měsíc")
        self.assertEqual(detail.location, "Rožnov pod Radhoštěm")


def job(portal, native_id, title, company, location, alt_key=""):
    return Job(portal=portal, native_id=native_id, url=f"https://{portal}/{native_id}", title=title,
               company=company, location=location, alt_key=alt_key)


class GroupingTests(unittest.TestCase):
    def groups(self, jobs):
        return group_duplicates(jobs, collectors.priority)

    def test_same_agency_same_title_different_towns(self):
        # Zadání: dvě stejně pojmenované pozice téže agentury v různých městech NESLUČOVAT.
        a = job("prace.cz", "u1", "Operátor výroby (m/ž)", "Jobs Contact Personal, s.r.o.", "Ostrava-Poruba")
        b = job("prace.cz", "u2", "Operátor výroby", "Jobs Contact Personal s.r.o.", "Zlín-Louky")
        self.assertEqual(len(self.groups([a, b])), 2)

    def test_same_title_same_town_merged_jobs_cz_first(self):
        p = job("prace.cz", "u1", "Účetní", "Firma s.r.o.", "Ostrava-Poruba")
        j = job("jobs.cz", "2001", "Účetní", "Firma, s. r. o.", "Ostrava – Poruba")
        groups = self.groups([p, j])  # prace.cz našel dřív, přesto vyhraje jobs.cz
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0].portal, "jobs.cz")
        self.assertEqual(groups[0][1], [p])

    def test_unsure_place_not_merged(self):
        p = job("prace.cz", "u1", "Lektor", "Firma s.r.o.", "Jihomoravský, Středočeský")
        j = job("jobs.cz", "2001", "Lektor", "Firma s.r.o.", "Jihomoravský, Středočeský")
        self.assertEqual(len(self.groups([p, j])), 2)

    def test_two_different_ids_not_merged_by_title(self):
        # Obě verze mají ID jobs.cz a liší se → podle portálu jde o dva inzeráty.
        a = job("jobs.cz", "2001", "Účetní", "Firma s.r.o.", "Ostrava")
        b = job("prace.cz", "u1", "Účetní", "Firma s.r.o.", "Ostrava", alt_key="jobs.cz:2002")
        self.assertEqual(len(self.groups([a, b])), 2)

    def test_id_match_ignores_title(self):
        a = job("jobs.cz", "2001502919", "Fullstack vývojář/ka", "VertiFlex", "Ostrava – Poruba")
        b = job("prace.cz", VERTIFLEX_UUID, "Fullstack developer", "", "", alt_key="jobs.cz:2001502919")
        self.assertEqual(len(self.groups([a, b])), 1)


class CrossPortalPipelineTests(unittest.TestCase):
    """jobs.cz + prace.cz v jednom běhu: VertiFlex je na obou."""

    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = [Search(name="jobs", url=JOBS_URL), Search(name="prace", url=PRACE_URL)]
        self.cfg.general.max_pages = 1
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def web(self, jobs_detail_without_text=False):
        web = make_web()
        web.pages = {PRACE_URL: sample("prace_cz_vypis_python.html"), **web.pages}
        if jobs_detail_without_text:
            web.pages = {"https://www.jobs.cz/rpd/2001502919/": sample("syntetika_js_kostra_almacareer.html"),
                         **web.pages}
        web.pages[VERTIFLEX_PRACE] = sample("prace_cz_detail_vertiflex.html")
        web.pages["https://www.prace.cz/"] = sample("prace_cz_detail_firma_onsemi.html")
        web.pages["https://"] = sample("syntetika_js_kostra_almacareer.html")  # kariérní weby firem
        return web

    def run_once(self, web, dry_run=True):
        return pipeline.run(self.cfg, self.storage, web, dry_run=dry_run, now=NOW,
                            unknown_samples_dir=Path(self.tmp.name))

    def vertiflex(self, report):
        found = [j for j in report.new_jobs + report.title_jobs + report.no_text_other
                 if "Fullstack vývojář" in j.title]
        self.assertEqual(len(found), 1, "VertiFlex musí být v e-mailu právě jednou")
        return found[0]

    def test_merged_by_id_jobs_cz_wins_one_detail(self):
        web = self.web()
        report = self.run_once(web)
        job = self.vertiflex(report)
        self.assertEqual((job.portal, job.url), ("jobs.cz", "https://www.jobs.cz/rpd/2001502919/"))
        self.assertEqual(job.also_on, ["prace.cz"])
        self.assertGreaterEqual(report.duplicates, 1)
        self.assertFalse([u for u in web.requested if VERTIFLEX_UUID in u], "detail z prace.cz se nemá stahovat")
        self.assertIsNone(report.balance_error())
        html = emailer.render(report, Templates(), now=NOW)
        self.assertIn("jobs.cz + prace.cz", html)

    def test_only_on_prace_count(self):
        report = self.run_once(self.web())
        shown = report.new_jobs + report.title_jobs + report.no_text_other
        prace_only = [j for j in shown if j.portal == "prace.cz"]
        self.assertTrue(prace_only)
        self.assertNotIn("Fullstack vývojář/ka (Python/Django + React/TypeScript)",
                         [j.title for j in prace_only])  # VertiFlex je i na jobs.cz → nepočítá se
        with_id = sum(1 for j in prace_only if j.alt_key.startswith("jobs.cz:"))
        self.assertEqual(report.only_on_secondary(), {"prace.cz": (len(prace_only), with_id)})
        html = emailer.render(report, Templates(), now=NOW)
        self.assertIn(f"jen na prace.cz {len(prace_only)} (z toho {with_id} existuje i na jobs.cz, jen mimo tvá hledání)", html)

    def test_only_on_prace_without_jobs_cz_id(self):
        report = pipeline.RunReport(started=NOW, dry_run=True)
        report.new_jobs = [Job(portal="prace.cz", native_id="u1", url="u", title="A", alt_key="jobs.cz:1"),
                           Job(portal="prace.cz", native_id="u2", url="u", title="B"),
                           Job(portal="jobs.cz", native_id="3", url="u", title="C", also_on=["prace.cz"])]
        self.assertEqual(report.only_on_secondary(), {"prace.cz": (2, 1)})

    def test_text_borrowed_when_jobs_cz_has_none(self):
        web = self.web(jobs_detail_without_text=True)
        report = self.run_once(web)
        job = self.vertiflex(report)
        self.assertEqual(job.portal, "jobs.cz")  # odkaz a historie z jobs.cz
        self.assertIn("warehouse control system", job.text)  # text z prace.cz
        self.assertIn("převzat z prace.cz", job.detail_note)
        self.assertEqual(sum(VERTIFLEX_UUID in u for u in web.requested), 1)
        # Další běh: oba detaily jsou v DB, nic se nestahuje znovu a text se převezme z cache.
        web2 = self.web(jobs_detail_without_text=True)
        again = self.run_once(web2)
        self.assertFalse([u for u in web2.requested if "/rpd/" in u or "/nabidka/" in u])
        self.assertIn("warehouse control system", self.vertiflex(again).text)

    def test_sent_from_jobs_cz_not_resent_from_prace(self):
        first = self.run_once(self.web())
        self.storage.mark_sent(first.new_jobs + first.title_jobs + first.no_text_other, [], NOW)
        self.cfg.searches = [Search(name="prace", url=PRACE_URL)]  # jobs.cz dnes nic nevrátil
        report = self.run_once(self.web())
        self.assertFalse([j for j in report.new_jobs + report.title_jobs + report.no_text_other
                          if "Fullstack vývojář" in j.title])
        self.assertGreaterEqual(report.already_sent, 1)
        self.assertIsNone(report.balance_error())


if __name__ == "__main__":
    unittest.main()
