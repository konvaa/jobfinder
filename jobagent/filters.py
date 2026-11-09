"""Tvrdé filtry: vrací důvod vyřazení, nebo None (= inzerát zůstává).

Spouští se dvakrát: nad daty z výpisu (abychom nestahovali detail zbytečně)
a znovu po stažení detailu (víc textu = víc šancí najít „3směnný provoz“).
Proto musí každý filtr unést chybějící data. Chybějící mzda nebo datum
znamená „nevím“ a inzerát se NIKDY nevyřadí.
"""

from __future__ import annotations

from datetime import date

from jobagent.config import Filters
from jobagent.models import Job
from jobagent.salary import monthly_upper, parse_salary
from jobagent.text import find_variant, fold, format_czk


def full_text(job: Job) -> str:
    """Veškerý text inzerátu (fold-nutý), ve kterém se hledají klíčová slova."""
    return fold(" \n ".join([job.title, " ".join(job.tags), job.text]))


def min_salary_for(location: str, filters: Filters) -> int | None:
    """První pravidlo z [[filtry.mista]], jehož text je v lokalitě, má přednost před min_mzda."""
    folded = fold(location)
    for rule in filters.places:
        if fold(rule.contains) in folded:
            return rule.min_salary
    return filters.min_salary


def check(job: Job, filters: Filters, today: date, max_age_days: int | None = None) -> str | None:
    """max_age_days: limit stáří pro tento inzerát (pipeline ho volí podle toho,
    jestli hledání už mělo ostrý běh). None = použij filters.max_age_days."""
    title = fold(job.title)
    for key in filters.reject_title_words:
        variant = find_variant(title, key)
        if variant:
            return f"název obsahuje „{variant}“"
    text = full_text(job)
    for key in filters.reject_words:
        variant = find_variant(text, key)
        if variant:
            return f"obsahuje „{variant}“"

    minimum = min_salary_for(job.location, filters)
    if minimum:
        monthly = monthly_upper(parse_salary(job.salary_text), filters.hours_per_month)
        if monthly is not None and monthly < minimum:
            return f"mzda {job.salary_text} je pod minimem {format_czk(minimum)} Kč"

    limit = max_age_days if max_age_days is not None else filters.max_age_days
    if limit and job.posted:
        age = (today - job.posted).days
        if age > limit:
            return f"starý inzerát ({age} dní, limit {limit})"

    return None
