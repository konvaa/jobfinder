"""jobs.cz občas přesměruje na beta.jobs.cz (A/B test). Ukázka je skutečná stránka 2
hledání IT z 5. 11. 2026, kterou program dostal místo klasického výpisu."""

import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

from bs4 import BeautifulSoup

from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.fetch import Fetcher, FetchError, Response

from tests.helpers import sample

TODAY = date(2026, 11, 5)
P1 = "https://www.jobs.cz/prace/?q%5B%5D=it&page=1"
P2 = "https://www.jobs.cz/prace/?q%5B0%5D=it&page=2"
BETA = "https://beta.jobs.cz/prace/jihomoravsky/brno/?keywords%5B%5D=it"


def classic_listing(next_url=None):
    """Zkrácený klasický výpis s odkazem „další“ na next_url (None = poslední stránka)."""
    soup = BeautifulSoup(sample("jobs_cz_vypis_zkraceny.html"), "html.parser")
    link = soup.select_one("a.Pagination__button--next")
    if next_url is None:
        link.decompose()
    else:
        link["href"] = next_url
    return str(soup)


class ScriptedFetcher:
    """Vrací odpovědi podle scénáře {url: [Response, …]}; počítá reset_session()."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.resets = 0

    def get(self, url):
        return self.script[url].pop(0)

    def reset_session(self):
        self.resets += 1


class BetaTests(unittest.TestCase):
    def setUp(self):
        self.collector = JobsCzCollector()
        self.beta = Response(url=BETA, text=sample("jobs_cz_beta_vypis_presmerovani.html"), status=200)
        self.p1 = Response(url=P1, text=classic_listing(P2), status=200)
        self.p2 = Response(url=P2, text=classic_listing(None), status=200)

    def test_beta_html_has_no_classic_cards(self):
        # Dokumentuje původní chybu: parser na betě nenajde nic.
        self.assertEqual(self.collector.parse_listing(self.beta.text, BETA, TODAY).jobs, [])
        self.assertIn("beta.jobs.cz", self.collector.unsupported_variant(self.beta))
        self.assertIsNone(self.collector.unsupported_variant(self.p1))

    def test_retry_with_new_session(self):
        web = ScriptedFetcher({P1: [self.p1], P2: [self.beta, self.p2]})
        jobs, notes = self.collector.collect(web, P1, 3, "IT", TODAY)
        self.assertEqual(len(jobs), 16)  # obě stránky
        self.assertEqual(notes, [])
        self.assertEqual(web.resets, 1)

    def test_persistent_beta_on_page_two_keeps_page_one(self):
        web = ScriptedFetcher({P1: [self.p1], P2: [self.beta] * 3})
        with self.assertLogs("jobagent.collectors.base", level="INFO"):
            jobs, notes = self.collector.collect(web, P1, 3, "IT", TODAY)
        self.assertEqual(len(jobs), 8)
        self.assertEqual(len(notes), 1)
        self.assertIn("beta.jobs.cz", notes[0])

    def test_persistent_beta_on_page_one_is_error(self):
        web = ScriptedFetcher({P1: [self.beta] * 3})
        with self.assertLogs("jobagent.collectors.base", level="INFO"), self.assertRaises(FetchError):
            self.collector.collect(web, P1, 3, "IT", TODAY)


class PageLimitTests(unittest.TestCase):
    """Varování, když hledání skončí na limitu stránek a portál nabízí další."""

    def setUp(self):
        self.p1 = Response(url=P1, text=classic_listing(P2), status=200)
        self.p2 = Response(url=P2, text=classic_listing(None), status=200)

    def test_truncated_warns(self):
        web = ScriptedFetcher({P1: [self.p1]})
        jobs, notes = JobsCzCollector().collect(web, P1, 1, "IT", TODAY)
        self.assertEqual(len(jobs), 8)
        self.assertEqual(len(notes), 1)
        self.assertIn("oříznuté limitem stránek (1)", notes[0])

    def test_complete_search_does_not_warn(self):
        # Poslední stránka nemá odkaz „další“ → žádné varování, i když je limit vyčerpaný.
        web = ScriptedFetcher({P1: [self.p1], P2: [self.p2]})
        jobs, notes = JobsCzCollector().collect(web, P1, 2, "IT", TODAY)
        self.assertEqual((len(jobs), notes), (16, []))


class CookieTests(unittest.TestCase):
    """Fetcher musí v rámci běhu posílat cookies zpět (zůstat v jedné verzi webu)."""

    def test_cookie_round_trip_and_reset(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/robots.txt":
                    self.send_response(404)
                    self.end_headers()
                    return
                seen.append(self.headers.get("Cookie"))
                self.send_response(200)
                self.send_header("Set-Cookie", "variant=classic; Path=/")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html>ok</html>")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        fetcher = Fetcher(delay_min=0, delay_max=0, sleep=lambda s: None)
        with self.assertLogs("jobagent.fetch", level="DEBUG"):  # robots.txt 404 → debug
            fetcher.get(base + "/a")
        fetcher.get(base + "/b")
        fetcher.reset_session()
        fetcher.get(base + "/c")
        self.assertEqual(seen, [None, "variant=classic", None])


if __name__ == "__main__":
    unittest.main()
