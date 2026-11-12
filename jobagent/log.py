"""Nastavení logování do souboru logs/jobagent.log (+ do konzole, pokud existuje).

Proč soubor: z Plánovače úloh běží program bez viditelné konzole, takže log
je jediný záznam o tom, co se stalo. Rotace drží velikost pod kontrolou,
aby kamarádům za rok nezabíral disk.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "jobagent.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Opakované volání (např. v testech) nesmí přidávat duplicitní handlery.
    # Staré se nejdřív zavřou: na Windows by otevřený log zůstal zamčený.
    close_logging()

    # encoding='utf-8' výslovně: na Windows by jinak byl použit cp1250
    # a log by se v jiných editorech zobrazoval rozsypaně.
    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    # Exe zabalené bez konzole má sys.stderr = None; zápis do něj by spadl.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)

    return log_path


def close_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
