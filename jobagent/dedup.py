"""Poznání stejného inzerátu na víc portálech (nebo dvakrát v jednom běhu).

Dvě úrovně, v tomto pořadí:
  1. Shoda ID: prace.cz u většiny inzerátů nese ID jobs.cz (Job.alt_key).
     Spolehlivé, tak se slučuje bez dalších podmínek.
  2. Shoda název + firma + OBEC: jen když aspoň jednomu z dvojice ID chybí.
     Obec je nutná, protože agentury mívají stejně pojmenované pozice
     v různých městech. Dva inzeráty, které oba mají ID a liší se, jsou
     podle portálu různé – neslučují se.

Zásada: při nejistotě NESLUČOVAT. Duplicita v e-mailu je menší zlo než
ztracený inzerát. Proto bez firmy nebo s nejasným místem („Česko“, kraj,
víc míst) klíč neexistuje a shoda podle názvu se nezkouší.

Slučuje se před stahováním detailů, takže se pro jeden inzerát nestahují
dva detaily. Přednost má portál výš v collectors.COLLECTORS (jobs.cz).
"""

from __future__ import annotations

import re

from jobagent.models import Job
from jobagent.text import fold

_GENDER = re.compile(r"/\s*(ka|ky|ice|kyne|na|ni)\b|\((m|z|m/z|z/m|f/m|m/f|m/w)\)|\b(m|z)/(z|m)\b")
_LEGAL = re.compile(r"\b(s\.?\s?r\.?\s?o\.?|spol\.|a\.?\s?s\.?|v\.?\s?o\.?\s?s\.?|k\.?\s?s\.?|z\.?\s?s\.?|"
                    r"gmbh|ltd\.?|limited|inc\.?|se|o\.?\s?p\.?\s?s\.?)(?=\s|$|,)")
_NON_WORD = re.compile(r"[^\w]+")
_MORE_PLACES = re.compile(r"\+\s*\d+\s*dals\w*\s*lokalit\w*")
# Místo, které není obec: celá republika nebo kraj (prace.cz: „Moravskoslezský“).
_NOT_A_TOWN = {
    "cesko", "ceska republika", "cr", "cela cr", "slovensko", "praha a okoli",
    "stredocesky", "jihocesky", "plzensky", "karlovarsky", "ustecky", "liberecky",
    "kralovehradecky", "pardubicky", "vysocina", "jihomoravsky", "olomoucky",
    "moravskoslezsky", "zlinsky",
}


# Mzda v názvu. jobs.cz u inzerátů agentur ji připojuje k názvu, na webu agentury
# chybí („Zkušený projektant | Strojírenství & průmysl | 50-70.000 Kč“ ×
# „Zkušený projektant | Strojírenství & průmysl“). Podoby ze skutečných názvů
# (8. 11. 2026, 94 inzerátů Jobs Contact): „(45.000 - 50.000 Kč)“, „| 50-70.000 Kč“,
# „(až 90.000,-)“, „- až 200 tis Kč“, „| 150 000kč“, „(mzda až 80.000 Kč)“,
# „(až 55.000 Kč + bonusy)“, „AŽ 85000,-“, „(až 280 Kč/hod.)“.
# Číslo bez „Kč“ ani „,-“ („13. plat“, „15. mzda“, „#3094“) mzdou NENÍ a zůstává.
_SALARY = re.compile(
    r"(?:\bmzda\s+)?(?:\b(?:od|az|do)\s+)?\d[\d\s.,]*(?:\s*[-–]\s*\d[\d\s.,]*)?"
    r"\s*(?:tis\.?|k\b)?\s*(?:kc\b|,-)(?:\s*/\s*hod\.?)?(?:\s*\+\s*bonusy)?"
)
# Závorka jen s rozpětím tisícovek bez měny: „(35.000 - 65.000)“.
_SALARY_RANGE_ONLY = re.compile(r"^\s*(?:od|az|do)?\s*\d[\d\s.,]*000(?:\s*[-–]\s*\d[\d\s.,]*000)?\s*$")
_PARENS = re.compile(r"\(([^()]*)\)")
# Spojka, která po odstranění mzdy zůstane na konci („Flexibilita, bonusy a až 90.000,-“).
_TRAILING_JOINER = re.compile(r"(?:\s+(?:a|az|i))+$")


def normalize_title(title: str) -> str:
    """Název pro porovnání: bez diakritiky, rodu, MZDY a interpunkce.

    Proč odstranit jen mzdu a ne vše za „|“: za svislítkem často stojí to,
    čím se inzeráty agentury liší (obor, technologie, místo). Ověřeno na
    skutečných párech jobs.cz × jobscontact.cz: „jen mzda“ = 43 shod bez
    falešných, „vše za |“ = 54 shod, ale i falešné („Elektro projektant |
    AutoCAD …“ ≈ „Elektro projektant | Měření a regulace …“).
    """
    t = fold(title)
    t = _GENDER.sub(" ", t)

    def parens(m: re.Match) -> str:
        inner = m.group(1)
        return " " if _SALARY.search(inner) or _SALARY_RANGE_ONLY.match(inner) else f" {inner} "

    t = _PARENS.sub(parens, t)
    t = _SALARY.sub(" ", t)
    t = _NON_WORD.sub(" ", t).strip()
    return _TRAILING_JOINER.sub("", t)


def normalize_company(company: str) -> str:
    c = fold(company)
    c = _LEGAL.sub(" ", c)
    return _NON_WORD.sub(" ", c).strip()


def town(location: str) -> str:
    """Obec z lokality: „Ostrava – Poruba“ i „Ostrava-Poruba“ → „ostrava“.

    Prázdný řetězec = nevíme (víc míst, kraj, republika, okres bez obce).
    jobscontact.cz uvádí okres („Ostrava-město“ → „ostrava“, „Přerov“ místo
    Hranic): shoda okres = obec je slabší, ale pořád obec + název + firma.
    Proč jen obec:
    portály píšou část obce různě (jobs.cz „Ostrava – Poruba“, prace.cz
    „Ostrava-Poruba“), obec se shoduje.
    """
    loc = fold(location)
    loc = _MORE_PLACES.sub("", loc).strip()
    loc = re.sub(r"^hlavni mesto\s+", "", loc)  # jobscontact.cz: „Hlavní město Praha“
    if not loc or "," in loc or loc.endswith("kraj"):
        return ""
    # Okresy, které nejsou obcí („Brno-venkov“, „Praha-východ“): obec v nich
    # neznáme. Bez toho by z nich řez na „-“ udělal „brno“ / „praha“.
    # „Ostrava-město“ / „Brno-město“ obec jsou, ty projdou dál.
    if re.search(r"-(?:venkov|vychod|zapad)$", loc):
        return ""
    first = re.split(r"\s+[–-]\s+|-", loc)[0].strip()
    if first in _NOT_A_TOWN or first.endswith(" kraj"):
        return ""
    return _NON_WORD.sub(" ", first).strip()


def dedup_key(title: str, company: str, location: str) -> str:
    """Klíč název|firma|obec, nebo "" když si nejsme jistí (pak se neslučuje)."""
    company_part = normalize_company(company)
    town_part = town(location)
    if not company_part or not town_part:
        return ""
    return f"{normalize_title(title)}|{company_part}|{town_part}"


def has_cross_id(job: Job) -> bool:
    """Má inzerát ID, podle kterého ho poznají i jiné portály?"""
    return bool(job.alt_key) or job.identity == job.key and job.portal == "jobs.cz"


def group_duplicates(jobs: list[Job], priority) -> list[tuple[Job, list[Job]]]:
    """Seskupí stejné inzeráty. Vrací [(hlavní verze, [duplicity])].

    jobs musí být v pořadí, v jakém je portály našly; priority(portal) → číslo,
    nižší = přednost. V rámci jednoho portálu má přednost první nalezený.
    """
    ordered = sorted(enumerate(jobs), key=lambda p: (priority(p[1].portal), p[0]))
    groups: list[tuple[Job, list[Job]]] = []
    by_identity: dict[str, int] = {}
    by_title: dict[str, list[int]] = {}

    for _, job in ordered:
        # 1) shoda ID
        index = by_identity.get(job.identity)
        # 2) shoda název + firma + obec, jen když jednomu z dvojice chybí ID
        if index is None:
            key = dedup_key(job.title, job.company, job.location)
            if key:
                for candidate in by_title.get(key, []):
                    primary = groups[candidate][0]
                    if not (has_cross_id(job) and has_cross_id(primary)):
                        index = candidate
                        break
        if index is None:
            groups.append((job, []))
            index = len(groups) - 1
            key = dedup_key(job.title, job.company, job.location)
            if key:
                by_title.setdefault(key, []).append(index)
        else:
            groups[index][1].append(job)
        by_identity.setdefault(job.identity, index)
    return groups


def merge_into(primary: Job, duplicate: Job) -> None:
    """Doplní do hlavní verze, co jí chybí (typicky mzdu, kterou ukazuje jen jeden portál).

    Odkaz, ID a historie zůstávají z hlavní verze.
    """
    primary.salary_text = primary.salary_text or duplicate.salary_text
    primary.company = primary.company or duplicate.company
    primary.location = primary.location or duplicate.location
    primary.posted = primary.posted or duplicate.posted
    for tag in duplicate.tags:
        if tag not in primary.tags:
            primary.tags.append(tag)
    # Odpověď filtru mzdy: „prošel“ v kterékoli verzi téhož portálu vyhrává
    # (viz pipeline._salary_pass). Různé portály zůstanou vedle sebe.
    for portal, passed in duplicate.salary_checks.items():
        primary.salary_checks[portal] = primary.salary_checks.get(portal, False) or passed
    if duplicate.portal != primary.portal and duplicate.portal not in primary.also_on:
        primary.also_on.append(duplicate.portal)
