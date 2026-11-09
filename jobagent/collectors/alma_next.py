"""Parser výpisu nové platformy Alma Career (Next.js) – prace.cz, beta.jobs.cz.

Proč obecný modul a ne kód přímo v prace_cz.py: beta.jobs.cz už běží na stejné
platformě. Až na ni jobs.cz přejde celý, stačí jeho collector přepnout na tenhle
parser (s jinou doménou), ne psát nový.

Co je na platformě stabilní (listopad 2026):
  * karta = article[id="advert-<uuid>"], UUID je stabilní ID inzerátu,
  * název a odkaz: [data-testid="advert-link"],
  * údaje karty mají skryté popisky pro čtečky obrazovky
    („Lokalita:“, „Název firmy:“, „Plat:“, „Typ úvazku:“) – podle nich čteme,
    protože CSS třídy jsou generované (ki5xOq_JobCard) a mění se s každým
    nasazením webu,
  * do stránky je vložený JSON (data pro Next.js a analytiku) s ID jobs.cz
    (item_id ↔ item_uuid) a přesnými datumy (publishedAt, updated).
    JSON je doplněk: když se změní nebo chybí, karta se zpracuje i bez něj.

Štítek v patičce karty („Včerejší“, „Méně než týden“…) je jen přibližné stáří.
Bere se, jen když v JSON datum není.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from jobagent.collectors.base import ListingPage
from jobagent.dates import parse_czech_date
from jobagent.models import Job
from jobagent.text import clean

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Uvozovky ve vloženém JSON bývají escapované (\"), proto \\? všude.
_Q = r'\\?"'
_ID_MAP = re.compile(rf'item_id{_Q}:{_Q}(\d+){_Q},{_Q}item_uuid{_Q}:{_Q}({_UUID})')
_META = re.compile(
    rf'advertId{_Q}:{_Q}({_UUID}){_Q},{_Q}publishedAt{_Q}:{_Q}([^"\\]+){_Q},'
    rf'{_Q}publishedTo{_Q}:{_Q}[^"\\]*{_Q},{_Q}updated{_Q}:{_Q}([^"\\]+)'
)
# Kariérní weby firem na jobs.cz: odkaz nese přímo ID jobs.cz.
_JOBS_CZ_PD = re.compile(r"^https?://[a-z0-9-]+\.jobs\.cz/pd/(\d+)")


def parse_listing(html: str, page_url: str, portal: str, today: date) -> ListingPage:
    soup = BeautifulSoup(html, "html.parser")
    jobs_cz_ids = {uuid: num for num, uuid in _ID_MAP.findall(html)}
    dates = {uuid: (published, updated) for uuid, published, updated in _META.findall(html)}

    jobs: list[Job] = []
    for card in soup.select('article[id^="advert-"]'):
        uuid = card["id"][len("advert-"):]
        link = card.select_one('[data-testid="advert-link"][href]')
        if link is None:
            continue
        href = urljoin(page_url, link["href"])
        labels = _labels(card)

        jobs_cz_id = jobs_cz_ids.get(uuid)
        if not jobs_cz_id:
            m = _JOBS_CZ_PD.match(href)
            jobs_cz_id = m.group(1) if m else None

        jobs.append(
            Job(
                portal=portal,
                native_id=uuid,
                url=_strip_tracking(href),
                title=clean(link.get_text(" ")),
                company=labels.get("nazev firmy", ""),
                location=labels.get("lokalita", ""),
                salary_text=labels.get("plat", ""),
                posted=_posted(uuid, dates, card, today),
                tags=_tags(card),
                # Stejný inzerát na jobs.cz má tohle ID → deduplikace bez stahování detailu.
                alt_key=f"jobs.cz:{jobs_cz_id}" if jobs_cz_id else "",
            )
        )

    return ListingPage(jobs=jobs, next_url=_next_page(soup, page_url))


def _labels(card: Tag) -> dict[str, str]:
    """{"lokalita": "Ostrava-Poruba", "nazev firmy": "…", "plat": "…"} podle skrytých popisků."""
    from jobagent.text import fold

    out: dict[str, str] = {}
    for hidden in card.select(".accessibility-hidden"):
        label = clean(hidden.get_text(" ")).rstrip(":").strip()
        parent = hidden.parent
        if not label or parent is None:
            continue
        value = clean(parent.get_text(" "))
        if value.startswith(clean(hidden.get_text(" "))):
            value = value[len(clean(hidden.get_text(" "))):].strip()
        out.setdefault(fold(label), value)
    return out


def _tags(card: Tag) -> list[str]:
    tags = []
    for li in card.select('[data-testid="search-results-item-highlights-part-two"] li'):
        text = clean(li.get_text(" "))
        if text:
            tags.append(text)
    return tags


def _posted(uuid: str, dates: dict, card: Tag, today: date) -> date | None:
    """Datum aktualizace z vloženého JSON (stejný význam jako „Aktualizováno“ na jobs.cz),
    jinak štítek z patičky karty (jen „Dnešní“/„Včerejší“ jsou přesné)."""
    if uuid in dates:
        published, updated = dates[uuid]
        for value in (updated, published):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                continue
    footer = card.find("footer")
    if footer:
        for li in footer.find_all("li"):
            parsed = parse_czech_date(clean(li.get_text(" ")), today)
            if parsed:
                return parsed
    return None


def _next_page(soup: BeautifulSoup, page_url: str) -> str | None:
    """Odkaz na stránku N+1, ať vypadá tlačítko jakkoli.

    Proč podle čísla stránky a ne podle tlačítka „Další“: jeho podobu jsme
    v ukázkách neviděli a generované třídy se mění. Parametr page=N je stabilní.
    """
    current = int((parse_qs(urlsplit(page_url).query).get("page") or ["1"])[0] or 1)
    wanted = str(current + 1)
    for a in soup.select("a[href*='page=']"):
        target = urljoin(page_url, a["href"])
        if (parse_qs(urlsplit(target).query).get("page") or [""])[0] == wanted:
            return target
    return None


def _strip_tracking(url: str) -> str:
    """Odstraní ?rps=… (sledování pozice ve výpisu). Ostatní parametry nechá,
    u kariérních webů může originJobAdUuid něco znamenat."""
    parts = urlsplit(url)
    query = [(k, v) for k, vs in parse_qs(parts.query).items() for v in vs if k != "rps"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
