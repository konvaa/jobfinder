"""Parsování mzdy z textu a rozhodnutí „je to jistě pod minimem?“.

Hlavní zásada (ze zadání): vyřazujeme JEN když jsme si jistí. Chybějící mzda,
neznámá měna nebo nejasná jednotka = ponechat. Proto monthly_upper() vrací
None ve všech nejistých případech a filtr None nikdy nevyřadí.
"""

from __future__ import annotations

import re

from jobagent.models import HiddenSalary, Job, Salary
from jobagent.text import clean, fold

# „40 000“, „40.000“ nebo prosté „40000“ / „180“ / „35,5“.
_NUMBER = re.compile(r"\d{1,3}(?:[ .]\d{3})+(?!\d)|\d+(?:,\d+)?")
_THOUSANDS = re.compile(r"\d\s*(?:tis\b|tis\.|k\b)")

_HOUR = re.compile(r"/\s*h(?:od)?\b|\bza hod|\bhodin|\bhod\b|kc/h\b")
_YEAR = re.compile(r"/\s*rok|\brocne\b|\bza rok\b|\bp\.\s*a\.")
_MONTH = re.compile(r"/\s*mes|\bmesic|\bmesicne\b")


def _to_number(token: str) -> float:
    return float(token.replace(" ", "").replace(".", "").replace(",", "."))


def parse_salary(text: str) -> Salary | None:
    if not text or not text.strip():
        return None
    raw = clean(text)
    t = fold(raw)

    numbers = [_to_number(m.group()) for m in _NUMBER.finditer(t)]
    if not numbers:
        return None
    if _THOUSANDS.search(t):
        # „35–40 tis. Kč“: násobíme jen malá čísla, „40 000 tis.“ by byl nesmysl.
        numbers = [n * 1000 if n < 1000 else n for n in numbers]

    if "eur" in t or "€" in raw:
        currency = "EUR"
    elif "kc" in t or "czk" in t:
        currency = "CZK"
    else:
        currency = None

    if _HOUR.search(t):
        period = "hour"
    elif _YEAR.search(t):
        period = "year"
    elif _MONTH.search(t):
        period = "month"
    else:
        period = None

    if len(numbers) >= 2:
        low, high = numbers[0], numbers[1]
        if low > high:
            low, high = high, low
    else:
        n = numbers[0]
        if re.search(r"\bod\b", t) and not re.search(r"\bdo\b|\baz\b", t):
            low, high = n, None  # „od 35 000“ – horní hranice neznámá
        elif re.search(r"\bdo\b|\baz\b|\bmax", t):
            low, high = None, n  # „až 60 000“
        else:
            low = high = n

    return Salary(
        low=int(low) if low is not None else None,
        high=int(high) if high is not None else None,
        period=period,
        currency=currency,
        raw=raw,
    )


def monthly_upper(salary: Salary | None, hours_per_month: int) -> float | None:
    """Horní hranice přepočtená na Kč/měsíc, nebo None, když si nejsme jistí.

    Proč horní hranice: u rozpětí 35–45 tis. a minima 40 tis. má smysl se
    ozvat, firma 40+ zaplatit může. Pod minimum je až nabídka, jejíž MAXIMUM
    je nižší.
    """
    if salary is None or salary.high is None:
        return None
    if salary.currency not in (None, "CZK"):
        return None  # EUR apod. nepřepočítáváme, kurz se mění a jde o okrajový případ
    high = salary.high
    if salary.period == "hour":
        return high * hours_per_month
    if salary.period == "year":
        return high / 12
    if salary.period == "month":
        return high
    # Jednotka neuvedena: české portály uvádějí měsíční mzdu, takže velká čísla
    # bereme jako měsíční. Malé číslo bez jednotky může být hodinovka i chyba → nevíme.
    if high >= 10_000:
        return high
    return None


def has_visible_salary(job: Job) -> bool:
    """Ukazuje inzerát (výpis nebo detail) nějakou částku? „Mzda dohodou“ se nepočítá."""
    return parse_salary(job.salary_text) is not None


def resolve_hidden(job: Job, minimum: int | None) -> HiddenSalary | None:
    """Skrytá mzda z výsledků hledání se mzdou (job.salary_checks), nebo None.

    None, když: inzerát mzdu ukazuje (tu hlídá náš filtr), minimum není
    nastavené, žádné hledání se mzdou nedalo jistou odpověď, nebo se portály
    neshodnou (jeden „prošel“, druhý „neprošel“ → nevíme).
    """
    if not minimum or not job.salary_checks or has_visible_salary(job):
        return None
    answers = set(job.salary_checks.values())
    if len(answers) != 1:
        return None
    return HiddenSalary(below=not answers.pop(), portals=list(job.salary_checks), minimum=minimum)
