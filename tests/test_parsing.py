"""Mzda, datum, normalizace textu a deduplikační klíč."""

import unittest
from datetime import date

from jobagent.dates import parse_czech_date
from jobagent.dedup import dedup_key
from jobagent.salary import monthly_upper, parse_salary
from jobagent.text import contains_keyword, fold

TODAY = date(2026, 11, 4)


class SalaryTests(unittest.TestCase):
    def upper(self, text, hours=173):
        return monthly_upper(parse_salary(text), hours)

    def test_range_uses_upper_bound(self):
        # Text přesně jak ho vrací jobs.cz, včetně NBSP a zero-width joinerů.
        self.assertEqual(self.upper("40 000 ‍–‍ 60 000 Kč"), 60000)

    def test_thousands_abbreviation(self):
        self.assertEqual(self.upper("35–40 tis. Kč"), 40000)
        self.assertEqual(self.upper("45k Kč/měsíc"), 45000)

    def test_hourly_converted(self):
        self.assertEqual(self.upper("180 Kč/hod"), 180 * 173)
        self.assertEqual(self.upper("200 Kč za hodinu"), 200 * 173)

    def test_yearly_converted(self):
        self.assertEqual(self.upper("600 000 Kč ročně"), 50000)

    def test_uncertain_cases_return_none(self):
        self.assertIsNone(self.upper(""))
        self.assertIsNone(self.upper("Dohodou"))
        self.assertIsNone(self.upper("od 35 000 Kč"))  # horní hranice neznámá
        self.assertIsNone(self.upper("2 500 – 3 000 EUR"))  # cizí měna
        self.assertIsNone(self.upper("250"))  # malé číslo bez jednotky – hodinovka? chyba?

    def test_upto(self):
        self.assertEqual(self.upper("až 60 000 Kč"), 60000)


class DateTests(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(parse_czech_date("Přidáno dnes", TODAY), TODAY)
        self.assertEqual(parse_czech_date("Aktualizováno před 3 hodinami", TODAY), TODAY)
        self.assertEqual(parse_czech_date("Aktualizováno včera", TODAY), date(2026, 11, 3))
        self.assertEqual(parse_czech_date("Přidáno před 5 dny", TODAY), date(2026, 10, 30))
        self.assertEqual(parse_czech_date("před týdnem", TODAY), date(2026, 10, 28))
        self.assertEqual(parse_czech_date("před 2 týdny", TODAY), date(2026, 10, 21))

    def test_absolute(self):
        self.assertEqual(parse_czech_date("2026-10-12T08:00:00+02:00", TODAY), date(2026, 10, 12))
        self.assertEqual(parse_czech_date("Zveřejněno 12. 10. 2026", TODAY), date(2026, 10, 12))
        self.assertEqual(parse_czech_date("12. října 2026", TODAY), date(2026, 10, 12))

    def test_month_without_year_in_past(self):
        self.assertEqual(parse_czech_date("28. prosince", date(2027, 1, 5)), date(2026, 12, 28))

    def test_unknown(self):
        self.assertIsNone(parse_czech_date("Příležitost dne", TODAY))
        self.assertIsNone(parse_czech_date("", TODAY))


class KeywordTests(unittest.TestCase):
    def test_diacritics_and_case(self):
        self.assertTrue(contains_keyword(fold("Práce na SMĚNY"), "směny"))

    def test_whole_word(self):
        self.assertFalse(contains_keyword(fold("digitalizace dokumentů"), "it"))
        self.assertTrue(contains_keyword(fold("Správce IT"), "it"))

    def test_wildcard_suffix(self):
        text = fold("Pracuje se ve dvousměnném provozu, příplatky za směnách")
        self.assertTrue(contains_keyword(text, "směn*"))
        self.assertFalse(contains_keyword(fold("výměna oleje"), "směn*"))

    def test_symbols(self):
        self.assertTrue(contains_keyword(fold("Vývojář C# / ASP.NET"), "c#"))
        self.assertTrue(contains_keyword(fold("Vývojář C# / ASP.NET"), ".net"))
        self.assertFalse(contains_keyword(fold("Vývojář C++"), "c#"))


class DedupTests(unittest.TestCase):
    def test_same_ad_different_formatting(self):
        # jobs.cz píše „Zlín – Lužkovice“, prace.cz „Zlín-Lužkovice“: obec je stejná.
        a = dedup_key("Vývojář/ka webových aplikací", "TESCOMA s.r.o.", "Zlín – Lužkovice")
        b = dedup_key("VÝVOJÁŘ webových aplikací (m/ž)", "Tescoma, s. r. o.", "Zlín-Lužkovice")
        self.assertEqual(a, b)

    def test_without_company_or_town_no_key(self):
        # Při nejistotě klíč neexistuje → neslučuje se.
        self.assertEqual(dedup_key("Účetní", "", "Ostrava"), "")
        for unsure in ("", "Česko", "Moravskoslezský", "Moravskoslezský kraj", "Ostrava, Opava"):
            self.assertEqual(dedup_key("Účetní", "Firma s.r.o.", unsure), "", unsure)

    def test_town(self):
        from jobagent.dedup import town
        self.assertEqual(town("Ostrava – Moravská Ostrava + 1 další lokalita"), "ostrava")
        self.assertEqual(town("Šenov u Nového Jičína"), "senov u noveho jicina")


if __name__ == "__main__":
    unittest.main()
