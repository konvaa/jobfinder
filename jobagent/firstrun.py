"""První spuštění: když chybí config.toml, vytvořit ho ze vzoru a říct, co vyplnit.

Proč ze vzoru přibaleného k programu (paths.example_config), ne z
config.example.toml vedle exe: ten mohl kamarád smazat nebo upravit, přibalený
vždy odpovídá verzi programu.

secrets.toml se nevytváří. Heslo k e-mailu se ukládá příkazem nastav-heslo
do Správce pověření Windows (credentials.py), takže soubor s heslem v čistém
textu vůbec nemusí vzniknout.

Existující config.toml se nikdy nepřepíše. Po vytvoření program skončí
kódem 2 (chyba v nastavení): s ukázkovým configem nemá smysl nic odesílat.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def command_name(frozen: bool) -> str:
    """Jak se program spouští – do návodů ve zprávách."""
    return "JobAgent.exe" if frozen else "python -m jobagent"


def ensure_config(config_path: Path, example_path: Path) -> bool:
    """Vytvořit config_path ze vzoru, pokud chybí. True = právě vznikl."""
    if config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example_path, config_path)
    return True


def welcome_text(config_path: Path, cmd: str) -> str:
    return f"""
Vítej v Job Agentovi! Tohle je první spuštění, proto jsem vytvořil soubor
    {config_path}
s ukázkovým nastavením. Otevři ho (třeba v Poznámkovém bloku) a vyplň:

  1. HLEDÁNÍ – u každého [[hledani]] adresu url = "…": otevři jobs.cz,
     prace.cz nebo jobscontact.cz, nastav místo a obor a zkopíruj adresu
     z adresního řádku prohlížeče. Ukázková hledání smaž nebo uprav.
  2. E-MAIL – v části [email] svou adresu (uzivatel) a komu posílat (prijemci).
Soubor ulož a v příkazovém řádku spusť:
    {cmd} nastav-heslo        (heslo k e-mailu, uloží se bezpečně ve Windows)
U Seznamu s dvoufázovým ověřením a u Gmailu je to „heslo pro aplikace“.

Pak vyzkoušej:
    {cmd} test-email          (přijde zkušební e-mail)
    {cmd} run --dry-run       (ukáže, co by přišlo, nic neodešle)

Podrobný návod je v souboru NAVOD.txt.
"""
