"""Bodování podle klíčových slov – běží vždy a je zdarma.

Pravidla:
  * každé slovo i každá skupina synonym „a | b | c“ se započítá jednou,
    podle nejsilnějšího místa nálezu: název (body × nasobek_v_nazvu) > štítek > popis.
    U skupiny se prochází místa v tomto pořadí a bere se první varianta,
    která se na daném místě najde – ta se ukáže ve zdůvodnění,
  * [hodnoceni.slova_v_nazvu] se hledají jen v názvu, bez násobku,
  * záporné body fungují stejně (bývalé „penalizovat“),
  * DELŠÍ SHODA MÁ PŘEDNOST, a to pro každý výskyt zvlášť: výskyt kratšího
    zápisu, který leží celý uvnitř výskytu delšího (jiného) zápisu, se
    nezapočítá. Výskyt kratšího zápisu jinde v textu platí normálně.
    Příklad: "občasné práce z domova" = 2 a skupina s "z domova" = 10.
    Štítek „Možnost občasné práce z domova“ dá jen 2, věta „3 dny v týdnu
    z domova“ dá 10. Bez pravidla by se obecné slovo uvnitř konkrétnější
    fráze započítalo znovu a slabý signál by se nedal odlišit.
    Platí jen pro bodování. Filtry vyradit se ničím nepotlačují.

Zdůvodnění u každé položky uvádí, KDE se slovo našlo. Bez toho nejde
poznat, jestli „+8 z domova“ znamená skutečný home office, nebo jen
obecný štítek portálu, a config by se ladil naslepo.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobagent.config import Scoring
from jobagent.models import Job
from jobagent.text import clean, fold, keyword_regex, variants

PLACE_TITLE = "název"
PLACE_TAG = "štítek"
PLACE_TEXT = "popis"


@dataclass(frozen=True)
class Hit:
    """Jeden započítaný zápis: kde se našel a kolik bodů dal.

    Pozice (start, end) míří do textu daného místa (název / štítky / popis)
    po clean(). Díky tomu `jobagent vysvetli` ukáže úryvek, kde se slovo našlo.
    """

    key: str
    variant: str
    place: str
    points: int
    start: int
    end: int

    @property
    def reason(self) -> str:
        return f"{self.points:+d} {self.variant} ({self.place})"


def found_variants(folded_text: str, keys: list[str]) -> dict[str, tuple[str, int, int]]:
    """Které zápisy se v textu najdou, s uplatněním pravidla „delší shoda má přednost“.

    Vrací {zápis: (nalezená varianta, začátek, konec)}. U skupiny se bere první
    varianta v pořadí z configu, která má aspoň jeden nepotlačený výskyt
    (a její první takový výskyt).
    """
    occurrences = []  # (začátek, konec, zápis, varianta)
    for key in keys:
        for variant in variants(key):
            for m in keyword_regex(variant).finditer(folded_text):
                occurrences.append((m.start(), m.end(), key, variant))

    def suppressed(start: int, end: int, key: str) -> bool:
        length = end - start
        return any(
            other_key != key and o_start <= start and end <= o_end and (o_end - o_start) > length
            for o_start, o_end, other_key, _ in occurrences
        )

    first_valid: dict[tuple[str, str], tuple[int, int]] = {}
    for start, end, key, variant in sorted(occurrences):
        if (key, variant) not in first_valid and not suppressed(start, end, key):
            first_valid[(key, variant)] = (start, end)
    found: dict[str, tuple[str, int, int]] = {}
    for key in keys:
        for variant in variants(key):
            if (key, variant) in first_valid:
                found[key] = (variant, *first_valid[(key, variant)])
                break
    return found


def place_texts(job: Job) -> dict[str, str]:
    """Texty jednotlivých míst po clean(). fold() na nich zachová pozice znaků
    (česká diakritika se rozloží a odstraní 1:1), takže pozice z regexu na
    fold()-nutém textu ukazují i do tohoto textu."""
    return {
        PLACE_TITLE: clean(job.title),
        PLACE_TAG: clean(" \n ".join(job.tags)),
        PLACE_TEXT: clean(job.text),  # očištěný popis, ne surový z DB
    }


def hits(job: Job, scoring: Scoring) -> list[Hit]:
    texts = {place: fold(text) for place, text in place_texts(job).items()}
    words = list(scoring.words)
    # V názvu soupeří o shodu i slova_v_nazvu (např. „it“ uvnitř „it support“).
    found = {
        PLACE_TITLE: found_variants(texts[PLACE_TITLE], words + list(scoring.title_words)),
        PLACE_TAG: found_variants(texts[PLACE_TAG], words),
        PLACE_TEXT: found_variants(texts[PLACE_TEXT], words),
    }
    result: list[Hit] = []
    for key, points in scoring.words.items():
        for place, multiplier in ((PLACE_TITLE, scoring.title_multiplier), (PLACE_TAG, 1), (PLACE_TEXT, 1)):
            if key in found[place]:
                variant, start, end = found[place][key]
                result.append(Hit(key, variant, place, points * multiplier, start, end))
                break
    for key, points in scoring.title_words.items():
        if key in found[PLACE_TITLE]:
            variant, start, end = found[PLACE_TITLE][key]
            result.append(Hit(key, variant, PLACE_TITLE, points, start, end))
    return result


HIDDEN_SALARY_REASON = "mzda skrytá, pravděpodobně pod minimem"


def extra_points(job: Job, scoring: Scoring) -> list[tuple[int, str]]:
    """Body mimo klíčová slova: (body, důvod). Zatím jen srážka za skrytou mzdu.

    Platí pro celkové skóre i pro body v názvu: podle bodů v názvu se řadí
    sekce bez popisu a srážka tam má působit stejně.
    """
    if job.hidden_salary and job.hidden_salary.below and scoring.hidden_salary_penalty:
        return [(scoring.hidden_salary_penalty, HIDDEN_SALARY_REASON)]
    return []


def score(job: Job, scoring: Scoring) -> tuple[int, list[str]]:
    found = hits(job, scoring)
    extra = extra_points(job, scoring)
    return (sum(h.points for h in found) + sum(p for p, _ in extra),
            [h.reason for h in found] + [f"{p:+d} {r}" for p, r in extra])


def title_points(job: Job, scoring: Scoring) -> int:
    """Body jen za slova nalezená v názvu (včetně záporných a násobku)
    plus srážka za skrytou mzdu (extra_points).

    Pro nabídky bez textu detailu je to jediné férové měřítko: celkové
    skóre jim chybí body z popisu, takže by vždy prohrávaly s ostatními.
    """
    return (sum(h.points for h in hits(job, scoring) if h.place == PLACE_TITLE)
            + sum(p for p, _ in extra_points(job, scoring)))
