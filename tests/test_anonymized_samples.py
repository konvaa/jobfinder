"""Anonymizované ukázky se musí parsovat stejně jako originály.

Originály leží v samples/private/ (nejsou v gitu, jen u autora). Bez nich se
testy přeskočí. Porovnávají se údaje, které čte program (ID, názvy, firmy,
místa, mzdy, štítky, data, ID jobs.cz, další stránka, způsob a výsledek
čtení detailu), ne text inzerátu: ten anonymizace záměrně nahrazuje.
Hodnoty z originálu se před porovnáním proženou stejnou náhradou lokality
a kontaktů jako HTML (map_text), jinak by se lišily právě o ni.
"""

import importlib.util
import unittest
from datetime import date

from jobagent import collectors
from jobagent.detail import MIN_TEXT

from tests.helpers import SAMPLES, needs_private, private_sample, sample

_spec = importlib.util.spec_from_file_location("anonymize_samples", SAMPLES.parent / "tools" / "anonymize_samples.py")
anon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(anon)

TODAY = date(2026, 11, 8)
# Adresa stránky: jen základ pro relativní odkazy a výběr collectoru (stejná pro obě verze).
PAGE_URL = {
    "jobs_listing": "https://www.jobs.cz/prace/",
    "prace_listing": "https://www.prace.cz/nabidky/",
    "jobscontact_listing": "https://www.jobscontact.cz/prace/informacni-technologie/brno",
    "prace_detail": "https://www.prace.cz/nabidka/x/",
    "jobscontact_detail": "https://www.jobscontact.cz/prace/x_id1",
}


def listing_view(c, html, url):
    page = c.parse_listing(html, url, TODAY)
    jobs = [(j.native_id, j.url, j.title, j.company, j.location, j.salary_text, tuple(j.tags), j.posted, j.alt_key)
            for j in page.jobs]
    return jobs, page.next_url


def mapped_listing(view):
    jobs, nxt = view
    m = anon.map_text
    return ([(i, m(u), m(t), m(co), m(lo), sal, tuple(m(x) for x in tags), posted, alt)
             for i, u, t, co, lo, sal, tags, posted, alt in jobs], m(nxt) if nxt else None)


class AnonymizedSamplesMatchOriginals(unittest.TestCase):
    @needs_private("anonymize_map.json")
    def test_every_spec_entry(self):
        checked = 0
        for source, target, kind in anon.SPEC:
            if not (SAMPLES / "private" / source).exists():
                continue
            checked += 1
            with self.subTest(target):
                original, public = private_sample(source), sample(target)
                url = PAGE_URL[kind]
                c = collectors.for_url(url)
                if kind.endswith("_listing"):
                    orig_view = listing_view(c, original, url)
                    self.assertEqual(listing_view(c, public, url), mapped_listing(orig_view))
                    self.assertGreater(len(orig_view[0]) + (orig_view[1] is not None) + ("beta" in source)
                                       + ("prazdna" in source), 0)
                else:
                    a, b = c.parse_detail(original, TODAY), c.parse_detail(public, TODAY)
                    self.assertEqual(
                        (b.method, b.salary_text, b.location, b.company, b.posted),
                        (a.method, anon.map_text(a.salary_text or ""), anon.map_text(a.location or ""),
                         anon.map_text(a.company or ""), a.posted),
                    )
                    self.assertGreaterEqual(len(b.description), min(len(a.description), MIN_TEXT))
                    self.assertNotEqual(b.description, a.description, "text inzerátu má být nahrazený")
        if not checked:
            self.skipTest("Chybí originály v samples/private/ (jsou jen u autora, viz samples/README.md).")

    def test_public_copies_have_no_leftovers(self):
        # Běží vždy: veřejné kopie nesmí obsahovat nic z kontrolního seznamu.
        for _, target, _ in anon.SPEC:
            with self.subTest(target):
                self.assertEqual(anon.leftovers(sample(target)), [])


if __name__ == "__main__":
    unittest.main()
