"""Skrytá mzda: hledání bez filtru mzdy a se filtrem mzdy portálu.

Skutečná data (prace.cz, hledání „python“, okruh 40 km):
  * bez filtru 7 inzerátů, s filtrem minimální mzdy (salaryMin) jen 3. Všechny
    4 vyřazené mzdu neukazují a je mezi nimi VertiFlex (Fullstack vývojář/ka),
  * prace_cz_vypis_python_se_mzdou.html je skutečný výpis bez těch 4 karet
    (výsledek ověřený v prohlížeči 6. a 8. 11. 2026).
Ukázky jsou anonymizované (samples/README.md): lokalita hledání je Brno a částka
filtru 50 000. Na výsledek to vliv nemá, testuje se chování, ne konkrétní částka.
Ostatní scénáře (neúplné druhé hledání, víc hledání, konflikt portálů) stojí
na zkráceném výpisu jobs.cz.
"""

import re
import tempfile
from dataclasses import replace
import unittest
from datetime import date, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from jobagent import emailer, explain, pipeline, salary, scoring
from jobagent.collectors.base import Collector
from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.collectors.prace_cz import PraceCzCollector
from jobagent.config import Search, load_config
from jobagent.fetch import FetchError
from jobagent.models import HiddenSalary, Job
from jobagent.storage import SCHEMA_VERSION, Storage

from tests.helpers import SAMPLES, FakeFetcher, sample
from tests.test_pipeline import LISTING_URL as JOBS_URL, make_web

ROOT = SAMPLES.parent
NOW = datetime(2026, 11, 6, 10, 0)
PRACE_URL = "https://www.prace.cz/nabidky/?q=python&workLocationIds%5B%5D=M265747%3B40"
VERTIFLEX = "e188232c-0ed7-402c-82dc-7a6b5795c034"
VERTIFLEX_URL = f"https://www.prace.cz/nabidka/{VERTIFLEX}/"
ONSEMI_URL = "https://www.prace.cz/firma/hjjnbs-onsemi/nabidka/7048c582-de91-4139-a442-e8fd39d300ce/"
BELOW = {VERTIFLEX, "e80957f4-0b4c-423e-9a3d-943ed85d2392", "4f53759b-5a87-47d8-a878-cb41cd80c054"}
ABOVE = {"9b94ca3b-55c1-4927-bc05-e808bb0caa6f", "30065225-e633-489b-b37c-422136495800",
         "c90a038a-4db4-4e07-8f07-67eb50e912ad"}

BRNO_JOBS = ("https://www.jobs.cz/prace/?q%5B%5D=python&locality%5Bcode%5D=M265747&locality%5Blabel%5D="
             "Brno%2C%20okres%20Brno-m%C4%9Bsto"
             "&locality%5Bcoords%5D=49.19186%2C16.61108&locality%5Bradius%5D=40")


def shown(report):
    return report.new_jobs + report.title_jobs + report.no_text_other


def subset_listing(html: str, keep: set[str], keep_next: bool = False) -> str:
    """Výpis jobs.cz jen s vybranými kartami (simulace výsledku hledání se mzdou)."""
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("article.SearchResultCard"):
        link = card.select_one("a[data-jobad-id]")
        if link is None or link["data-jobad-id"] not in keep:
            card.decompose()
    if not keep_next:
        for a in soup.select("a.Pagination__button--next"):
            a.decompose()
    return str(soup)


class UrlTests(unittest.TestCase):
    def test_jobs_cz_adds_salary_keeps_rest_verbatim(self):
        c = JobsCzCollector()
        self.assertEqual(c.with_salary(BRNO_JOBS, 50000), BRNO_JOBS + "&salary=50000")
        self.assertEqual(c.without_salary(BRNO_JOBS), (BRNO_JOBS, None))

    def test_salary_from_config_url_is_replaced(self):
        c = JobsCzCollector()
        url = BRNO_JOBS.replace("&locality%5Bradius%5D", "&salary=30000&locality%5Bradius%5D")
        self.assertEqual(c.without_salary(url), (BRNO_JOBS, "30000"))
        self.assertEqual(c.with_salary(url, 50000), BRNO_JOBS + "&salary=50000")

    def test_prace_cz(self):
        c = PraceCzCollector()
        self.assertEqual(c.with_salary(PRACE_URL, 50000), PRACE_URL + "&salaryMin=50000")
        self.assertEqual(c.without_salary(PRACE_URL + "&salaryMin=20000"), (PRACE_URL, "20000"))
        self.assertEqual(c.with_salary("https://www.prace.cz/nabidky/", 1), "https://www.prace.cz/nabidky/?salaryMin=1")

    def test_portal_without_salary_param_untouched(self):
        class Plain(Collector):
            def parse_listing(self, html, page_url, today):
                raise NotImplementedError

        self.assertEqual(Plain().without_salary("https://x.cz/?salary=1"), ("https://x.cz/?salary=1", None))


class VertiflexTests(unittest.TestCase):
    """Skutečné výpisy prace.cz bez a s filtrem minimální mzdy (salaryMin)."""

    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = [Search(name="python prace", url=PRACE_URL)]
        self.cfg.filters.min_salary = 50000
        self.cfg.scoring.hidden_salary_penalty = -10
        self.cfg.summary.score_threshold = None
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def web(self):
        vertiflex = sample("prace_cz_detail_vertiflex.html")
        return FakeFetcher({
            PRACE_URL + "&salaryMin=50000": sample("prace_cz_vypis_python_se_mzdou.html"),
            PRACE_URL: sample("prace_cz_vypis_python.html"),
            VERTIFLEX_URL: vertiflex,
            ONSEMI_URL: sample("prace_cz_detail_firma_onsemi.html"),  # detail ukazuje mzdu 20–40 tis.
            "https://www.prace.cz/": vertiflex,  # Brose, Weppler: stačí jakýkoli detail s JSON-LD
            "https://": sample("syntetika_js_kostra_almacareer.html"),  # kariérní weby firem (bez textu)
        })

    def run_once(self, web):
        return pipeline.run(self.cfg, self.storage, web, dry_run=True, now=NOW,
                            unknown_samples_dir=Path(self.tmp.name))

    def test_vertiflex_marked_and_penalised(self):
        report = self.run_once(self.web())
        by_id = {j.native_id: j for j in shown(report)}
        job = by_id[VERTIFLEX]
        self.assertEqual(job.hidden_salary, HiddenSalary(below=True, portals=["prace.cz"], minimum=50000))
        self.assertIn("-10 mzda skrytá, pravděpodobně pod minimem", job.score_reasons)
        without = scoring.score(replace(job, hidden_salary=None), self.cfg.scoring)[0]
        self.assertEqual(job.score, without - 10)
        html = emailer.render(report, ROOT / "templates", now=NOW)
        self.assertIn("skrytá, podle prace.cz pravděpodobně pod minimem", html)

    def test_all_cards(self):
        report = self.run_once(self.web())
        by_id = {j.native_id: j for j in shown(report)}
        self.assertEqual({k for k, j in by_id.items() if j.hidden_salary and j.hidden_salary.below}, BELOW)
        self.assertEqual({k for k, j in by_id.items() if j.hidden_salary and not j.hidden_salary.below}, ABOVE)
        for key in ABOVE:
            self.assertFalse([r for r in by_id[key].score_reasons if "skrytá" in r])
        # Onsemi: filtr portálu ho vyřadil, ale detail mzdu ukazuje (max. 40 000) → hlídá ji náš filtr.
        onsemi = [j for j in report.rejected if j.native_id.startswith("7048c582")]
        self.assertEqual(len(onsemi), 1)
        self.assertIn("pod minimem", onsemi[0].reject_reason)
        self.assertIsNone(onsemi[0].hidden_salary)
        self.assertEqual(report.hidden_salary_counts(), (3, 3))
        html = emailer.render(report, ROOT / "templates", now=NOW)
        self.assertIn("skrytá, podle prace.cz ≥ 50 000 Kč", html)
        self.assertIn("skrytá mzda: 3 pravděpodobně pod minimem, 3 nad minimem", html)
        self.assertIsNone(report.balance_error())

    def test_penalty_also_in_title_points(self):
        report = self.run_once(self.web())
        opmobility = [j for j in shown(report) if j.native_id.startswith("e80957f4")][0]  # kariérní web, bez textu
        self.assertFalse(opmobility.text)
        plain = scoring.title_points(Job(portal="prace.cz", native_id="x", url="u", title=opmobility.title),
                                     self.cfg.scoring)
        self.assertEqual(opmobility.title_score, plain - 10)
        # Kompaktní řádek sekce „Ostatní“ (bez popisu): mzda i důvod srážky musí být vidět.
        self.assertIn(opmobility, report.no_text_other)
        html = emailer.render(report, ROOT / "templates", now=NOW)
        row = next(m.group(0) for m in re.finditer(r'<div style="padding:4px 0.*?</div>', html, re.S)
                   if "opmobility.jobs.cz" in m.group(0))
        self.assertIn("skrytá, podle prace.cz pravděpodobně pod minimem", row)
        self.assertIn("-10 mzda skrytá, pravděpodobně pod minimem", row)

    def test_second_pass_downloads_no_details(self):
        web = self.web()
        report = self.run_once(web)
        listing = [u for u in web.requested if u.startswith(PRACE_URL)]
        self.assertEqual(listing, [PRACE_URL, PRACE_URL + "&salaryMin=50000"])
        details = [u for u in web.requested if not u.startswith(PRACE_URL)]
        self.assertEqual(len(details), len(set(details)), "každý detail jen jednou")
        self.assertEqual(len(details), 7)
        self.assertEqual(report.details_fetched, 7)
        self.assertEqual(report.details_over_limit, 0)

    def test_detail_limit_not_used_by_second_pass(self):
        self.cfg.general.detail_limit = 7  # přesně na detaily, druhé hledání nesmí nic ubrat
        report = self.run_once(self.web())
        self.assertEqual((report.details_fetched, report.details_over_limit, len(report.pending)), (7, 0, 0))

    def test_stored_for_explain(self):
        self.run_once(self.web())
        listing = self.storage.find_listing("prace.cz", VERTIFLEX, VERTIFLEX_URL)
        self.assertTrue(listing.hidden_salary.below)
        out = explain.explain(sample("prace_cz_detail_vertiflex.html"), VERTIFLEX_URL, self.cfg, NOW.date(),
                              listing=listing)
        self.assertIn("skrytá, podle prace.cz pravděpodobně pod minimem (z posledního běhu)", out)
        self.assertIn("-10  mzda skrytá, pravděpodobně pod minimem (podle hledání se mzdou na prace.cz)", out)

    def test_without_min_salary_one_pass(self):
        self.cfg.filters.min_salary = None
        web = self.web()
        report = self.run_once(web)
        self.assertEqual([u for u in web.requested if u.startswith(PRACE_URL)], [PRACE_URL])
        self.assertFalse([j for j in shown(report) if j.hidden_salary])


class UncertaintyTests(unittest.TestCase):
    """Zkrácený výpis jobs.cz: 8 karet, IT konzultant (2001491347) jako jediný ukazuje mzdu."""

    KEEP = {"2001480136", "2001462015"}

    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = [Search(name="it", url=JOBS_URL)]
        self.cfg.general.max_pages = 1
        self.cfg.filters.min_salary = 50000
        self.cfg.scoring.hidden_salary_penalty = -10
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.listing = sample("jobs_cz_vypis_zkraceny.html")

    def web(self, filtered, url=JOBS_URL):
        web = make_web()
        web.pages = {f"{url}&salary=50000": filtered, **web.pages}
        return web

    def run_once(self, web):
        return pipeline.run(self.cfg, self.storage, web, dry_run=True, now=NOW,
                            unknown_samples_dir=Path(self.tmp.name))

    def verdicts(self, report):
        jobs = shown(report) + report.pending + report.rejected
        return {j.native_id: (None if j.hidden_salary is None else j.hidden_salary.below) for j in jobs}

    def test_complete_pass(self):
        report = self.run_once(self.web(subset_listing(self.listing, self.KEEP)))
        v = self.verdicts(report)
        self.assertEqual(v["2001480136"], False)
        self.assertEqual(v["2001502919"], True)
        self.assertIsNone(v["2001491347"], "viditelná mzda → hlídá náš filtr, ne portál")
        self.assertFalse([w for w in report.warnings if "se mzdou" in w])

    def test_truncated_pass_marks_only_passed(self):
        # Druhé hledání narazí na limit stránek (portál nabízí další stránku).
        report = self.run_once(self.web(subset_listing(self.listing, self.KEEP, keep_next=True)))
        v = self.verdicts(report)
        self.assertEqual(v["2001480136"], False)  # „prošel“ platí i z neúplného průchodu
        self.assertIsNone(v["2001502919"], "chybí jen v neúplném průchodu → nevíme")
        self.assertTrue([w for w in report.warnings if "neproběhlo celé" in w])

    def test_failed_pass(self):
        def listing(url):
            if "salary=" in url:
                raise FetchError("HTTP chyba 503")
            return self.listing

        web = make_web()
        web.pages[JOBS_URL] = listing
        report = self.run_once(web)
        self.assertEqual(report.errors, 0, "chyba druhého hledání není chyba hledání")
        self.assertEqual(set(self.verdicts(report).values()), {None})
        self.assertTrue([w for w in report.warnings if "HTTP chyba 503" in w])
        self.assertTrue([w for w in report.warnings if "neproběhlo celé" in w])

    def test_passed_in_any_search_wins(self):
        other = JOBS_URL.replace("radius%5D=50", "radius%5D=40")
        self.cfg.searches.append(Search(name="it 40", url=other))
        web = self.web(subset_listing(self.listing, self.KEEP))
        web.pages = {other + "&salary=50000": subset_listing(self.listing, {"2001502919"}),
                     other: self.listing, **web.pages}
        v = self.verdicts(self.run_once(web))
        self.assertEqual(v["2001502919"], False, "v druhém hledání filtrem prošel")
        self.assertEqual(v["2001515412"], True, "chybí v obou úplných průchodech")

    def test_salary_in_config_url_ignored(self):
        url = JOBS_URL + "&salary=30000"
        self.cfg.searches = [Search(name="it", url=url)]
        web = self.web(subset_listing(self.listing, self.KEEP))
        report = self.run_once(web)
        listing_requests = [u for u in web.requested if u.startswith(JOBS_URL)]
        self.assertEqual(listing_requests, [JOBS_URL, JOBS_URL + "&salary=50000"])
        self.assertTrue([w for w in report.warnings if "salary=30000" in w and "Smaž" in w])
        self.assertEqual(self.storage.conn.execute("SELECT url FROM search_stats").fetchone()[0], url)


class ResolveTests(unittest.TestCase):
    def job(self, checks, salary_text=""):
        return Job(portal="jobs.cz", native_id="1", url="u", title="t", salary_text=salary_text,
                   salary_checks=dict(checks))

    def test_portals_disagree(self):
        self.assertIsNone(salary.resolve_hidden(self.job({"jobs.cz": False, "prace.cz": True}), 50000))

    def test_portals_agree(self):
        hidden = salary.resolve_hidden(self.job({"jobs.cz": False, "prace.cz": False}), 50000)
        self.assertEqual(hidden.label, "skrytá, podle jobs.cz a prace.cz pravděpodobně pod minimem")

    def test_visible_or_unknown(self):
        self.assertIsNone(salary.resolve_hidden(self.job({"jobs.cz": False}, "40 000 Kč"), 50000))
        self.assertIsNone(salary.resolve_hidden(self.job({}), 50000))
        self.assertIsNone(salary.resolve_hidden(self.job({"jobs.cz": False}), None))
        # „Mzda dohodou“ žádnou částku neukazuje → pořád skrytá.
        self.assertTrue(salary.resolve_hidden(self.job({"jobs.cz": False}, "Mzda dohodou"), 50000).below)

    def test_merge_of_duplicates(self):
        from jobagent.dedup import merge_into

        primary = self.job({"jobs.cz": False})
        merge_into(primary, Job(portal="prace.cz", native_id="u", url="u", title="t", salary_checks={"prace.cz": True}))
        self.assertEqual(primary.salary_checks, {"jobs.cz": False, "prace.cz": True})
        self.assertIsNone(salary.resolve_hidden(primary, 50000))

    def test_json_roundtrip_and_schema(self):
        h = HiddenSalary(below=False, portals=["jobs.cz"], minimum=50000)
        self.assertEqual(HiddenSalary.from_json(h.to_json()), h)
        self.assertEqual(h.label, "skrytá, podle jobs.cz ≥ 50 000 Kč")
        s = Storage(":memory:")
        self.addCleanup(s.close)
        self.assertEqual(s.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        s._migrate()  # znovu: idempotentní

    def test_config(self):
        cfg = load_config(ROOT / "config.example.toml")
        self.assertEqual(cfg.scoring.hidden_salary_penalty, -10)


if __name__ == "__main__":
    unittest.main()
