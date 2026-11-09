"""Kde leží soubory, které program čte a zapisuje (config, DB, logy, šablony).

JEDINÉ místo v kódu, které řeší rozdíl mezi během ze zdrojáků a během jako exe.
Všechny ostatní moduly se ptají sem, nikdy si cestu neskládají samy.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Složka, ve které uživatel vidí a upravuje své soubory.

    Proč ne sys._MEIPASS: PyInstaller (hlavně v režimu onefile) při každém
    spuštění rozbalí program do dočasné složky a _MEIPASS ukazuje tam.
    Ta se po skončení maže, takže config ani DB by tam nepřežily a kamarád by
    je ani nenašel. Proto u exe bereme složku, kde leží samotné .exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Ze zdrojáků: kořen projektu (o úroveň nad balíčkem jobagent/).
    return Path(__file__).resolve().parent.parent


def config_file() -> Path:
    return app_dir() / "config.toml"


def secrets_file() -> Path:
    return app_dir() / "secrets.toml"


def db_file() -> Path:
    return app_dir() / "jobagent.db"


def log_dir() -> Path:
    return app_dir() / "logs"


def templates_dir() -> Path:
    # Šablony leží vedle exe, ne uvnitř něj, aby šly upravit bez rebuildu.
    return app_dir() / "templates"


def output_dir() -> Path:
    """Sem ukládá `run --dry-run` náhled e-mailu."""
    return app_dir() / "vystup"


def samples_dir() -> Path:
    return app_dir() / "samples"


def private_samples_dir() -> Path:
    """Sem ukládá `jobagent sample` (v .gitignore).

    Proč ne rovnou samples/: uložená stránka je skutečná, s plným textem
    inzerátu a kontakty na personalisty. Do samples/ (sledované gitem) patří
    jen anonymizovaná kopie z tools/anonymize_samples.py, takže skutečná
    stránka se nesmí dostat do commitu ani omylem.
    """
    return samples_dir() / "private"


def unknown_samples_dir() -> Path:
    """Automaticky uložené detaily s neznámým rozložením (viz pipeline)."""
    return samples_dir() / "nezname"
