"""Slušné stahování stránek přes urllib.

Všechny síťové požadavky v programu jdou přes Fetcher, takže pravidla
slušnosti (prodleva, robots.txt, timeout, poctivý User-Agent) platí všude
a nedají se omylem obejít v novém collectoru.

Proč urllib a ne requests: standardní knihovna = menší exe a o jednu věc
méně, která se může rozbít (rozhodnutí ze zadání). Cena: dekódování znakové
sady a opakování pokusů si řešíme sami, je to ale pár řádků.

Cookies: drží se jen v paměti po dobu jednoho běhu (http.cookiejar, standardní
knihovna). Proč: jobs.cz zkouší novou verzi webu (beta.jobs.cz) a návštěvníky
do ní zařazuje podle cookie. Bez cookies se los házel u každého požadavku
znovu a stránka 2 výpisu skončila na betě, kterou neumíme číst. S cookies
zůstane program po celý běh v jedné verzi. reset_session() je zahodí, když
je potřeba zkusit štěstí znovu (viz Collector.fetch_page).

robots.txt podle RFC 9309 (oddíl 2.3.1):
  * 2xx → řídíme se pravidly,
  * 4xx (typicky 404 = soubor neexistuje) → web nic nezakazuje,
  * 5xx nebo chyba sítě → web je „nedostupný“ a RFC říká chovat se, jako
    by zakazoval vše. Celý web v tomto běhu přeskočíme (RobotsUnavailable)
    a pipeline to uvede ve stavovém řádku. Zítra se to zkusí znovu.
"""

from __future__ import annotations

import logging
import random
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Callable
from urllib.parse import urlsplit

from jobagent import __version__, tls

log = logging.getLogger(__name__)

# Adresa projektu v User-Agentu: správce webu z logu pozná, kdo k němu chodí a jak
# se chová. Bez e-mailu (ten do hlavičky nepatří, sbíraly by ho roboty).
PROJECT_URL = "https://github.com/konvaa/jobfinder"
USER_AGENT = f"JobAgent/{__version__} (+{PROJECT_URL}; osobni nastroj pro hledani prace; 1 dotaz za 1-3 s)"

_NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError)


class FetchError(Exception):
    """Stránku se nepodařilo stáhnout. Zpráva je srozumitelná pro uživatele."""


class RobotsUnavailable(FetchError):
    """robots.txt webu nejde zjistit (5xx/síť) → web v tomto běhu nestahujeme."""


@dataclass
class Response:
    url: str  # konečná adresa po přesměrování
    text: str
    status: int


# Stav robots.txt pro jeden web: parser, ALLOW_ALL (4xx) nebo UNAVAILABLE (5xx/síť).
_ALLOW_ALL = "allow_all"
_UNAVAILABLE = "unavailable"


class Fetcher:
    def __init__(
        self,
        delay_min: float = 1.0,
        delay_max: float = 3.0,
        timeout: int = 20,
        respect_robots: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._sleep = sleep  # v testech se podstrčí funkce, která nečeká
        self._last_request = 0.0
        self._robots: dict[str, object] = {}
        self.request_count = 0
        # Jeden TLS kontext pro celý běh: ověření certifikátů přes Windows (tls.py).
        self._tls = tls.create_context()
        self.reset_session()

    def reset_session(self) -> None:
        """Zahodí cookies (nová „návštěva“ webu). robots.txt zůstává v cache."""
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()),
            urllib.request.HTTPSHandler(context=self._tls),
        )

    # -- veřejné API ---------------------------------------------------------

    def get(self, url: str) -> Response:
        if self.respect_robots:
            self._check_robots(url)
        return self._fetch(url, retries=1)

    # -- interní -------------------------------------------------------------

    def _wait_politely(self) -> None:
        # Prodleva je globální, ne zvlášť pro každý web. Jednodušší, a při
        # pár stovkách požadavků denně na rychlosti nezáleží.
        # Náhodný rozptyl, aby požadavky nepřicházely v pravidelném rytmu, který působí jako útok.
        wait = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < wait:
            self._sleep(wait - elapsed)

    def _request(self, url: str) -> Response:
        """Jeden požadavek bez překladu chyb. HTTPError/URLError propadnou volajícímu."""
        self._wait_politely()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "cs,en;q=0.5",
                # Accept-Encoding záměrně neposíláme: urllib neumí gzip
                # rozbalit sám, server pak pošle nekomprimovaný text.
            },
        )
        self._last_request = time.monotonic()
        self.request_count += 1
        with self._opener.open(request, timeout=self.timeout) as resp:
            body = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            log.debug("GET %s -> %s (%d B)", url, resp.status, len(body))
            return Response(url=resp.geturl(), text=body.decode(charset, errors="replace"), status=resp.status)

    def _fetch(self, url: str, retries: int) -> Response:
        try:
            return self._request(url)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 and retries > 0:
                log.warning("Server vrátil %s pro %s, zkusím to znovu za chvíli.", exc.code, url)
                self._sleep(5)
                return self._fetch(url, retries - 1)
            if exc.code in (403, 429):
                raise FetchError(
                    f"Portál odmítl požadavek (HTTP {exc.code}) – možná blokuje automatické stahování "
                    f"nebo jsme se ptali moc často. Adresa: {url}"
                ) from exc
            if exc.code == 404:
                raise FetchError(
                    f"Stránka neexistuje (HTTP 404) – zkontroluj adresu v prohlížeči a zkopíruj ji znovu: {url}"
                ) from exc
            raise FetchError(f"HTTP chyba {exc.code} pro {url}") from exc
        except _NETWORK_ERRORS as exc:
            if tls.is_cert_error(exc):
                # Neopakovat: certifikát bude za 5 s stejný. Vlastní rada místo „síťová chyba“.
                raise FetchError(tls.cert_advice(urlsplit(url).netloc, mail=False)) from exc
            if retries > 0:
                log.warning("Síťová chyba u %s (%s), zkusím to znovu.", url, exc)
                self._sleep(5)
                return self._fetch(url, retries - 1)
            reason = getattr(exc, "reason", exc)
            raise FetchError(f"Nepodařilo se spojit s {urlsplit(url).netloc}: {reason}") from exc

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        state = self._robots[origin]
        if state == _UNAVAILABLE:
            raise RobotsUnavailable(
                f"{parts.netloc}: robots.txt teď nejde načíst (chyba serveru nebo sítě), "
                "web v tomto běhu přeskakuji."
            )
        if state != _ALLOW_ALL and not state.can_fetch(USER_AGENT, url):  # type: ignore[union-attr]
            raise FetchError(f"robots.txt webu zakazuje stahovat {url} – přeskakuji.")

    def _load_robots(self, origin: str) -> object:
        # robots.txt stahujeme vlastním _request, ne RobotFileParser.read():
        # ten posílá User-Agent „Python-urllib“, který některé weby rovnou blokují,
        # neumí timeout ani prodlevu a 5xx nerozliší od 4xx tak, jak chce RFC.
        url = origin + "/robots.txt"
        for attempt in (1, 2):  # jeden opakovaný pokus: krátký výpadek nemá shodit celý web
            try:
                resp = self._request(url)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    # DEBUG: kariérní weby firem (firma.jobs.cz) robots.txt nemají
                    # a každá subdoména by jinak zapsala do logu zbytečný řádek.
                    log.debug("robots.txt pro %s vrací %s → bez omezení (RFC 9309).", origin, exc.code)
                    return _ALLOW_ALL
                log.warning("robots.txt pro %s vrací %s (pokus %d).", origin, exc.code, attempt)
            except _NETWORK_ERRORS as exc:
                if tls.is_cert_error(exc):
                    # Bez toho by hláška zněla „robots.txt teď nejde načíst“ a příčina by se ztratila.
                    raise FetchError(tls.cert_advice(urlsplit(origin).netloc, mail=False)) from exc
                log.warning("robots.txt pro %s nejde stáhnout: %s (pokus %d).", origin, exc, attempt)
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
                return parser
            if attempt == 1:
                self._sleep(5)
        return _UNAVAILABLE
