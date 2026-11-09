"""Registr collectorů: podle URL z configu najde správný portál.

Proč explicitní seznam a ne automatické načtení všech souborů ze složky:
PyInstaller do exe zabalí jen moduly, které vidí v import příkazech.
Dynamicky načítané (importlib/pkgutil) by v exe tiše chyběly a portál by
u kamaráda „nefungoval“ bez zjevného důvodu. Nový portál = nový soubor
+ jeden řádek tady.
"""

from __future__ import annotations

from jobagent.collectors.base import Collector, ListingPage
from jobagent.collectors.jobs_cz import JobsCzCollector
from jobagent.collectors.jobscontact_cz import JobsContactCzCollector
from jobagent.collectors.prace_cz import PraceCzCollector

# Pořadí = přednost při deduplikaci: když se inzerát najde na víc portálech,
# zůstane verze z portálu, který je v seznamu výš (jobs.cz má historii
# odeslaných a odladěný detail).
COLLECTORS: list[Collector] = [
    JobsCzCollector(),
    PraceCzCollector(),
    # Web agentury: místo jen na úrovni okresu a bez data, proto až za portály.
    JobsContactCzCollector(),
]


def for_url(url: str, portal: str | None = None) -> Collector | None:
    """Najde collector pro URL. `portal` z configu má přednost před doménou."""
    if portal:
        wanted = portal.strip().lower()
        return next((c for c in COLLECTORS if c.name == wanted), None)
    return next((c for c in COLLECTORS if c.handles(url)), None)


def by_name(name: str) -> Collector | None:
    return next((c for c in COLLECTORS if c.name == name), None)


def priority(portal: str) -> int:
    """Nižší číslo = vyšší přednost (pořadí v COLLECTORS)."""
    return next((i for i, c in enumerate(COLLECTORS) if c.name == portal), len(COLLECTORS))


def supported_portals() -> list[str]:
    return [c.name for c in COLLECTORS]


__all__ = ["Collector", "ListingPage", "COLLECTORS", "for_url", "by_name", "priority", "supported_portals"]
