"""Normalizace textu a hledání klíčových slov.

Proč vlastní modul: filtry, bodování i deduplikace musí porovnávat text
stejně. Kdyby každý normalizoval jinak, „Směny“ by se v jednom místě našly
a v jiném ne.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Znaky, které portály vkládají do textu kvůli typografii (jobs.cz má
# v mzdě „40 000 ‍–‍ 60 000“ se zero-width joinery). Pro regexy jsou neviditelný šum.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def clean(text: str) -> str:
    """Odstraní neviditelné znaky a sjednotí mezery (včetně NBSP) na jednu."""
    text = text.translate(_INVISIBLE)
    return re.sub(r"\s+", " ", text).strip()


def fold(text: str) -> str:
    """Malá písmena bez diakritiky: „Směnný provoz“ → „smenny provoz“.

    Proč bez diakritiky: inzeráty i uživatelé v configu ji píšou nekonzistentně
    (agentury často „smeny“). casefold() místo lower() kvůli správnému
    chování u speciálních znaků.
    """
    decomposed = unicodedata.normalize("NFKD", clean(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


# Úvodní hvězdička jen u slov s aspoň tolika znaky. „*it*“ by našlo
# „digitalizace“, „kvalita“, „profit“… a bodování by ztratilo smysl.
MIN_LEADING_WILDCARD = 4


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """fold() + mapa: pro každý znak výsledku index znaku v clean(text).

    Proč: hledá se ve fold()-nutém textu, ale úryvek pro člověka (příkaz
    vysvetli) má být s diakritikou. Většina znaků se složí 1:1, ale např.
    „…“ se rozpadne na tři tečky a posunula by se pozice.
    """
    source = clean(text)
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(source):
        decomposed = unicodedata.normalize("NFKD", ch)
        folded = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
        out.append(folded)
        index.extend([i] * len(folded))
    return "".join(out), index


def variants(key: str) -> list[str]:
    """Skupina synonym „a | b | c“ → ["a", "b", "c"]. Obyčejné slovo → [slovo].

    Proč skupiny: synonyma („home office“, „hybridní“, „remote“) se jinak
    sčítají a jeden inzerát s třemi formulacemi dostal trojnásobek bodů.
    Skupina se v inzerátu počítá nejvýš jednou.
    """
    return [v.strip() for v in key.split("|") if v.strip()]


def wildcard_problem(keyword: str) -> str | None:
    """Vrátí popis problému se zápisem slova, nebo None, když je v pořádku."""
    parts = keyword.strip().split("*")
    if not "".join(parts).strip():
        return "prázdné slovo"
    if keyword.strip().startswith("*"):
        first = next((p for p in parts if p), "")
        if len(first.strip()) < MIN_LEADING_WILDCARD:
            return (f"hvězdička na začátku je povolená jen u slov s aspoň {MIN_LEADING_WILDCARD} znaky "
                    f"(„{first.strip()}“ by se našlo uvnitř spousty nesouvisejících slov)")
    return None


@lru_cache(maxsize=1024)
def keyword_regex(keyword: str) -> re.Pattern[str]:
    """Regex pro klíčové slovo z configu, aplikovaný na fold()-nutý text.

    Pravidla (popsaná i v configu):
      * hledá se celé slovo – „it“ nenajde „digitalizace“,
      * hvězdička = libovolný zbytek slova, a to kdekoli:
          „směn*“ najde „směny“, „směnný“ (čeština skloňuje),
          „*směn*“ najde i „dvousměnný“ (složeniny; délku hlídá wildcard_problem),
          „*pružn* pracovní dob*“ najde „pružnou pracovní dobu“ i „Pružná pracovní doba“,
      * hranice slova se kontroluje jen u písmene/číslice, takže fungují
        i „c#“ a „.net“ (u „ASP.NET“ před tečkou je písmeno, a přesto má sedět).

    Pozn.: \\w v Pythonu 3 (str regex) zahrnuje i písmena s diakritikou, ale
    text je stejně předem fold()-nutý, takže na tom nezáleží.
    """
    kw = fold(keyword)
    parts = kw.split("*")
    # Mezi úseky i na krajích, kde byla hvězdička, je \w* = „libovolný zbytek slova“.
    body = r"\w*".join(re.escape(part) for part in parts)
    core = kw.strip("*")
    prefix = "" if kw.startswith("*") else (r"(?<!\w)" if core[:1].isalnum() else "")
    suffix = "" if kw.endswith("*") else (r"(?!\w)" if core[-1:].isalnum() else "")
    return re.compile(prefix + body + suffix)


def find_variant(folded_text: str, key: str) -> str | None:
    """První varianta ze skupiny (nebo samotné slovo), která se v textu najde."""
    for variant in variants(key):
        if keyword_regex(variant).search(folded_text):
            return variant
    return None


def contains_keyword(folded_text: str, key: str) -> bool:
    return find_variant(folded_text, key) is not None


def format_czk(amount: int | float) -> str:
    """40000 → „40 000“ (česká mezera mezi tisíci)."""
    return f"{int(round(amount)):,}".replace(",", " ")
