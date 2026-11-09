"""Hranice textu detailu: šablonové bloky agentur, stránky bez inzerátu, příkaz vysvetli, migrace v4."""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.config import Scoring, load_config
from jobagent.cleaning import clean_description
from jobagent.detail import extract_detail, title_present
from jobagent.explain import explain
from jobagent.models import Job
from jobagent.scoring import score
from jobagent.storage import Storage

from tests.helpers import SAMPLES, sample

ROOT = SAMPLES.parent
TODAY = date(2026, 11, 5)
STAVBYVEDOUCI = "Hlavní stavbyvedoucí - pozemní stavby (70-90.000 Kč)"
URL = "https://www.jobs.cz/rpd/2001494352/"


class AgencyBoilerplateTests(unittest.TestCase):
    """Zadání: stavbyvedoucí od Jobs Contact Personal nesmí dostat body za C#/.NET/SQL."""

    def setUp(self):
        self.raw = JobsCzCollector().parse_detail(
            sample("jobs_cz_detail_stavbyvedouci_zkraceny.html"), TODAY, STAVBYVEDOUCI).description
        self.job = Job(portal="jobs.cz", native_id="2001494352", url=URL, title=STAVBYVEDOUCI, description=self.raw)

    def test_detail_keeps_raw_text(self):
        # Do DB jde surový text – čistí se až při použití (Job.text).
        self.assertIn("c#, asp.net", self.raw)

    def test_keyword_block_removed(self):
        text = self.job.text.lower()
        for word in ("c#", "asp.net", "ms sql", "winforms"):
            self.assertNotIn(word, text)
        self.assertIn("řízení projektu od přípravné fáze", text)  # samotný inzerát zůstal
        self.assertIn("pružnou pracovní dobu", text)

    def test_removed_lines_are_reported(self):
        removed = clean_description(self.raw)[1]
        kinds = sorted(line.split("]")[0] + "]" for line in removed)
        self.assertEqual(kinds, ["[klíčová slova agentury]", "[odkaz na další nabídky]", "[osobní údaje / GDPR]"])

    def test_no_points_for_foreign_keywords(self):
        total, reasons = score(self.job, Scoring(words={"c#": 12, ".net": 8, "sql": 5}))
        self.assertEqual((total, reasons), (0, []))

    def test_text_cache_follows_description(self):
        job = Job(portal="t", native_id="1", url="u", title="x", description="A\nTato nabídka je vhodná pro pozice: c#")
        self.assertEqual(job.text, "A")
        job.description = "B"
        self.assertEqual(job.text, "B")

    def test_clean_description_variants(self):
        text = ("Popis práce.\nTato nabídka je vhodná i pro pozice: účetní, ekonom.\n"
                "Odesláním souhlasíte se zpracováním osobních údajů dle GDPR.\nNabízíme stravenky.")
        cleaned, removed = clean_description(text)
        self.assertEqual(cleaned, "Popis práce.\nNabízíme stravenky.")
        self.assertEqual(len(removed), 2)

    def test_gdpr_requirement_is_kept(self):
        # Reálný řádek z DB (Java Backend Developer): znalost GDPR je požadavek, ne souhlas.
        text = "Knowledge of OWASP secure coding practices, GDPR and regulated environments"
        self.assertEqual(clean_description(text), (text, []))


class StoredRawTextTests(unittest.TestCase):
    """Zadání: uložený NEočištěný text v DB → bodování → bez bodů za řádky agentury.
    Přesně případ stavbyvedoucího: detail stažený starší verzí programu."""

    def test_pipeline_cleans_text_from_db(self):
        from tests.test_pipeline import LISTING_URL, NOW, make_web
        from jobagent import pipeline

        cfg = load_config(ROOT / "config.example.toml")
        cfg.searches = cfg.searches[:1]
        cfg.searches[0].url = LISTING_URL
        cfg.scoring.words.update({"c#": 12, "asp.net": 8, "ms sql": 5})
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline.run(cfg, storage, make_web(), dry_run=True, now=NOW, unknown_samples_dir=Path(tmp))
            # Do DB „starou verzí“ uložíme surový text včetně šablony agentury.
            raw = ("Hledáme IT konzultanta pro podporu produktu.\n"
                   "Tato pracovní nabídka je vhodná pro pozice: programátor, developer, c#, asp.net, ms sql.\n"
                   "Bude Vaše osobní údaje zpracovávat v souladu se zákonem.")
            storage.conn.execute("UPDATE jobs SET description=? WHERE native_id='2001491347'", (raw,))
            report = pipeline.run(cfg, storage, make_web(), dry_run=True, now=NOW, unknown_samples_dir=Path(tmp))
        job = next(j for j in report.new_jobs if j.native_id == "2001491347")
        self.assertEqual(job.description, raw)  # v paměti i v DB zůstává surový
        self.assertFalse([r for r in job.score_reasons if any(w in r for w in ("c#", "asp.net", "ms sql"))],
                         job.score_reasons)


class PageWithoutAdTests(unittest.TestCase):
    """Kariérní web s JS: obecný text o firmě není inzerát (případ CSG)."""

    def test_missing_title_means_no_text(self):
        html = sample("syntetika_karierni_web_bez_inzeratu.html")
        self.assertEqual(extract_detail(html, None, TODAY, "Financial Reporting Specialist").method, "bez-textu")
        # Bez názvu by obecná vrstva text vzala – proto pipeline název předává.
        self.assertEqual(extract_detail(html, None, TODAY).method, "hlavni-text")

    def test_title_present_tolerates_variants(self):
        self.assertTrue(title_present("Účetní/ý – mzdová", "Hledáme účetní pro mzdovou agendu: mzdová účetní…"))
        self.assertTrue(title_present("IT technik (m/ž)", "Pozice: IT technik pro správu sítě"))
        self.assertFalse(title_present("Financial Reporting Specialist", "Naše vize a mise, kariéra u nás"))


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.scoring.words["c#"] = 12
        self.html = sample("jobs_cz_detail_stavbyvedouci_zkraceny.html")

    def test_with_listing(self):
        listing = Job(portal="jobs.cz", native_id="2001494352", url=URL, title=STAVBYVEDOUCI,
                      company="Jobs Contact Personal, s.r.o.", location="Vsetín", salary_text="70 000 – 90 000 Kč")
        out = explain(self.html, URL, self.cfg, TODAY, listing)
        self.assertIn("Zdroj textu: CSS selektory portálu", out)
        self.assertIn("Odstraněno z textu (3 řádků", out)
        self.assertIn("data z výpisu: ano", out)
        self.assertIn("»Flexibilní začátek«", out)  # úryvek s diakritikou
        self.assertNotIn("(popis)  …vývojář se znalostí technologií »c#«", out)
        self.assertIn("Sekce e-mailu: Ostatní (skóre pod hranicí 15)", out)  # po vyčištění jen 10 bodů

    def test_without_listing_takes_title_from_page(self):
        out = explain(self.html, URL, self.cfg, TODAY, None)
        self.assertIn(f"Název:  {STAVBYVEDOUCI}", out)
        self.assertIn("data z výpisu: NE", out)

    def test_page_without_text(self):
        out = explain(sample("syntetika_js_kostra_almacareer.html"), "https://firma.jobs.cz/detail-pozice?id=1",
                      self.cfg, TODAY, Job(portal="jobs.cz", native_id="1", url="u", title="Python vývojář"))
        self.assertIn("Zdroj textu: JEN VÝPIS", out)
        self.assertIn("Sekce e-mailu: Zajímavé podle názvu", out)


class ListingLookupTests(unittest.TestCase):
    """vysvetli: dohledání v DB podle ID portálu, ne podle celé URL."""

    def test_jobs_cz_id_from_all_url_forms(self):
        c = JobsCzCollector()
        for url in (
            "https://www.jobs.cz/rpd/2001238232/?searchId=abc&rps=233",
            "https://www.jobs.cz/fp/huisman-czech-republic-s-r-o-328149/2001238232/?searchId=x",
            "https://huisman.jobs.cz/pd/2001238232?originJobAdUuid=u&rps=2233",
            "https://acjobs.jobs.cz/detail-pozice?r=detail&id=2001238232&rps=0&impressionId=",
        ):
            self.assertEqual(c.native_id_from_url(url), "2001238232", url)
        self.assertIsNone(c.native_id_from_url("https://www.jobs.cz/prace/?q%5B%5D=it"))

    def test_find_listing_by_id(self):
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        job = Job(portal="jobs.cz", native_id="2001238232", url="https://www.jobs.cz/rpd/2001238232/",
                  title="Finanční účetní senior", tags=["Odpověď do 2 týdnů"])
        storage.merge_seen(job, "", __import__("datetime").datetime(2026, 11, 5))
        fp = "https://www.jobs.cz/fp/huisman-czech-republic-s-r-o-328149/2001238232/?searchId=x"
        c = JobsCzCollector()
        found = storage.find_listing(c.name, c.native_id_from_url(fp), c.canonical_url(fp))
        self.assertEqual(found.title, "Finanční účetní senior")
        self.assertEqual(found.tags, ["Odpověď do 2 týdnů"])


class MigrationV4Tests(unittest.TestCase):
    def test_old_details_are_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite"
            Storage(path).close()
            conn = sqlite3.connect(path)
            conn.execute("""INSERT INTO jobs (key, portal, native_id, url, title, first_seen, last_seen,
                            description, detail_fetched, detail_status)
                            VALUES ('a', 'p', '1', 'u', 't', 'x', 'x', 'patička', 1, NULL),
                                   ('b', 'p', '2', 'u', 't', 'x', 'x', 'dobrý text', 1, 'selektory')""")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
            conn.close()
            storage = Storage(path)
            rows = dict(storage.conn.execute("SELECT key, detail_fetched FROM jobs").fetchall())
            storage.close()
            self.assertEqual(rows, {"a": 0, "b": 1})


if __name__ == "__main__":
    unittest.main()
