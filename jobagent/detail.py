"""Obecná vrstva pro detail inzerátu – hlavní cesta pro všechny portály.

Pořadí pokusů:
  1. JSON-LD schema.org/JobPosting – strukturovaná data pro Google Jobs,
     mění se nejméně často (žádný z prvních tří portálů je zatím nemá,
     ale budoucí portály a firemní kariérní weby často ano),
  2. CSS selektory, které portál volitelně nadeklaruje (DetailSelectors),
  3. vytažení hlavního textu stránky heuristikou.

Proč takhle: portálů bude hodně a každý nový má stát co nejméně kódu.
Collector proto řeší hlavně výpis (odkazy, stránkování). U detailu stačí
nic nedělat, nebo dodat pár selektorů. Pro bodování klíčovými slovy i pro
AI stačí „rozumně čistý text“, nepotřebujeme přesnou strukturu.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, Tag

from jobagent.dates import parse_czech_date
from jobagent.cleaning import clean_description  # noqa: F401  (zpětná kompatibilita importu)
from jobagent.text import clean, fold

log = logging.getLogger(__name__)

MAX_DESCRIPTION = 15_000  # delší text nic nepřidá a zbytečně by zvětšoval DB i AI dotaz

# Prvky, které skoro nikdy nenesou text inzerátu: navigace, patičky, modální
# okna („pošlete si nabídku e-mailem“), tlačítka, formuláře.
_BOILERPLATE_TAGS = [
    "script", "style", "noscript", "svg", "template", "iframe",
    "nav", "header", "footer", "form", "dialog", "button", "aside",
]
_BOILERPLATE_SELECTORS = (
    '[role="navigation"], [role="dialog"], [role="contentinfo"], [aria-hidden="true"], [hidden], '
    # Patičky a cookie lišty často nejsou v sémantickém <footer>, ale v divu
    # s id/třídou „footer“ (např. #almc-footer na kariérních webech Alma Career).
    '[id*="footer" i], [class*="footer" i], [id*="cookie" i], [class*="cookie" i]'
)

# Méně textu po očištění = stránka text inzerátu nemá. Typicky ho dokresluje
# JavaScript (kariérní weby *.jobs.cz/detail-pozice), urllib dostane jen kostru.
# Takový „popis“ by do bodování i do AI vnášel jen šum z kostry stránky.
MIN_TEXT = 300



@dataclass(frozen=True)
class DetailSelectors:
    """Volitelné CSS selektory pro detail na konkrétním portálu.

    text: selektory, jejichž text se spojí do popisu (v daném pořadí).
    Ostatní pole: když jsou vyplněná a najdou se, doplní údaje, které ve
    výpisu chyběly (třeba mzdu).
    """

    text: tuple[str, ...] = ()
    salary: str | None = None
    company: str | None = None
    location: str | None = None
    posted: str | None = None


@dataclass
class DetailResult:
    description: str = ""  # surový text, čistí se až při použití (Job.text)
    salary_text: str = ""
    company: str = ""
    location: str = ""
    posted: date | None = None
    # "json-ld" | "selektory" | "hlavni-text" | "bez-textu" – kvůli logu, ukládání
    # neznámých stránek a poznámce v e-mailu
    method: str = ""


def extract_detail(html: str, selectors: DetailSelectors | None, today: date, title: str | None = None) -> DetailResult:
    """title: název pozice z výpisu. Slouží ke kontrole, že hlavní text je opravdu inzerát.

    Vrací SUROVÝ text. Čistí se až při použití (Job.text, modul cleaning),
    aby se lepší pravidla čištění projevila i na textu uloženém v DB.
    """
    return _extract(html, selectors, today, title)


def _extract(html: str, selectors: DetailSelectors | None, today: date, title: str | None) -> DetailResult:
    soup = BeautifulSoup(html, "html.parser")

    result = _from_json_ld(soup, today)
    if result and len(result.description) > 200:
        return result

    if selectors and selectors.text:
        from_selectors = _from_selectors(soup, selectors, today)
        if from_selectors.description:
            return _merge(result, from_selectors)

    text, page_text = _main_text(soup)
    # Stránka, na které název pozice vůbec není, nenese tento inzerát, jen obsah
    # kolem něj (kariérní web CSG vrátil „Naše vize a mise“, inzerát dotahuje
    # JavaScript). Hledá se na celé očištěné stránce, ne jen v nejlepším bloku:
    # nadpis <h1> bývá vedle bloku s textem, ne uvnitř.
    if len(text) < MIN_TEXT or (title and not title_present(title, page_text)):
        return _merge(result, DetailResult(description="", method="bez-textu"))
    return _merge(result, DetailResult(description=text, method="hlavni-text"))


def title_present(title: str, text: str) -> bool:
    """Je název pozice (aspoň 60 % jeho slov se 3+ znaky) v textu?

    Ne doslovná shoda: weby název zkracují nebo mění rod („Účetní/ý“), proto
    stačí většina slov. Kratší slova („IT“, „a“) by dávala falešné shody.
    """
    from jobagent.dedup import normalize_title  # lokálně: dedup → text, ne naopak

    words = [w for w in normalize_title(title).split() if len(w) >= 3]
    if not words:
        return True
    folded = fold(text)
    return sum(1 for w in words if w in folded) >= 0.6 * len(words)


# ---------------------------------------------------------------- JSON-LD


def _from_json_ld(soup: BeautifulSoup, today: date) -> DetailResult | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue  # weby mívají v JSON-LD chyby; jeden rozbitý blok nesmí shodit celý detail
        for item in _walk_json_ld(data):
            if _is_job_posting(item):
                return _job_posting_to_result(item, today)
    return None


def _walk_json_ld(data):
    """JSON-LD bývá objekt, seznam objektů nebo {"@graph": [...]}. Projdeme vše."""
    if isinstance(data, list):
        for x in data:
            yield from _walk_json_ld(x)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _walk_json_ld(data["@graph"])


def _is_job_posting(item: dict) -> bool:
    kind = item.get("@type")
    return kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind)


def _job_posting_to_result(item: dict, today: date) -> DetailResult:
    description_html = item.get("description") or ""
    description = _html_to_text(BeautifulSoup(description_html, "html.parser")) if description_html else ""

    org = item.get("hiringOrganization")
    company = org.get("name", "") if isinstance(org, dict) else (org if isinstance(org, str) else "")

    location = ""
    places = item.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    for place in places:
        if isinstance(place, dict):
            address = place.get("address")
            if isinstance(address, dict) and address.get("addressLocality"):
                location = address["addressLocality"]
                break

    posted = parse_czech_date(str(item.get("datePosted") or ""), today)
    return DetailResult(
        description=description[:MAX_DESCRIPTION],
        salary_text=_salary_from_json_ld(item.get("baseSalary")),
        company=clean(str(company)),
        location=clean(str(location)),
        posted=posted,
        method="json-ld",
    )


def _salary_from_json_ld(base: object) -> str:
    """Převede baseSalary na text, který pak zpracuje společný salary.parse_salary()."""
    if not isinstance(base, dict):
        return ""
    currency = base.get("currency", "")
    value = base.get("value")
    unit = ""
    if isinstance(value, dict):
        unit = value.get("unitText", "")
        low, high = value.get("minValue"), value.get("maxValue")
        single = value.get("value")
    else:
        low = high = None
        single = value
    unit_text = {"HOUR": "/hod", "MONTH": "/měsíc", "YEAR": "/rok"}.get(str(unit).upper(), "")
    if low and high:
        return f"{low} – {high} {currency}{unit_text}"
    if single or low or high:
        return f"{single or low or high} {currency}{unit_text}"
    return ""


# ---------------------------------------------------------------- selektory


def _from_selectors(soup: BeautifulSoup, sel: DetailSelectors, today: date) -> DetailResult:
    parts: list[str] = []
    seen: set[str] = set()
    for css in sel.text:
        for el in soup.select(css):
            text = _html_to_text(el)
            if text and text not in seen:  # stejný blok bývá na stránce dvakrát (nahoře a dole)
                seen.add(text)
                parts.append(text)

    def first_text(css: str | None) -> str:
        if not css:
            return ""
        el = soup.select_one(css)
        return clean(el.get_text(" ")) if el else ""

    return DetailResult(
        description="\n\n".join(parts)[:MAX_DESCRIPTION],
        salary_text=first_text(sel.salary),
        company=first_text(sel.company),
        location=first_text(sel.location),
        posted=parse_czech_date(first_text(sel.posted), today) if sel.posted else None,
        method="selektory",
    )


# ---------------------------------------------------------------- hlavní text


def _main_text(soup: BeautifulSoup) -> tuple[str, str]:
    """Zjednodušená obdoba „Readability“: najdi kontejner s nejvíc souvislým textem.

    Každý odstavec/odrážka přidá svou délku rodiči a polovinu prarodiči.
    Vyhraje prvek, ve kterém se sešlo nejvíc textu. Když je vítěz moc malý
    (méně než čtvrtina stránky), stránka má text rozházený a bereme
    raději celý očištěný obsah. Vrací (hlavní text, celý očištěný text stránky).
    """
    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()
    for tag in soup.select(_BOILERPLATE_SELECTORS):
        tag.decompose()

    body = soup.body or soup
    scores: dict[int, int] = {}
    nodes: dict[int, Tag] = {}
    for block in body.find_all(["p", "li", "h2", "h3", "dd", "td"]):
        length = len(clean(block.get_text(" ")))
        if length < 25:
            continue
        parent = block.parent
        if isinstance(parent, Tag):
            # Klíčem je id(), protože bs4 Tag porovnává obsah, ne identitu,
            # a dva stejné prvky by se v dict slily.
            scores[id(parent)] = scores.get(id(parent), 0) + length
            nodes[id(parent)] = parent
            grand = parent.parent
            if isinstance(grand, Tag):
                scores[id(grand)] = scores.get(id(grand), 0) + length // 2
                nodes[id(grand)] = grand

    body_text = _html_to_text(body)
    if scores:
        best = nodes[max(scores, key=scores.__getitem__)]
        best_text = _html_to_text(best)
        if len(best_text) >= 0.25 * len(body_text):
            return best_text[:MAX_DESCRIPTION], body_text
    return body_text[:MAX_DESCRIPTION], body_text


def _html_to_text(el) -> str:
    """Text s rozumnými zalomeními řádků (odstavce a odrážky zůstanou čitelné)."""
    text = el.get_text("\n")
    lines = (clean(line) for line in text.splitlines())
    joined = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", joined)


def _merge(primary: DetailResult | None, secondary: DetailResult) -> DetailResult:
    """Doplní prázdná pole z JSON-LD (např. datum) do výsledku z jiné metody."""
    if primary is None:
        return secondary
    return DetailResult(
        description=secondary.description or primary.description,
        salary_text=primary.salary_text or secondary.salary_text,
        company=primary.company or secondary.company,
        location=primary.location or secondary.location,
        posted=primary.posted or secondary.posted,
        method=secondary.method if secondary.description else primary.method,
    )
