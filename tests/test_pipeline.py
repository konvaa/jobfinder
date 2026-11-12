"""Celý běh offline: config z config.example.toml, falešný web, DB v paměti."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jobagent import emailer, pipeline
from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.config import ConfigError, load_config
from jobagent.storage import Storage
from jobagent.templating import Templates

from tests.helpers import SAMPLES, FakeFetcher, sample

ROOT = SAMPLES.parent
NOW = datetime(2026, 11, 4, 10, 0)
LISTING_URL = "https://www.jobs.cz/prace/brno/?q%5B%5D=it&locality%5Bradius%5D=50"


def make_web(listing_html=None):
    listing_html = listing_html if listing_html is not None else sample("jobs_cz_vypis_zkraceny.html")
    page = JobsCzCollector().parse_listing(sample("jobs_cz_vypis_zkraceny.html"), LISTING_URL, NOW.date())
    titles = {j.url: j.title for j in page.jobs}
    unknown = sample("syntetika_nezname_rozlozeni.html")

    def unknown_layout(url):
        # Firemní stránka s neznámým rozložením, ale s názvem TOHOTO inzerátu:
        # obecná vrstva kontroluje, že hlavní text název obsahuje.
        return unknown.replace("Specialista IT pro výrobu", titles.get(url, "Specialista IT pro výrobu"))

    return FakeFetcher({
        page.next_url: "<html><body></body></html>",  # druhá stránka výpisu je prázdná
        LISTING_URL: listing_html,
        "https://www.jobs.cz/rpd/2001491347/": sample("jobs_cz_detail_zkraceny.html"),
        "https://www.jobs.cz/rpd/": unknown_layout,
    })


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = self.cfg.searches[:1]
        self.cfg.searches[0].url = LISTING_URL
        self.storage = Storage(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.unknown_dir = Path(self.tmp.name) / "nezname"

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def run_once(self, web=None, **kw):
        return pipeline.run(
            self.cfg, self.storage, web or make_web(), dry_run=True, now=NOW,
            unknown_samples_dir=self.unknown_dir, **kw,
        )

    def test_first_run(self):
        report = self.run_once()
        self.assertEqual(report.found_total, 8)
        self.assertEqual(report.errors, 0)
        titles = [j.title for j in report.new_jobs]
        self.assertIn("IT konzultant", titles)
        konzultant = next(j for j in report.new_jobs if j.title == "IT konzultant")
        self.assertTrue(konzultant.detail_fetched)
        self.assertIn("technickou podporou", konzultant.description)
        # Seřazeno podle skóre sestupně.
        scores = [j.score for j in report.new_jobs]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # Neznámé rozložení detailu se uložilo, ale jen jednou za doménu.
        self.assertEqual(len(list(self.unknown_dir.glob("www.jobs.cz_*.html"))), 1)
        # Stránka 2 (přes odkaz „další“) je ve falešném webu prázdná → uložená + upozornění.
        self.assertEqual(len(list(self.unknown_dir.glob("vypis_jobs.cz_str2_*.html"))), 1)
        self.assertTrue(any("stránka 2 výpisu" in w for w in report.warnings), report.warnings)

    def test_dry_run_does_not_consume_and_details_are_cached(self):
        first = self.run_once()
        web = make_web()
        second = self.run_once(web)
        self.assertEqual(len(second.new_jobs), len(first.new_jobs))
        self.assertFalse([u for u in web.requested if "/rpd/" in u], "detail se nemá stahovat znovu")

    def test_sent_jobs_are_not_new_again(self):
        first = self.run_once()
        self.storage.mark_sent(first.new_jobs, first.rejected, NOW)
        second = self.run_once()
        self.assertEqual(second.new_jobs, [])
        self.assertEqual(second.already_sent, len(first.new_jobs))

    def test_detail_limit_defers_jobs(self):
        # Nabídky, na které nevyšel limit, se odloží: nejsou v e-mailu, nejsou
        # „jen podle výpisu“ a jeden vyčerpaný limit ještě nevaruje.
        self.cfg.general.detail_limit = 2
        report = self.run_once()
        self.assertEqual(report.details_fetched, 2)
        self.assertEqual(len(report.pending), 6)
        self.assertEqual(report.shown_total, 2)
        self.assertTrue(all(j.description for j in report.new_jobs))
        self.assertFalse(any("limit_detailu" in w for w in report.warnings))
        self.assertIsNone(report.balance_error())
        # Další běh s vyšším limitem je zařadí normálně.
        self.cfg.general.detail_limit = 40
        later = self.run_once()
        self.assertEqual(later.pending, [])
        self.assertEqual(later.shown_total, 8)

    def test_zero_results_after_success_warns(self):
        self.run_once()
        empty = make_web(listing_html="<html><body><p>Web se změnil</p></body></html>")
        report = self.run_once(empty)
        self.assertTrue(any("vrátilo 0 inzerátů" in w for w in report.warnings), report.warnings)

    def test_reject_word_and_salary(self):
        self.cfg.filters.reject_words = ["logistik*"]
        self.cfg.filters.min_salary = 70000  # IT konzultant má max. 60 000 → vyřadit
        report = self.run_once()
        reasons = {j.title: j.reject_reason for j in report.rejected}
        self.assertIn("obsahuje", reasons["Projektový Manager/ka logistiky"])
        self.assertIn("pod minimem", reasons["IT konzultant"])
        # Inzeráty bez mzdy zůstávají.
        self.assertIn("Li-ion Project Manager", [j.title for j in report.new_jobs])

    def test_render_email(self):
        report = self.run_once()
        report.new_jobs[0].title = "<script>alert(1)</script>"
        html = emailer.render(report, Templates(), now=NOW)
        self.assertIn("nalezeno 8", html)
        self.assertIn("NÁHLED", html)
        self.assertNotIn("<script>alert", html)  # text z webu je escapovaný
        self.assertNotIn("$nabidky", html)  # komentáře šablony se nevykreslují
        self.assertEqual(html.count("IT konzultant"), 1)


class ConfigTests(unittest.TestCase):
    def write(self, text: str, bom=False) -> Path:
        tmp = tempfile.NamedTemporaryFile("wb", suffix=".toml", delete=False)
        tmp.write((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_example_config_is_clean(self):
        cfg = load_config(ROOT / "config.example.toml", ROOT / "secrets.example.toml")
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(cfg.filters.min_salary, 40000)
        self.assertEqual(cfg.filters.places[0].contains, "Praha")

    def test_typo_is_reported_as_warning(self):
        path = self.write('[[hledani]]\nurl = "https://www.jobs.cz/prace/"\n[filtry]\nmin_mzad = 1\n')
        cfg = load_config(path)
        self.assertTrue(any("filtry.min_mzad" in w for w in cfg.warnings))

    def test_wrong_type_is_error_with_key(self):
        path = self.write('[[hledani]]\nurl = "https://www.jobs.cz/prace/"\n[filtry]\nmin_mzda = "40000"\n')
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("filtry.min_mzda", str(ctx.exception))

    def test_bom_from_notepad(self):
        path = self.write('[[hledani]]\nurl = "https://www.jobs.cz/prace/"\n', bom=True)
        self.assertEqual(len(load_config(path).searches), 1)

    def test_missing_search(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[obecne]\nmax_stranek = 1\n"))


if __name__ == "__main__":
    unittest.main()
