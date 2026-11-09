"""Collector jobs.cz a obecná vrstva detailu nad uloženými ukázkami."""

import unittest
from datetime import date

from jobagent import collectors
from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.detail import extract_detail

from tests.helpers import sample

TODAY = date(2026, 11, 4)
LISTING_URL = "https://www.jobs.cz/prace/brno/?q%5B%5D=it&locality%5Bradius%5D=50"


class JobsCzListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = JobsCzCollector().parse_listing(sample("jobs_cz_vypis_zkraceny.html"), LISTING_URL, TODAY)
        cls.by_id = {j.native_id: j for j in cls.page.jobs}

    def test_all_cards_parsed(self):
        self.assertEqual(len(self.page.jobs), 8)

    def test_fields(self):
        job = self.by_id["2001491347"]
        self.assertEqual(job.title, "IT konzultant")
        self.assertEqual(job.company, "OptimiDoc s.r.o.")
        self.assertEqual(job.location, "Zlín – Louky + 1 další lokalita")
        self.assertEqual(job.salary_text, "40 000 – 60 000 Kč")
        self.assertEqual(job.url, "https://www.jobs.cz/rpd/2001491347/")  # bez sledovacích parametrů
        self.assertIn("Možnost občasné práce z domova", job.tags)
        self.assertNotIn(job.salary_text, job.tags)  # mzda není zároveň štítek
        self.assertEqual(job.posted, TODAY)

    def test_expiry_is_not_publication_date(self):
        # „Končí zítra“ je konec inzerátu, nesmí se z toho stát datum zveřejnění.
        self.assertIsNone(self.by_id["2001270137"].posted)

    def test_missing_salary_is_empty(self):
        self.assertEqual(self.by_id["2001480136"].salary_text, "")

    def test_next_page(self):
        self.assertIsNotNone(self.page.next_url)
        self.assertTrue(self.page.next_url.startswith("https://www.jobs.cz/prace/brno/"))
        self.assertIn("page=2", self.page.next_url)

    def test_registry_finds_collector_by_domain(self):
        self.assertIsInstance(collectors.for_url(LISTING_URL), JobsCzCollector)
        self.assertIsNone(collectors.for_url("https://www.example.com/prace"))


class DetailTests(unittest.TestCase):
    def test_jobs_cz_selectors(self):
        result = JobsCzCollector().parse_detail(sample("jobs_cz_detail_zkraceny.html"), TODAY)
        self.assertEqual(result.method, "selektory")
        self.assertIn("technickou podporou produktu", result.description)
        self.assertIn("Možnost občasné práce z domova", result.description)
        self.assertNotIn("Alma Career", result.description)  # modální okno a patička ne
        self.assertEqual(result.description.count("Mobilní telefon"), 1)  # zdvojený blok jen jednou
        self.assertIn("40 000", result.salary_text)

    def test_json_ld_preferred(self):
        result = extract_detail(sample("syntetika_json_ld.html"), None, TODAY)
        self.assertEqual(result.method, "json-ld")
        self.assertIn("automatizaci výrobních systémů", result.description)
        self.assertEqual(result.company, "Příklad a.s.")
        self.assertEqual(result.location, "Kopřivnice")
        self.assertEqual(result.posted, date(2026, 10, 30))
        self.assertIn("55000", result.salary_text)
        self.assertIn("70000", result.salary_text)

    def test_main_text_fallback(self):
        result = extract_detail(sample("syntetika_nezname_rozlozeni.html"), None, TODAY)
        self.assertEqual(result.method, "hlavni-text")
        self.assertIn("MES systém", result.description)
        self.assertIn("odpoledne je možné pracovat z domova", result.description)
        self.assertNotIn("cookies", result.description)
        self.assertNotIn("Všechna práva", result.description)
        self.assertNotIn("Produkty a služby", result.description)

    def test_selectors_that_do_not_match_fall_back(self):
        # Firemní stránka pod jobs.cz s jiným rozložením: selektory nic nenajdou.
        result = JobsCzCollector().parse_detail(sample("syntetika_nezname_rozlozeni.html"), TODAY)
        self.assertEqual(result.method, "hlavni-text")
        self.assertIn("MES systém", result.description)


if __name__ == "__main__":
    unittest.main()
