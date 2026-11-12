"""Skupiny nabídek podle textu detailu (s textem / podle názvu / odložené),
pokusy o detail napříč běhy, varování na rostoucí frontu a bodování pružné doby."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from jobagent import emailer, pipeline
from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.config import Scoring, load_config
from jobagent.fetch import FetchError
from jobagent.models import Job
from jobagent.scoring import score, title_points
from jobagent.storage import Storage
from jobagent.templating import Templates

from tests.helpers import SAMPLES, sample
from tests.test_pipeline import LISTING_URL, NOW, make_web

ROOT = SAMPLES.parent
FAILING_ID = "2001502919"  # Fullstack vývojář/ka (Python…) – kladné body v názvu


class FailingDetailWeb:
    """Web, kde detail jednoho inzerátu vždy selže (výpadek, chyba serveru)."""

    def __init__(self):
        self.web = make_web()
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        if FAILING_ID in url:
            raise FetchError("HTTP chyba 500")
        return self.web.get(url)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = self.cfg.searches[:1]
        self.cfg.searches[0].url = LISTING_URL
        self.storage = Storage(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.storage.close)

    def run_once(self, web, day=0, dry_run=True):
        return pipeline.run(self.cfg, self.storage, web, dry_run=dry_run, now=NOW + timedelta(days=day),
                            unknown_samples_dir=Path(self.tmp.name))

    def keys(self, jobs):
        return [j.native_id for j in jobs]

    def test_failed_detail_waits_three_runs_then_title_section(self):
        web = FailingDetailWeb()
        with self.assertLogs("jobagent.pipeline", level="WARNING"):
            first = self.run_once(web)
            second = self.run_once(web)
        self.assertIn(FAILING_ID, self.keys(first.pending))
        self.assertIn(FAILING_ID, self.keys(second.pending))
        # V rámci jednoho běhu se detail zkouší jen jednou.
        self.assertEqual(sum(FAILING_ID in u for u in web.requested), 2)
        with self.assertLogs("jobagent.pipeline", level="WARNING"):
            third = self.run_once(web)
        self.assertNotIn(FAILING_ID, self.keys(third.pending))
        job = next(j for j in third.title_jobs if j.native_id == FAILING_ID)
        self.assertIn("opakovaně", job.detail_note)
        self.assertGreater(job.title_score, 0)
        for report in (first, second, third):
            self.assertIsNone(report.balance_error())
        # Čtvrtý běh už detail nezkouší a nabídka zůstává v sekci podle názvu.
        fourth = self.run_once(web)
        self.assertEqual(sum(FAILING_ID in u for u in web.requested), 3)
        self.assertIn(FAILING_ID, self.keys(fourth.title_jobs))

    def test_title_section_in_email(self):
        with self.assertLogs("jobagent.pipeline", level="WARNING"):
            for _ in range(3):
                report = self.run_once(FailingDetailWeb())
        html = emailer.render(report, Templates(), now=NOW, threshold=self.cfg.summary.score_threshold)
        self.assertIn("Zajímavé podle názvu – bez popisu, otevři ručně (1)", html)
        self.assertIn("1 podle názvu", html)
        self.assertNotIn("čeká na detail", html)

    def test_min_score_ignores_jobs_without_text(self):
        self.cfg.scoring.min_score = 1000  # s textem neprojde nic
        web = make_web()
        web.pages["https://www.jobs.cz/rpd/"] = sample("syntetika_js_kostra_almacareer.html")
        report = self.run_once(web)
        self.assertEqual(report.new_jobs, [])
        self.assertEqual(len(report.title_jobs + report.no_text_other), 7)


class BacklogWarningTests(unittest.TestCase):
    """Varování jen když fronta (mimo první běh) roste BACKLOG_RUNS ostrých běhů po sobě."""

    class FakeStorage:
        def __init__(self, newest_first):
            self.history = [{"ceka_mimo_prvni_beh": v} for v in newest_first]

        def recent_live_summaries(self, exclude_run, limit):
            return self.history[:limit]

    def warn(self, newest_first, current):
        report = pipeline.RunReport(started=NOW, dry_run=True, pending_outside_first_run=current)
        pipeline._warn_about_backlog(self.FakeStorage(newest_first), 1, report)
        return report.warnings

    def test_growing(self):
        self.assertTrue(any("limit_detailu" in w and "1 → 3 → 5" in w for w in self.warn([3, 1], 5)))

    def test_not_growing(self):
        self.assertEqual(self.warn([3, 1], 3), [])
        self.assertEqual(self.warn([5, 3], 4), [])

    def test_too_little_history(self):
        self.assertEqual(self.warn([1], 5), [])


class FlexibleHoursTests(unittest.TestCase):
    """Pružná doba: skupina ze skutečného configu musí najít benefit jobs.cz a počítat se jednou."""

    WORDS = {"flexibilní začátek* | pružn* pracovní dob* | flexibilní pracovní doba": 12}

    def test_benefit_from_jobs_cz_detail(self):
        html = sample("jobs_cz_detail_zkraceny.html").replace(
            ">Dovolená 5 týdnů<", ">Flexibilní začátek/konec pracovní doby<")
        detail = JobsCzCollector().parse_detail(html, NOW.date())
        job = Job(portal="jobs.cz", native_id="1", url="u", title="IT konzultant", description=detail.description)
        total, reasons = score(job, Scoring(words=self.WORDS))
        # Ukázka má benefit i větu „Flexibilní pracovní doba a podpora…“.
        # Skupina se počítá jednou (dřív 12 + 12) a ukáže první nalezenou variantu.
        self.assertEqual(reasons, ["+12 flexibilní začátek* (popis)"])
        self.assertEqual(total, 12)

    def test_variants(self):
        def points(text):
            return score(Job(portal="t", native_id="1", url="u", title="x", description=text),
                         Scoring(words=self.WORDS))[0]
        self.assertEqual(points("Nabízíme pružnou pracovní dobu."), 12)
        self.assertEqual(points("Pružná pracovní doba"), 12)
        self.assertEqual(points("flexibilní pracovní doba"), 12)
        self.assertEqual(points("Pevná pracovní doba 6–14."), 0)
        self.assertEqual(points("Nepružná pracovní doba 6–14."), 0)  # bez úvodní * se zápor nechytá

    def test_title_points(self):
        job = Job(portal="t", native_id="1", url="u", title="Python vývojář – obchod",
                  description="python, python, python")
        cfg = Scoring(words={"python*": 10, "obchod*": -5}, title_words={"vývojář": 3}, title_multiplier=2)
        self.assertEqual(title_points(job, cfg), 10 * 2 - 5 * 2 + 3)


if __name__ == "__main__":
    unittest.main()
