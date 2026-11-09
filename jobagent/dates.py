"""Převod českých datumových textů z portálů na date.

Portály píšou datum různě: „Přidáno dnes“, „Aktualizováno před 3 dny“,
„12. října 2026“, „12. 10. 2026“, v JSON-LD „2026-10-12“. Jeden parser pro
všechno, aby collector nemusel řešit češtinu. Když text nepoznáme, vracíme
None a filtr stáří takový inzerát ponechá.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from jobagent.text import fold

_MONTHS = {
    "ledna": 1, "unora": 2, "brezna": 3, "dubna": 4, "kvetna": 5, "cervna": 6,
    "cervence": 7, "srpna": 8, "zari": 9, "rijna": 10, "listopadu": 11, "prosince": 12,
}


def parse_czech_date(text: str | None, today: date) -> date | None:
    if not text:
        return None
    t = fold(text)

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))

    if "dnes" in t or re.search(r"pred (\d+ )?(minut|hodin)", t):
        return today
    # „vcerejsi“ (štítek „Včerejší“ na prace.cz) „vcera“ neobsahuje, proto obojí.
    if "vcera" in t or "vcerejs" in t:
        return today - timedelta(days=1)

    relative = [
        (r"pred (\d+) (?:dny|dnu|dnem)", 1),
        (r"pred dnem", 1),
        (r"pred (\d+) tydn", 7),
        (r"pred tydnem", 7),
        (r"pred (\d+) mesic", 30),
        (r"pred mesicem", 30),
    ]
    for pattern, days in relative:
        m = re.search(pattern, t)
        if m:
            count = int(m.group(1)) if m.groups() else 1
            return today - timedelta(days=count * days)

    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", t)
    if m:
        return _safe_date(int(m[3]), int(m[2]), int(m[1]))

    m = re.search(r"(\d{1,2})\.\s*([a-z]+)(?:\s+(\d{4}))?", t)
    if m and m[2] in _MONTHS:
        year = int(m[3]) if m[3] else today.year
        result = _safe_date(year, _MONTHS[m[2]], int(m[1]))
        # „28. prosince“ bez roku čtené v lednu patří do loňska.
        if result and not m[3] and result > today:
            result = _safe_date(year - 1, _MONTHS[m[2]], int(m[1]))
        return result

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
