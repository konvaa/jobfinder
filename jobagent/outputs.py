"""Soubory ve složce vystup/: uložené souhrny, dohledání posledního a úklid starých.

Každý běh (ostrý i --dry-run) uloží dvojici se stejným jménem:
  email_RRRR-MM-DD_HHMMSS.html  celý souhrn k otevření v prohlížeči (sbalitelná „Ostatní“)
  email_RRRR-MM-DD_HHMMSS.eml   přesně ta zpráva, která odešla / odešla by: tělo,
                                textová verze i příloha (otevře Outlook i Thunderbird)
Neodeslaný e-mail má navíc příponu _NEODESLANO, aby byl ve složce vidět.

Proč .eml a ne jen HTML: HTML je obsah přílohy, ne tělo, které viděl příjemce
(to je u velkého souhrnu zkrácené a bez <details>). Problém „sekce v poště
chybí“ šel ověřit jen u .eml. `test-email --nahled` posílá právě .eml.

Úklid a ukládání jsou v jednom modulu, protože musí znát stejné vzory jmen:
mazat se smí jen to, co program sám vyrobil, nikdy cizí soubor, který si
uživatel do vystup/ odložil.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "email_"
UNSENT_SUFFIX = "_NEODESLANO"
# Jen tyhle vzory se smí mazat. Nic dalšího ve vystup/ úklid nevidí.
OWN_PATTERNS = (f"{PREFIX}*.html", f"{PREFIX}*.eml")


def save_summary(msg: EmailMessage, summary_html: str, out_dir: Path, now: datetime | None = None,
                 *, unsent: bool = False) -> tuple[Path, Path]:
    """Uložit celý souhrn (.html) a zprávu (.eml) pod stejným jménem. Vrací (html, eml)."""
    now = now or datetime.now()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{PREFIX}{now:%Y-%m-%d_%H%M%S}" + (UNSENT_SUFFIX if unsent else "")
    html_path, eml_path = out_dir / f"{stem}.html", out_dir / f"{stem}.eml"
    html_path.write_text(summary_html, encoding="utf-8")
    eml_path.write_bytes(msg.as_bytes())
    return html_path, eml_path


def latest(out_dir: Path, suffix: str) -> Path | None:
    """Nejnovější vlastní soubor s danou příponou (".eml" / ".html"), podle času změny."""
    if not out_dir.is_dir():
        return None
    files = [path for path in out_dir.glob(f"{PREFIX}*{suffix}") if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime, default=None)


def cleanup(out_dir: Path, keep_days: int, now: datetime | None = None) -> int:
    """Smazat vlastní soubory starší než keep_days dní. 0 = nemazat. Vrací počet smazaných.

    Podle času změny souboru (otevření ho nemění), ne podle data ve jméně:
    soubor přejmenovaný nebo zkopírovaný ručně se tak neposoudí podle
    jména, které už nemusí odpovídat.
    """
    if keep_days <= 0 or not out_dir.is_dir():
        return 0
    limit = ((now or datetime.now()) - timedelta(days=keep_days)).timestamp()
    removed = 0
    for pattern in OWN_PATTERNS:
        for path in out_dir.glob(pattern):  # glob bez ** = jen přímo ve složce, ne v podsložkách
            try:
                if path.is_file() and path.stat().st_mtime < limit:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                # Typicky soubor otevřený v Outlooku. Nevadí, zkusí se příště.
                log.warning("Starý soubor %s se nepodařilo smazat (%s), zkusím to příště.", path.name, exc)
    if removed:
        log.info("Smazáno %d souborů starších než %d dní z %s.", removed, keep_days, out_dir)
    return removed
