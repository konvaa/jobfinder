"""Anonymizace skutečných ukázek portálů pro veřejný repozitář.

Originály (celé stránky uložené příkazem `jobagent sample`) leží v
samples/private/ a do gitu nejdou: obsahují plné texty inzerátů, jména
a kontakty personalistů a lokalitu autorova hledání. Tento skript z nich
vyrobí kopie do samples/, které:

  * zachovají STRUKTURU HTML, kterou čtou parsery: tagy, třídy, data-testid,
    JSON-LD (kromě textu popisu), odkazy stránkování, názvy pozic, firmy
    a místa (veřejné firemní údaje),
  * nahradí text inzerátů neutrální výplní podobné délky. Zůstanou jen
    nadpisy sekcí a fráze z KEEP_PHRASES, na které se ptají testy,
  * nahradí jména, telefony a e-maily smyšlenými,
  * změní lokalitu hledání (na Brno) a částku filtru mzdy (na 50 000) podle
    soukromé mapy náhrad samples/private/anonymize_map.json,
  * zahodí skripty a styly (stovky kB). Data, která parser prace.cz čte
    z vloženého JSON (dvojice ID a data zveřejnění), zachová doslovně.

Že anonymizace nerozbila strukturu, hlídá tests/test_anonymized_samples.py:
kopie se musí parsovat stejně jako originál (když je originál k dispozici).

Použití (z kořene projektu):
    python tools/anonymize_samples.py          # všechny ukázky z SPEC
    python tools/anonymize_samples.py --check  # jen kontrola zbytků, nic nezapisuje
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "samples"
PRIVATE = PUBLIC / "private"

# (originál v samples/private/, veřejná kopie v samples/, druh stránky)
SPEC = [
    ("jobs_cz_vypis_zkraceny.html", "jobs_cz_vypis_zkraceny.html", "jobs_listing"),
    ("jobs_cz_beta_vypis_presmerovani.html", "jobs_cz_beta_vypis_presmerovani.html", "jobs_listing"),
    ("jobs_cz_vypis_prazdna_nekonzistentni.html", "jobs_cz_vypis_prazdna_nekonzistentni.html", "jobs_listing"),
    ("prace_cz_vypis_python.html", "prace_cz_vypis_python.html", "prace_listing"),
    ("prace_cz_vypis_python_se_mzdou.html", "prace_cz_vypis_python_se_mzdou.html", "prace_listing"),
    ("prace_cz_vypis_it_strana2.html", "prace_cz_vypis_it_strana2.html", "prace_listing"),
    ("prace_cz_detail_vertiflex.html", "prace_cz_detail_vertiflex.html", "prace_detail"),
    ("prace_cz_detail_firma_onsemi.html", "prace_cz_detail_firma_onsemi.html", "prace_detail"),
    ("jobscontact_vypis_it_40km.html", "jobscontact_vypis_it_40km.html", "jobscontact_listing"),
    ("jobscontact_vypis_it_40km_strana2.html", "jobscontact_vypis_it_40km_strana2.html", "jobscontact_listing"),
    ("jobscontact_vypis_it_40km_posledni.html", "jobscontact_vypis_it_40km_posledni.html", "jobscontact_listing"),
    ("jobscontact_detail_it_support.html", "jobscontact_detail_it_support.html", "jobscontact_detail"),
    ("jobscontact_detail_vice_lokalit.html", "jobscontact_detail_vice_lokalit.html", "jobscontact_detail"),
]

# Bloky s textem inzerátu podle druhu stránky. Text uvnitř se nahradí výplní.
AD_TEXT = {
    "prace_detail": ['[class$="_RichContent"]'],
    "jobscontact_detail": [".new-detail .job-content .content > p", ".new-detail .job-content .content > .uk-grid"],
    "jobscontact_listing": [".job-box a.job-box-left > p"],
}
# Mimo bloky inzerátu se nahradí jen dlouhé texty (upoutávky na blog, souhlasy…).
LONG_TEXT = 150
# Šablona agentury (pošlete CV + GDPR) zůstává: test čištění ověřuje, že ji parser NEVEZME.
KEEP_BLOCKS = [".next-info"]
# Fráze z textů inzerátů, na které se ptají testy. Zbytek uzlu se nahradí výplní.
KEEP_PHRASES = [
    "warehouse control system",
    "Do mezinárodní firmy z oblasti automotive",
    "L1 IT podporu",
    "Home office až 2 dny v týdnu",
]
# Krátký text v nadpisu nebo tučně („Náplň práce“) je struktura, ne obsah inzerátu.
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"}
HEADING_MAX = 40

FILLER_WORDS = "Ukázkový text inzerátu nahrazený pro veřejný repozitář".split()

# Co přesně nahradit (lokalita autorova hledání, částka jeho filtru mzdy, jména
# personalistů) je SOUKROMÉ: seznam sám obsahuje to, co skrývá. Leží vedle
# originálů v samples/private/anonymize_map.json (není v gitu), formát:
#   {"labels": [["původní název lokality", "náhrada"], …],   # i procentově zakódované podoby
#    "replace": [["původní", "náhrada"], …],                  # kódy, souřadnice, slugy URL
#    "salary": [původní_částka, 50000],                       # částka v salary= / salaryMin=
#    "people": [["Jméno Příjmení", "Smyšlené Jméno"], …],
#    "forbidden": ["…"]}                                      # kontrola: nesmí zůstat (bez diakritiky)
MAP_FILE = PRIVATE / "anonymize_map.json"


def encoded_variants(old: str, new: str) -> list[tuple[str, str]]:
    """Dvojice (původní, náhrada) ve všech podobách kódování, které portály používají."""
    from urllib.parse import quote

    out = []
    for safe in ("", ",", ",/"):
        once_old, once_new = quote(old, safe=safe), quote(new, safe=safe)
        out += [(once_old, once_new), (once_old.replace("%20", "+"), once_new.replace("%20", "+")),
                (quote(once_old, safe=""), quote(once_new, safe=""))]
    return [(o, n) for o, n in dict(out).items() if o != old]


def load_map() -> dict:
    if not MAP_FILE.exists():
        return {"labels": [], "replace": [], "salary": None, "people": [], "forbidden": []}
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    labels = [tuple(p) for p in data.get("labels", [])]
    # Pořadí je důležité: delší a zakódované podoby dřív než obecné jméno obce.
    replace = labels + [pair for old, new in labels for pair in encoded_variants(old, new)]
    replace += [tuple(p) for p in data.get("replace", [])]
    salary = data.get("salary")
    return {
        "replace": replace,
        # Bez \b na začátku: v zakódovaných odkazech je před názvem „%26“ („…%26salary%3D…“).
        "salary": [(re.compile(rf"(?<![A-Za-z])(salary(?:Min)?(?:=|%3D|%253D)){int(salary[0])}(?!\d)"),
                    rf"\g<1>{int(salary[1])}")]
        if salary else [],
        "people": [tuple(p) for p in data.get("people", [])],
        "forbidden": [fold(w) for w in data.get("forbidden", [])],
    }


PHONE = re.compile(r"\+420[\s ]?\d{3}[\s ]?\d{3}[\s ]?\d{3}|\b\d{3}[\s ]\d{3}[\s ]\d{3}\b")
PHONE_PLACEHOLDER = "+420 000 000 000"
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?!png\b|jpe?g\b|svg\b|webp\b|gif\b)[a-z]{2,}\b")
EMAIL_PLACEHOLDER = "kontakt@example.com"

def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c)).lower()


def filler(length: int) -> str:
    """Neutrální výplň zhruba dané délky (bez slov, která by bodovala)."""
    words, out = FILLER_WORDS, []
    while sum(len(w) + 1 for w in out) < max(length, 12):
        out.append(words[len(out) % len(words)])
    return " ".join(out).rstrip() + "."


_MAP: dict | None = None


def private_map() -> dict:
    global _MAP
    if _MAP is None:
        _MAP = load_map()
    return _MAP


def map_text(text: str) -> str:
    """Náhrady lokality, částky a kontaktů. Používá ji i test shody s originálem."""
    m = private_map()
    for old, new in m["replace"]:
        text = text.replace(old, new)
    for rx, new in m["salary"]:
        text = rx.sub(new, text)
    for old, new in m["people"]:
        text = text.replace(old, new)
    text = PHONE.sub(PHONE_PLACEHOLDER, text)
    return EMAIL.sub(EMAIL_PLACEHOLDER, text)


def _replace_ad_text(node: NavigableString) -> None:
    text = str(node)
    if not text.strip():
        return
    kept = [p for p in KEEP_PHRASES if p in text]
    parent = node.parent.name if node.parent else ""
    if not kept and parent in HEADING_TAGS and len(text.strip()) <= HEADING_MAX:
        return
    new = " ".join(kept) + " " + filler(min(len(text.strip()) - sum(map(len, kept)), 300)) if kept \
        else filler(min(len(text.strip()), 300))
    node.replace_with(new)


def _anonymize_ld(script) -> None:
    try:
        data = json.loads(script.string or "")
    except json.JSONDecodeError:
        return
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("description"), str):
            desc = BeautifulSoup(item["description"], "html.parser")
            for node in list(desc.find_all(string=True)):
                _replace_ad_text(node)
            item["description"] = str(desc)
    script.string = json.dumps(data, ensure_ascii=False)


def anonymize(html: str, kind: str, source_name: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Původní hlavička „<!-- Zdroj: … -->“ nese adresu autorova hledání.
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    parser_data = []
    if kind == "prace_listing":
        from jobagent.collectors import alma_next  # stejné vzory jako parser

        parser_data = [m.group(0) for rx in (alma_next._ID_MAP, alma_next._META) for m in rx.finditer(html)]
    for tag in soup.find_all(["script", "style", "noscript", "template"]):
        if tag.name == "script" and (tag.get("type") or "").lower() == "application/ld+json":
            _anonymize_ld(tag)
        else:
            tag.decompose()
    for meta in soup.select('meta[name="description"], meta[property="og:description"], meta[name="twitter:description"]'):
        meta["content"] = filler(60)

    replaced: set[int] = set()
    for selector in AD_TEXT.get(kind, []):
        for block in soup.select(selector):
            for node in list(block.find_all(string=True)):
                if id(node) not in replaced:
                    _replace_ad_text(node)
            replaced.add(id(block))
    keep_blocks = [b for sel in KEEP_BLOCKS for b in soup.select(sel)]
    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment) or len(node.strip()) < LONG_TEXT:
            continue
        if node.parent is not None and node.parent.name == "script":  # JSON-LD: popis už je nahrazený
            continue
        if any(b in node.parents for b in keep_blocks):
            continue
        _replace_ad_text(node)

    if parser_data:
        data = soup.new_tag("script", type="application/json", id="anonymized-parser-data")
        # Každý záznam ukončit escapovanou uvozovkou jako v původním JSON, jinak by
        # hodnota „updated“ (vzor [^"\\]+) pokračovala přes konec řádku do dalšího záznamu.
        data.string = "".join(f'{m}\\"\n' for m in parser_data)
        (soup.body or soup).append(data)

    header = (f"<!-- Anonymizovaná ukázka (tools/anonymize_samples.py) z originálu {source_name}. "
              "Struktura HTML zachována; texty inzerátů, jména a kontakty nahrazeny; "
              "lokalita hledání změněna na Brno a částka filtru mzdy na 50 000; skripty a styly odstraněny. -->\n")
    return header + map_text(str(soup))


def leftovers(text: str) -> list[str]:
    folded = fold(text)
    found = [w for w in private_map()["forbidden"] if w in folded]
    found += [f"telefon {m}" for m in PHONE.findall(text) if m != PHONE_PLACEHOLDER]
    found += [f"e-mail {m}" for m in EMAIL.findall(text) if m != EMAIL_PLACEHOLDER]
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="jen zkontrolovat veřejné kopie, nic nezapisovat")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    problems = 0
    for source, target, kind in SPEC:
        out_path = PUBLIC / target
        if not args.check:
            src = PRIVATE / source
            if not MAP_FILE.exists():
                print(f"chybí {MAP_FILE.relative_to(ROOT)} (soukromá mapa náhrad), nic nezapisuji")
                return 1
            if not src.exists():
                print(f"přeskočeno (chybí originál): {src.relative_to(ROOT)}")
                continue
            text = anonymize(src.read_text(encoding="utf-8"), kind, source)
            out_path.write_text(text, encoding="utf-8")
            print(f"{target}: {src.stat().st_size // 1024} kB → {len(text.encode()) // 1024} kB")
        if out_path.exists():
            left = leftovers(out_path.read_text(encoding="utf-8"))
            if left:
                problems += 1
                print(f"  ZBYTKY v {target}: {sorted(set(left))[:10]}")
    print("bez zbytků" if not problems else f"{problems} souborů se zbytky")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
