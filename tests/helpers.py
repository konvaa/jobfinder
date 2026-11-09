"""Společné pomůcky pro testy: načtení ukázek a falešný Fetcher bez sítě."""

from __future__ import annotations

import unittest
from pathlib import Path

from jobagent.fetch import FetchError, Response

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


PRIVATE = SAMPLES / "private"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def private_sample(name: str) -> str:
    """Originál ukázky ze samples/private/ (není v gitu, jen u autora)."""
    return (PRIVATE / name).read_text(encoding="utf-8")


def needs_private(*names: str):
    """Přeskočí test, když chybí soukromý originál (veřejný repozitář je nemá)."""
    missing = [n for n in names if not (PRIVATE / n).exists()]
    return unittest.skipUnless(
        not missing,
        f"Chybí soukromá ukázka samples/private/{', '.join(missing)} (jen u autora, viz samples/README.md).",
    )


class FakeFetcher:
    """Místo sítě vrací připravené HTML podle URL.

    Proč ne unittest.mock: pipeline volá jen fetcher.get(url), takže malá
    třída se slovníkem je čitelnější a v testu je hned vidět, co „web“ vrátí.
    """

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> Response:
        self.requested.append(url)
        for prefix, html in self.pages.items():
            if url.startswith(prefix):
                # Hodnota může být i funkce url → HTML (detail šitý na konkrétní inzerát).
                text = html(url) if callable(html) else html
                return Response(url=url, text=text, status=200)
        raise FetchError(f"HTTP chyba 404 pro {url}")
