"""Prázdná stránka výpisu uprostřed stránkování (jobs.cz, 8. 11. 2026).

jobs_cz_vypis_prazdna_nekonzistentni.html je skutečná stránka 3 hledání
„IT, okruh 40 km“ s filtrem mzdy z dry-runu (anonymizovaná, viz samples/README.md). Klasická šablona (žádná beta),
text „Nenašli jsme žádné nabídky práce odpovídající zadání“, ale stránkování
uvádí 4 stránky a odkaz „Následující“ na stránku 4. O chvíli později vracela
stejná adresa normálně 23 karet (83 nabídek, 3 stránky). Dočasná chyba portálu,
proto se prázdná stránka zkusí stáhnout ještě jednou.
"""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from jobagent import pipeline
from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.config import Search, load_config
from jobagent.fetch import FetchError, Response
from jobagent.storage import Storage

from tests.helpers import SAMPLES, FakeFetcher, sample
from tests.test_hidden_salary import subset_listing
from tests.test_pipeline import LISTING_URL

ROOT = SAMPLES.parent
TODAY = date(2026, 11, 8)
EMPTY = "jobs_cz_vypis_prazdna_nekonzistentni.html"
PAGE1 = sample("jobs_cz_vypis_zkraceny.html")
PAGE2_URL = JobsCzCollector().parse_listing(PAGE1, LISTING_URL, TODAY).next_url
ALL_IDS = {j.native_id for j in JobsCzCollector().parse_listing(PAGE1, LISTING_URL, TODAY).jobs}


def sequence(*pages):
    """Hodnota pro FakeFetcher: při každém požadavku další stránka (poslední se opakuje)."""
    queue = list(pages)

    def serve(url):
        page = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(page, Exception):
            raise page
        return page

    return serve


class SavedPageTests(unittest.TestCase):
    def test_saved_file_is_classic_template_with_portal_message(self):
        c = JobsCzCollector()
        html = sample(EMPTY)
        page = c.parse_listing(html, "https://www.jobs.cz/prace/?q%5B0%5D=it&salary=50000&page=3", TODAY)
        self.assertEqual(page.jobs, [])
        self.assertIsNotNone(page.next_url, "stránkování si myslí, že existuje stránka 4")
        url = "https://www.jobs.cz/prace/?q%5B0%5D=it&salary=50000&page=3"
        self.assertIsNone(c.unsupported_variant(Response(url=url, text=html, status=200)), "není to beta")
        self.assertIn("Nenašli jsme žádné nabídky", c.empty_listing_reason(html))

    def test_normal_listing_has_no_reason(self):
        self.assertIsNone(JobsCzCollector().empty_listing_reason(PAGE1))


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.debug = Path(self.tmp.name)
        self.last = subset_listing(PAGE1, {"2001480136"})  # „správná“ stránka 2, bez dalšího odkazu

    def collect(self, page2, max_pages=3):
        web = FakeFetcher({PAGE2_URL: page2, LISTING_URL: PAGE1})
        with self.assertLogs("jobagent", level="INFO"):
            jobs, notes, complete = JobsCzCollector()._walk(web, LISTING_URL, max_pages, "it", TODAY, self.debug)
        return web, jobs, notes, complete

    def test_empty_once_then_ok(self):
        web, jobs, notes, complete = self.collect(sequence(sample(EMPTY), self.last))
        self.assertEqual(len(jobs), len(ALL_IDS) + 1)
        self.assertEqual((notes, complete), ([], True))
        self.assertEqual(web.requested.count(PAGE2_URL), 2)
        self.assertFalse(list(self.debug.iterdir()), "nic se neukládá, druhý pokus vyšel")

    def test_empty_twice(self):
        web, jobs, notes, complete = self.collect(sequence(sample(EMPTY)))
        self.assertEqual(len(jobs), len(ALL_IDS))  # stránka 1 zůstává
        self.assertFalse(complete)
        self.assertEqual(web.requested.count(PAGE2_URL), 2, "jen jeden pokus navíc")
        self.assertEqual(len(notes), 1)
        self.assertIn("i napodruhé prázdná: jobs.cz hlásí „Nenašli jsme žádné nabídky“", notes[0])
        self.assertEqual(len(list(self.debug.iterdir())), 1)

    def test_retry_fails_with_error(self):
        _, _, notes, complete = self.collect(sequence(sample(EMPTY), FetchError("HTTP chyba 503")))
        self.assertFalse(complete)
        self.assertIn("i napodruhé prázdná", notes[0])

    def test_unknown_layout_text(self):
        _, _, notes, _ = self.collect("<html><body><p>Něco úplně jiného</p></body></html>")
        self.assertIn("neobsahuje ani napodruhé žádný inzerát", notes[0])

    def test_first_page_empty_is_not_retried(self):
        web = FakeFetcher({LISTING_URL: subset_listing(PAGE1, set())})
        with self.assertLogs("jobagent", level="INFO"):
            jobs, notes, complete = JobsCzCollector()._walk(web, LISTING_URL, 3, "it", TODAY, self.debug)
        self.assertEqual((jobs, notes, complete, len(web.requested)), ([], [], True, 1))


class SalaryPassTests(unittest.TestCase):
    """Druhé hledání se mzdou: prázdná stránka 2 napoprvé nesmí zrušit označení."""

    KEEP = {"2001480136", "2001462015"}

    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = [Search(name="it", url=LISTING_URL)]
        self.cfg.general.max_pages = 3
        self.cfg.filters.min_salary = 50000
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_once(self, filtered_page2):
        web = FakeFetcher({
            LISTING_URL + "&salary=50000": subset_listing(PAGE1, {"2001480136"}, keep_next=True),
            PAGE2_URL: filtered_page2,  # odkaz „další“ ze stránky 1 (v ukázce bez salary, stačí)
            LISTING_URL: subset_listing(PAGE1, ALL_IDS),  # hledání bez filtru: jedna stránka
            "https://www.jobs.cz/rpd/": "<html><body></body></html>",
            "https://": "<html><body></body></html>",
        })
        report = pipeline.run(self.cfg, self.storage, web, dry_run=True, now=datetime(2026, 11, 8, 10),
                              unknown_samples_dir=Path(self.tmp.name))
        jobs = report.new_jobs + report.title_jobs + report.no_text_other + report.pending + report.rejected
        return report, {j.native_id: j.hidden_salary for j in jobs}

    def test_recovered_page_keeps_marks(self):
        report, hidden = self.run_once(sequence(sample(EMPTY), subset_listing(PAGE1, {"2001462015"})))
        self.assertFalse(hidden["2001480136"].below)
        self.assertFalse(hidden["2001462015"].below)  # ze stránky 2 po opakování
        self.assertTrue(hidden["2001502919"].below)
        self.assertFalse([w for w in report.warnings if "se mzdou" in w])

    def test_still_empty_no_below_marks(self):
        report, hidden = self.run_once(sequence(sample(EMPTY)))
        self.assertFalse(hidden["2001480136"].below)  # „prošel“ platí i z neúplného průchodu
        self.assertIsNone(hidden["2001502919"])
        self.assertTrue([w for w in report.warnings if "Nenašli jsme žádné nabídky" in w])
        self.assertTrue([w for w in report.warnings if "neproběhlo celé" in w])


if __name__ == "__main__":
    unittest.main()
