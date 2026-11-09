"""Umožňuje spuštění `py -3.12 -m jobagent ...` ze zdrojáků.

Proč samostatný soubor: `-m balicek` hledá právě `__main__.py`. Veškerá logika
je v cli.py, aby ji mohl stejně zavolat i vstupní skript pro PyInstaller (fáze 6).
"""

import sys

from jobagent.cli import main

sys.exit(main())
