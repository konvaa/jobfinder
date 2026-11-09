"""Čištění textu inzerátu: odstranění řádků, které nepatří k popisu pozice.

V DB se ukládá SUROVÝ text detailu a čistí se až při použití (Job.text).
Proč: každé vylepšení pravidel se pak projeví i u inzerátů stažených dřív,
bez nového stahování. (Dřív se čistilo jen při stažení, takže stavbyvedoucí
staženy předtím dál sbíral body za šablonu agentury.)

Samostatný modul, protože čištění potřebuje models.Job (vlastnost text)
i detail/explain. Závisí jen na text.py, takže nevznikne kruhový import.
"""

from __future__ import annotations

import re

from jobagent.text import fold

# Řádky, které nejsou popisem pozice, i když je zaměstnavatel vložil přímo
# do textu inzerátu. Porovnává se na fold()-nutém řádku (bez diakritiky, malá písmena).
# Proč: bodování by z nich bralo body (stavbyvedoucí agentury Jobs Contact dostal
# +12 za C#, protože šablona agentury obsahuje řádek „Tato pracovní nabídka je
# vhodná pro pozice: programátor… c#, asp.net…“) a AI by dostalo šum.
JUNK_LINES = [
    # Blok klíčových slov pro vyhledávače („vhodná pro pozice: …“)
    (re.compile(r"vhodn\w* (?:i )?(?:pro|na) (?:pozic|nasledujici pozic)"), "klíčová slova agentury"),
    # Souhlas se zpracováním osobních údajů. Ne samotné „GDPR“: to bývá
    # i požadavek na znalost („Knowledge of GDPR“), který k inzerátu patří.
    (re.compile(r"osobni\w* udaj\w*.{0,80}zpracov|zpracov\w*.{0,80}osobni\w* udaj"), "osobní údaje / GDPR"),
    # Odkazy na další nabídky agentury
    (re.compile(r"^dalsi (?:pracovni )?nabidky (?:prace )?(?:naleznete|najdete)"), "odkaz na další nabídky"),
]


def clean_description(text: str) -> tuple[str, list[str]]:
    """Vrací (očištěný text, odstraněné řádky s důvodem). Opakované volání nic nemění."""
    if not text:
        return text, []
    kept, removed = [], []
    for line in text.split("\n"):
        folded = fold(line)
        reason = next((why for rx, why in JUNK_LINES if rx.search(folded)), None)
        if reason:
            removed.append(f"[{reason}] {line}")
        else:
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), removed
