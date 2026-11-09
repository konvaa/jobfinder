"""`jobagent vysvetli <URL nebo uložený soubor>` – proč má inzerát takové skóre.

Vypíše zdroj textu (JSON-LD / selektory / hlavní text / jen výpis), co se
z textu odstranilo, samotný text, výsledek filtrů a každé započítané slovo
s místem, body a úryvkem, kde se našlo. Nakonec skóre a sekci e-mailu.

Proč samostatný modul a ne kus cli.py: výsledek je obyčejný text, který jde
testovat bez sítě a bez konzole. CLI jen obstará HTML a vypíše ho.

Používá přesně stejné funkce jako denní běh (parse_detail, filters.check,
scoring.hits, pipeline.classify), jinak by vysvětlení mohlo lhát.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from bs4 import BeautifulSoup

from jobagent import collectors, filters, pipeline, salary, scoring
from jobagent.cleaning import clean_description
from jobagent.config import Config
from jobagent.detail import extract_detail
from jobagent.models import Job
from jobagent.text import clean, fold, fold_with_map

METHOD_LABELS = {
    "json-ld": "JSON-LD (strukturovaná data schema.org/JobPosting)",
    "selektory": "CSS selektory portálu",
    "hlavni-text": "hlavní text stránky (obecná heuristika)",
    "bez-textu": "JEN VÝPIS – stránka text inzerátu nemá (JavaScript) nebo na ní není název pozice",
}
SECTION_LABELS = {
    "text": "hlavní tabulka",
    "title": "Zajímavé podle názvu – bez popisu",
    "no_text_other": "Ostatní (bez popisu)",
    "pending": "čeká na detail",
}


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for el in (soup.find("h1"), soup.title):
        if el and clean(el.get_text(" ")):
            return clean(el.get_text(" "))
    return ""


def explain(html: str, url: str, cfg: Config, today: date, listing: Job | None = None,
            full_text: bool = False, text_limit: int = 2500) -> str:
    collector = collectors.for_url(url)
    out: list[str] = []

    if listing is not None:
        job = replace(listing, tags=list(listing.tags))
        origin = "data z výpisu: ano (z databáze posledního běhu)"
    else:
        native_id = (collector.native_id_from_url(url) if collector else None) or "?"
        job = Job(portal=collector.name if collector else "?", native_id=native_id, url=url, title=page_title(html))
        origin = "data z výpisu: NE – inzerát není v databázi, název je z nadpisu stránky, štítky chybí"

    detail = (collector.parse_detail(html, today, job.title) if collector
              else extract_detail(html, None, today, job.title))
    pipeline._apply_detail(job, detail)
    if job.hidden_salary and salary.has_visible_salary(job):
        job.hidden_salary = None  # detail mzdu ukazuje → hlídá ji náš filtr, stejně jako v denním běhu

    out += [
        "=== Inzerát ===",
        f"Název:  {job.title}",
        f"Firma:  {job.company or '—'}",
        f"Místo:  {job.location or '—'}",
        f"Mzda:   {job.salary_text or (job.hidden_salary.label + ' (z posledního běhu)' if job.hidden_salary else 'neuvedeno')}",
        f"Štítky: {', '.join(job.tags) or '—'}",
        f"URL:    {url}",
        f"Portál: {collector.name if collector else 'neznámý (obecná vrstva bez selektorů)'} · {origin}",
        "",
        f"=== Zdroj textu: {METHOD_LABELS.get(detail.method, detail.method)} ===",
    ]
    removed = clean_description(job.description)[1]
    if removed:
        out.append(f"Odstraněno z textu ({len(removed)} řádků, nepatří k popisu pozice):")
        out += [f"  - {_short(line, 160)}" for line in removed]
    out.append("")

    text = job.text
    out.append(f"=== Text ({len(text)} znaků) ===")
    if not text:
        out.append("(žádný – hodnotí se jen název a štítky z výpisu)")
    elif full_text or len(text) <= text_limit:
        out.append(text)
    else:
        out.append(text[:text_limit])
        out.append(f"… (zkráceno, celý text: přidej --cely-text)")
    out.append("")

    reason = filters.check(job, cfg.filters, today)
    out.append("=== Filtry ===")
    out.append(f"VYŘAZEN: {reason}" if reason else "prošel (vyradit, mzda, stáří)")
    if listing is None and job.posted:
        # Detail (JSON-LD) mívá datum PRVNÍHO zveřejnění, výpis datum aktualizace.
        # Denní běh bere výpis, takže tady může inzerát vyjít „starší“, než ho vidí běh.
        out.append(f"Pozn.: datum {job.posted:%d. %m. %Y} je z detailu (obvykle první zveřejnění). "
                   "Denní běh bere datum aktualizace z výpisu, takže stáří může vyjít jinak.")
    if cfg.filters.max_age_first_run and not job.posted:
        pass
    elif job.posted and not reason:
        out.append(f"Zveřejněno/aktualizováno: {job.posted:%d. %m. %Y} "
                   f"(limit {cfg.filters.max_age_days} dní, v prvním běhu hledání {cfg.filters.max_age_first_run})")
    out.append("")

    found = scoring.hits(job, cfg.scoring)
    texts = scoring.place_texts(job)
    out.append("=== Body ===")
    if not found and not scoring.extra_points(job, cfg.scoring):
        out.append("žádné klíčové slovo")
    for hit in found:
        out.append(f"  {hit.points:+4d}  {hit.variant}  ({hit.place})  {_snippet(texts[hit.place], hit)}")
    extra = scoring.extra_points(job, cfg.scoring)
    for points, reason_text in extra:
        out.append(f"  {points:+4d}  {reason_text} (podle hledání se mzdou na {' a '.join(job.hidden_salary.portals)})")
    total = sum(h.points for h in found) + sum(p for p, _ in extra)
    job.score = total
    job.title_score = sum(h.points for h in found if h.place == scoring.PLACE_TITLE) + sum(p for p, _ in extra)
    out.append("")

    out.append("=== Výsledek ===")
    out.append(f"Skóre: {total} · body v názvu: {job.title_score}")
    if reason:
        out.append("Sekce e-mailu: vyřazené")
    else:
        group = pipeline.classify(job)
        section = SECTION_LABELS[group]
        threshold = cfg.summary.score_threshold
        if group == "text" and threshold is not None and total < threshold:
            section = f"Ostatní (skóre pod hranicí {threshold})"
        if group == "text" and cfg.scoring.min_score is not None and total < cfg.scoring.min_score:
            section = f"vyřazené (nízké skóre, min_skore {cfg.scoring.min_score})"
        out.append(f"Sekce e-mailu: {section}")
    return "\n".join(out)


def _snippet(text: str, hit: scoring.Hit, width: int = 50) -> str:
    """Úryvek kolem nálezu s původní diakritikou, nalezené místo v »…«."""
    folded, index = fold_with_map(text)
    source = clean(text)
    if folded != fold(text) or hit.end > len(index):
        source, start, end = folded, hit.start, hit.end  # pojistka: ukázat aspoň text bez diakritiky
    else:
        start, end = index[hit.start], index[hit.end - 1] + 1
    before = source[max(0, start - width):start].replace("\n", " ")
    after = source[end:end + width].replace("\n", " ")
    return f"…{before}»{source[start:end]}«{after}…"


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
