"""Chování v okně konzole: spuštění dvojklikem, pauza na konci, otevření souboru k úpravě.

Proč: konzolový program spuštěný dvojklikem z Průzkumníka dostane vlastní okno,
které se po skončení hned zavře. Kamarád by viděl jen problikutí a myslel by
si, že program nefunguje. Program proto pozná, že okno patří jen jemu,
a před koncem počká na Enter. Z příkazové řádky (cmd, Windows Terminal) se
nečeká, aby program šel dál používat ve skriptech.

Jak se to pozná: GetConsoleProcessList vrací procesy připojené ke konzoli.
Jen jeden = okno vzniklo pro nás (dvojklik, zástupce). Z cmd jsou dva (cmd
a my). Funguje jen u onedir buildu: onefile má dva procesy (rozbalovač
a program), proto by vracel 2 i po dvojkliku. Mimo Windows vždy False.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def owns_console() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint32 * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, len(processes))
    except (AttributeError, OSError):
        return False
    return count == 1  # 0 = žádná konzole (exe bez okna z Plánovače)


def pause_if_own_window(own: bool) -> None:
    if not own:
        return
    try:
        input("\nStiskni Enter pro zavření okna…")
    except (EOFError, KeyboardInterrupt):
        pass


def can_ask() -> bool:
    """Je kde zobrazit dotaz a přečíst odpověď? (Ne v exe bez okna ani při přesměrování.)"""
    return sys.stdin is not None and sys.stdout is not None and sys.stdin.isatty()


def open_for_edit(path: Path) -> bool:
    """Otevřít soubor v Poznámkovém bloku. True = podařilo se.

    Proč notepad.exe a ne os.startfile: .toml nemá ve Windows obvykle
    přiřazený program, startfile by skončil chybou „žádná aplikace“.
    """
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(["notepad.exe", str(path)])
    except OSError:
        return False
    return True
