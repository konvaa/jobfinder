"""Testy na připomínky z review fáze 1 (počty, robots.txt, první běh, místo nálezu…)."""

import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from pathlib import Path

from jobagent import emailer, pipeline
from jobagent.config import Scoring, load_config
from jobagent.detail import extract_detail
from jobagent.fetch import Fetcher, FetchError, Response, RobotsUnavailable
from jobagent.models import Job
from jobagent.scoring import score
from jobagent.storage import Storage

from tests.helpers import SAMPLES, FakeFetcher, sample
from tests.test_pipeline import LISTING_URL, NOW, make_web

ROOT = SAMPLES.parent


class BasePipelineTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = self.cfg.searches[:1]
        self.cfg.searches[0].url = LISTING_URL
        self.storage = Storage(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.storage.close)

    def run_once(self, web=None, dry_run=True, now=NOW):
        return pipeline.run(self.cfg, self.storage, web or make_web(), dry_run=dry_run, now=now,
                            unknown_samples_dir=Path(self.tmp.name))


class AccountingTests(BasePipelineTest):
    """Bod 5: stavový řádek musí sedět beze zbytku."""

    def test_repeated_ids_across_searches_are_counted(self):
        second = type(self.cfg.searches[0])(name="Druhé", url=LISTING_URL + "&jine=1")
        self.cfg.searches.append(second)
        report = self.run_once()
        self.assertEqual(report.found_total, 16)
        self.assertEqual(report.unique, 8)
        self.assertEqual(report.repeated, 8)
        self.assertIsNone(report.balance_error())

    def test_balance_with_sent_and_known_rejections(self):
        self.cfg.filters.reject_words = ["logistik*"]
        first = self.run_once()
        self.storage.mark_sent(first.new_jobs[:3], first.rejected, NOW)
        second = self.run_once()
        self.assertEqual(second.already_sent, 3)
        self.assertEqual(second.rejected_known, 1)
        self.assertIsNone(second.balance_error())

    def test_status_line_adds_up(self):
        second = type(self.cfg.searches[0])(name="Druhé", url=LISTING_URL + "&jine=1")
        self.cfg.searches.append(second)
        report = self.run_once()
        html = emailer.render(report, ROOT / "templates", now=NOW)
        self.assertIn("nalezeno 16 = 8 unikátních + 8 opakovaných", html)


class FirstRunTests(BasePipelineTest):
    """Bod 9: delší limit stáří, dokud hledání nemělo ostrý běh (dry-run se nepočítá)."""

    def old_listing(self):
        # Jeden inzerát „Přidáno před 20 dny“: pod 30 (první běh), nad 7 (běžně).
        return sample("jobs_cz_vypis_zkraceny.html").replace("Přidáno dnes", "Přidáno před 20 dny", 1)

    def titles(self, report):
        return {j.title for j in report.new_jobs}

    def test_first_run_window_until_live_run(self):
        old = "Projektový Manager/ka logistiky"
        self.assertIn(old, self.titles(self.run_once(make_web(self.old_listing()))))  # dry-run: první běh
        self.assertIn(old, self.titles(self.run_once(make_web(self.old_listing()))))  # další dry-run: pořád první
        live = self.run_once(make_web(self.old_listing()), dry_run=False)  # ostrý běh, e-mail zatím neodešel
        self.assertIn(old, self.titles(self.run_once(make_web(self.old_listing()))), "neodeslaný běh se nepočítá")
        self.deliver(live)  # e-mail odešel → teprve teď je to ostrý běh
        later = self.run_once(make_web(self.old_listing()))
        self.assertNotIn(old, self.titles(later))
        self.assertTrue(any("20 dní, limit 7" in (j.reject_reason or "") for j in later.rejected))

    def deliver(self, report):
        """Jako cli po úspěšném odeslání, jen bez označení inzerátů (test se týká okna
        prvního běhu, ne odeslaných inzerátů): běh se zapíše jako doručený."""
        self.storage.mark_sent([], [], datetime(2026, 11, 4, 10), run_id=report.run_id)

    def test_new_search_gets_its_own_first_run(self):
        self.deliver(self.run_once(make_web(self.old_listing()), dry_run=False))
        new_search = type(self.cfg.searches[0])(name="Nové", url=LISTING_URL + "&nove=1")
        self.cfg.searches.append(new_search)
        report = self.run_once(make_web(self.old_listing()))
        self.assertIn("Projektový Manager/ka logistiky", self.titles(report))


class RobotsTests(unittest.TestCase):
    """Bod 2: RFC 9309 – 4xx bez omezení, 5xx/síť = web v tomto běhu přeskočit."""

    class ScriptedFetcher(Fetcher):
        def __init__(self, robots):
            super().__init__(sleep=lambda s: None)
            self.robots = robots  # funkce, která vrátí Response nebo vyhodí výjimku

        def _request(self, url):
            if url.endswith("/robots.txt"):
                return self.robots(url)
            return Response(url=url, text="<html>ok</html>", status=200)

    @staticmethod
    def http_error(code):
        def raise_(url):
            raise urllib.error.HTTPError(url, code, "x", {}, None)
        return raise_

    def test_404_means_allow_all(self):
        f = self.ScriptedFetcher(self.http_error(404))
        with self.assertLogs("jobagent.fetch", level="DEBUG") as logs:  # 404 → jen DEBUG
            self.assertEqual(f.get("https://a.cz/prace/").text, "<html>ok</html>")
        self.assertEqual([r.levelname for r in logs.records if "robots.txt" in r.getMessage()], ["DEBUG"])

    def test_5xx_skips_site(self):
        f = self.ScriptedFetcher(self.http_error(503))
        # assertLogs: ověří, že se problém zalogoval (oba pokusy), a zároveň
        # log pohltí, takže výstup unittestu zůstane čistý.
        with self.assertLogs("jobagent.fetch", level="WARNING") as logs, self.assertRaises(RobotsUnavailable):
            f.get("https://a.cz/prace/")
        self.assertEqual(len(logs.records), 2)
        self.assertIn("503", logs.output[0])

    def test_network_error_skips_site(self):
        def down(url):
            raise urllib.error.URLError("timeout")
        with self.assertLogs("jobagent.fetch", level="WARNING") as logs, self.assertRaises(RobotsUnavailable):
            self.ScriptedFetcher(down).get("https://a.cz/prace/")
        self.assertIn("timeout", logs.output[-1])

    def test_rules_are_respected(self):
        f = self.ScriptedFetcher(lambda u: Response(u, "User-agent: *\nDisallow: /api/\n", 200))
        self.assertEqual(f.get("https://a.cz/prace/").status, 200)
        with self.assertRaises(FetchError):
            f.get("https://a.cz/api/x")

    def test_pipeline_reports_skipped(self):
        class Down(FakeFetcher):
            def get(self, url):
                raise RobotsUnavailable("a.cz: robots.txt teď nejde načíst")
        cfg = load_config(ROOT / "config.example.toml")
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        report = pipeline.run(cfg, storage, Down({}), dry_run=True, now=NOW)
        self.assertEqual(report.skipped, len(cfg.searches))
        self.assertEqual(report.errors, 0)
        html = emailer.render(report, ROOT / "templates", now=NOW)
        self.assertIn(f"přeskočeno {len(cfg.searches)}", html)
        self.assertIn("PŘESKOČENO", html)


class ScoringPlaceTests(unittest.TestCase):
    """Body 7 a 8: místo nálezu ve zdůvodnění, slova jen v názvu, záporné body."""

    def job(self, **kw):
        base = dict(portal="t", native_id="1", url="u", title="IT technik", tags=["Možnost práce z domova"],
                    description="Práce na směny, skriptování v Pythonu výhodou.")
        base.update(kw)
        return Job(**base)

    def test_places_and_title_words(self):
        cfg = Scoring(words={"python*": 10, "z domova": 4, "směn*": -30, "technik": 3},
                      title_words={"it": 10}, title_multiplier=2)
        total, reasons = score(self.job(), cfg)
        self.assertIn("+10 python* (popis)", reasons)
        self.assertIn("+4 z domova (štítek)", reasons)
        self.assertIn("-30 směn* (popis)", reasons)
        self.assertIn("+6 technik (název)", reasons)
        self.assertIn("+10 it (název)", reasons)
        self.assertEqual(total, 10 + 4 - 30 + 6 + 10)

    def test_title_word_ignored_in_description(self):
        cfg = Scoring(title_words={"it": 10})
        total, _ = score(self.job(title="Účetní", description="IT oddělení vám nastaví notebook."), cfg)
        self.assertEqual(total, 0)

    def test_reject_title_words(self):
        from jobagent.config import Filters
        from jobagent.filters import check
        f = Filters(reject_title_words=["obchodník"], max_age_days=None)
        self.assertIsNotNone(check(self.job(title="Obchodník s IT"), f, date(2026, 11, 4)))
        self.assertIsNone(check(self.job(description="Spolupráce s obchodníkem."), f, date(2026, 11, 4)))


class DetailPriorityTests(unittest.TestCase):
    """Bod 6: při shodě skóre rozhoduje novější datum, pak uvedená mzda – ne pořadí ve výpisu."""

    def test_tiebreak(self):
        mk = lambda i, s, d, sal="": Job(portal="t", native_id=i, url="u", title=i, score=s, posted=d, salary_text=sal)
        jobs = [mk("stary", 8, date(2026, 10, 1)), mk("bez-data", 8, None), mk("novy", 8, date(2026, 11, 4)),
                mk("novy-s-mzdou", 8, date(2026, 11, 4), "50 000 Kč"), mk("vysoke", 20, date(2026, 9, 1))]
        jobs.sort(key=pipeline._detail_priority, reverse=True)
        self.assertEqual([j.native_id for j in jobs], ["vysoke", "novy-s-mzdou", "novy", "stary", "bez-data"])


class JsSkeletonTests(BasePipelineTest):
    """Bod 11: kariérní weby *.jobs.cz/detail-pozice mají text jen v JavaScriptu."""

    def test_skeleton_is_no_text(self):
        result = extract_detail(sample("syntetika_js_kostra_almacareer.html"), None, NOW.date())
        self.assertEqual(result.method, "bez-textu")
        self.assertEqual(result.description, "")

    def test_footer_div_is_not_main_text(self):
        result = extract_detail(sample("syntetika_nezname_rozlozeni.html"), None, NOW.date())
        self.assertNotIn("Všechna práva", result.description)

    def test_pipeline_notes_and_does_not_refetch(self):
        web = make_web()
        web.pages["https://www.jobs.cz/rpd/"] = sample("syntetika_js_kostra_almacareer.html")
        report = self.run_once(web)
        self.assertEqual(report.details_no_text, 7)  # všechny kromě IT konzultanta
        no_text = report.title_jobs + report.no_text_other
        self.assertEqual(len(no_text), 7)
        self.assertTrue(all("JavaScript" in j.detail_note for j in no_text))
        self.assertEqual([j.title for j in report.new_jobs], ["IT konzultant"])
        web2 = make_web()
        again = self.run_once(web2)
        self.assertFalse([u for u in web2.requested if "/rpd/" in u])
        self.assertEqual(len(again.title_jobs + again.no_text_other), 7)


class ThresholdEmailTests(BasePipelineTest):
    """Bod 10: hranice skóre – hlavní tabulka a sbalená sekce Ostatní."""

    def test_split(self):
        report = self.run_once()
        threshold = max(j.score for j in report.new_jobs)  # nad hranicí jen ten nejlepší
        main, others = emailer.split_by_threshold(report.new_jobs, threshold)
        self.assertTrue(main and others)
        html = emailer.render(report, ROOT / "templates", now=NOW, threshold=threshold)
        self.assertIn(f"Ostatní nabídky ({len(others)})", html)
        # Tělo e-mailu: obyčejný nadpis, ne <details> (webmail Seznamu ho smaže i s obsahem).
        self.assertNotIn("<details", html)
        collapsed = emailer.render(report, ROOT / "templates", now=NOW, threshold=threshold, collapsible=True)
        self.assertIn("<details", collapsed)  # příloha pro prohlížeč
        self.assertIn(f"({len(main)} v hlavní tabulce, {len(others)} ostatních)", html)
        self.assertEqual(len(main) + len(others), len(report.new_jobs))

    def test_no_threshold_no_section(self):
        html = emailer.render(self.run_once(), ROOT / "templates", now=NOW, threshold=None)
        self.assertNotIn("Ostatní nabídky", html)


class FollowUpTests(BasePipelineTest):
    """Doplňky po review: úvodní hvězdička a počet „jen podle výpisu“."""

    def test_leading_wildcard_matches_compounds(self):
        from jobagent.text import contains_keyword, fold
        self.assertTrue(contains_keyword(fold("Práce ve dvousměnném provozu"), "*směn*"))
        self.assertTrue(contains_keyword(fold("Práce na směny"), "*směn*"))
        self.assertTrue(contains_keyword(fold("třísměnný"), "*směnný"))
        self.assertFalse(contains_keyword(fold("třísměnný provoz"), "*směn"))  # bez koncové * musí slovo skončit

    def test_short_leading_wildcard_is_ignored_with_warning(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        tmp.write('[[hledani]]\nurl = "https://www.jobs.cz/prace/"\n'
                  '[filtry]\nvyradit = ["*it*", "*směn*"]\nvyradit_v_nazvu = ["*obc"]\n'
                  '[hodnoceni.slova]\n"*ai*" = 5\n"*automat*" = 5\n'
                  '[email]\nprijemci = ["a@b.cz"]\n')
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        cfg = load_config(Path(tmp.name))
        self.assertEqual(cfg.filters.reject_words, ["*směn*"])
        self.assertEqual(cfg.filters.reject_title_words, [])
        self.assertEqual(cfg.scoring.words, {"*automat*": 5})
        self.assertEqual(cfg.email.recipients, ["a@b.cz"])
        self.assertEqual(sum("se ignoruje" in w for w in cfg.warnings), 3)

    def test_listing_only_count_in_status(self):
        web = make_web()
        web.pages["https://www.jobs.cz/rpd/"] = sample("syntetika_js_kostra_almacareer.html")
        report = self.run_once(web)
        self.assertEqual(report.listing_only, 7)
        html = emailer.render(report, ROOT / "templates", now=NOW)
        self.assertIn("z nich 7 jen podle výpisu", html)
        self.assertIsNone(report.balance_error())


class ConfigMigrationTests(unittest.TestCase):
    def test_old_penalty_keys_explained(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        tmp.write('[[hledani]]\nurl = "https://www.jobs.cz/prace/"\n[filtry]\npenalizovat = ["směn*"]\npenalizace = 30\n')
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        cfg = load_config(Path(tmp.name))
        self.assertTrue(any("filtry.penalizovat už neexistuje" in w for w in cfg.warnings))
        self.assertFalse(any("překlep" in w for w in cfg.warnings))

    def test_example_sections(self):
        cfg = load_config(ROOT / "config.example.toml")
        self.assertEqual(cfg.scoring.title_words, {"it": 10})
        self.assertEqual(cfg.scoring.words["směn*"], -30)
        self.assertEqual(cfg.filters.max_age_first_run, 30)
        self.assertEqual(cfg.summary.score_threshold, 15)


if __name__ == "__main__":
    unittest.main()
