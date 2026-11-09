"""Skupiny synonym „a | b | c“ ve všech sekcích slov."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from jobagent.config import Filters, Scoring, load_config
from jobagent.filters import check
from jobagent.models import Job
from jobagent.scoring import score, title_points
from jobagent.text import contains_keyword, find_variant, fold, variants

HO = "home office | práce převážně z domova | hybridní* | remote | práce na dálku"


def job(title="Správce IT", tags=(), description=""):
    return Job(portal="t", native_id="1", url="u", title=title, tags=list(tags), description=description)


class VariantTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(variants(" a |b*|  c c "), ["a", "b*", "c c"])
        self.assertEqual(variants("python*"), ["python*"])

    def test_find_variant(self):
        text = fold("Nabízíme hybridní režim a občas remote")
        self.assertEqual(find_variant(text, HO), "hybridní*")  # první varianta v pořadí ze zápisu, která sedí
        self.assertIsNone(find_variant(fold("Práce v kanceláři"), HO))
        self.assertTrue(contains_keyword(text, HO))


class ScoringGroupTests(unittest.TestCase):
    def test_counted_once(self):
        j = job(description="Home office 2 dny, hybridní model, částečně remote.")
        total, reasons = score(j, Scoring(words={HO: 10}))
        self.assertEqual(total, 10)
        self.assertEqual(reasons, ["+10 home office (popis)"])

    def test_best_place_wins_with_multiplier(self):
        j = job(title="Python vývojář (remote)", tags=["Práce převážně z domova"], description="hybridní")
        total, reasons = score(j, Scoring(words={HO: 10}, title_multiplier=2))
        self.assertEqual(reasons, ["+20 remote (název)"])
        self.assertEqual(total, 20)

    def test_tag_before_description(self):
        j = job(tags=["Práce převážně z domova"], description="home office")
        self.assertEqual(score(j, Scoring(words={HO: 10}))[1], ["+10 práce převážně z domova (štítek)"])

    def test_negative_group(self):
        cfg = Scoring(words={"směn* | turnus* | noční": -30})
        total, reasons = score(job(description="Nepřetržitý provoz, turnusy i noční směny."), cfg)
        self.assertEqual(total, -30)
        self.assertEqual(len(reasons), 1)

    def test_title_words_group(self):
        cfg = Scoring(title_words={"it | ict": 10})
        self.assertEqual(score(job(title="Technik ICT"), cfg)[1], ["+10 ict (název)"])
        self.assertEqual(score(job(title="Účetní", description="IT oddělení"), cfg)[0], 0)
        self.assertEqual(title_points(job(title="IT / ICT správce"), cfg), 10)


class LongestMatchTests(unittest.TestCase):
    """Delší shoda má přednost – pro každý výskyt zvlášť."""

    HO_CLEAN = "home office | práce převážně z domova | z domova | hybridní* | remote | práce na dálku"
    CFG = Scoring(words={HO_CLEAN: 10, "občasné práce z domova": 2})

    def test_weak_tag_and_strong_sentence(self):
        # Zadání: štítek „Možnost občasné práce z domova“ + věta „3 dny v týdnu z domova“ → +2 a +10.
        j = job(tags=["Možnost občasné práce z domova"], description="Nabízíme 3 dny v týdnu z domova.")
        total, reasons = score(j, self.CFG)
        self.assertEqual(total, 12)
        self.assertIn("+2 občasné práce z domova (štítek)", reasons)
        self.assertIn("+10 z domova (popis)", reasons)

    def test_weak_only(self):
        # Štítek i benefit v popisu jsou obojí „občasné“ → jen +2.
        j = job(tags=["Možnost občasné práce z domova"], description="Benefity: Možnost občasné práce z domova")
        self.assertEqual(score(j, self.CFG), (2, ["+2 občasné práce z domova (štítek)"]))

    def test_strong_tag(self):
        j = job(tags=["Práce převážně z domova"])
        self.assertEqual(score(j, self.CFG), (10, ["+10 práce převážně z domova (štítek)"]))

    def test_python_example_from_config(self):
        cfg = Scoring(words={"python vývojář*": 20, "python*": 15})
        only_phrase = job(title="Programátor", description="Hledáme Python vývojáře do týmu.")
        self.assertEqual(score(only_phrase, cfg)[0], 20)  # ne 35
        elsewhere = job(title="Programátor", description="Hledáme Python vývojáře. Testy píšeme v Pythonu.")
        self.assertEqual(score(elsewhere, cfg)[0], 35)  # „Pythonu“ je mimo delší shodu → platí

    def test_partial_overlap_counts_both(self):
        # Shody se jen překrývají, kratší neleží celá uvnitř delší → obě platí.
        cfg = Scoring(words={"správa sítě": 6, "sítě a serverů": 4})
        self.assertEqual(score(job(description="Správa sítě a serverů."), cfg)[0], 10)

    def test_title_words_compete_in_title(self):
        cfg = Scoring(words={"it support": 5}, title_words={"it": 10}, title_multiplier=2)
        self.assertEqual(score(job(title="IT support"), cfg), (10, ["+10 it support (název)"]))
        self.assertEqual(title_points(job(title="IT support"), cfg), 10)
        self.assertEqual(title_points(job(title="IT technik"), cfg), 10)

    def test_filters_are_not_suppressed(self):
        f = Filters(reject_words=["směn*"], max_age_days=None)
        self.assertIsNotNone(check(job(description="Nepracujeme na směny."), f, date(2026, 11, 5)))


class FilterGroupTests(unittest.TestCase):
    def test_reject_group_shows_variant(self):
        f = Filters(reject_words=["3směnný | třísměnný | nepřetržit* provoz*"], max_age_days=None)
        reason = check(job(description="Práce v nepřetržitém provozu."), f, date(2026, 11, 5))
        self.assertEqual(reason, "obsahuje „nepřetržit* provoz*“")

    def test_reject_title_group(self):
        f = Filters(reject_title_words=["obchodník | obchodnice"], max_age_days=None)
        self.assertEqual(check(job(title="Obchodnice IT"), f, date(2026, 11, 5)), "název obsahuje „obchodnice“")


class ConfigGroupTests(unittest.TestCase):
    def load(self, body):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        tmp.write('[[hledani]]\nurl = "https://www.jobs.cz/prace/"\n' + body)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return load_config(Path(tmp.name))

    def test_wildcard_rules_per_variant(self):
        cfg = self.load('[filtry]\nvyradit = ["*it* | *směn*"]\n'
                        '[hodnoceni.slova]\n"*ai* | umělá inteligence" = 8\n"*ai*|*ml*" = 3\n')
        self.assertEqual(cfg.filters.reject_words, ["*směn*"])  # neplatná varianta pryč, zbytek platí
        self.assertEqual(cfg.scoring.words, {"umělá inteligence": 8})  # skupina bez platné varianty zmizí
        self.assertEqual(sum("se ignoruje" in w for w in cfg.warnings), 4)

    def test_example_config_groups(self):
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
        self.assertEqual(cfg.warnings, [])
        # Ukázkový config musí dávat slabému štítku málo a větě v textu hodně.
        weak = job(title="Účetní", tags=["Možnost občasné práce z domova"])
        strong = job(title="Účetní", description="Nabízíme 3 dny v týdnu z domova.")
        self.assertEqual(score(weak, cfg.scoring)[0], 2)
        self.assertEqual(score(strong, cfg.scoring)[0], 8)


if __name__ == "__main__":
    unittest.main()
