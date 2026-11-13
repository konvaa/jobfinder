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

Režim na pozadí: JobAgent-planovac.exe (a pythonw.exe ze zdrojáků) nemá
konzoli vůbec, sys.stdout i sys.stderr jsou None. print() pak nic nedělá,
ale cokoli, co volá .write() přímo (argparse při chybě, traceback), by
spadlo – proto se na začátku přesměrují do os.devnull. Všechno důležité
jde do logs/jobagent.log.
"""

from __future__ import annotations

import os
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


# Vypne otevírání Poznámkového bloku (build ho nastavuje při zkoušce exe).
NO_EDITOR_ENV = "JOBAGENT_NO_EDITOR"


def can_ask() -> bool:
    """Sedí u programu člověk? Terminál musí být na vstupu I na výstupu.

    Jen vstup nestačí: build pouští exe s výstupem do roury a vstupem
    zděděným z terminálu. Program by si myslel, že se může ptát a otevírat
    editor, ale nikdo by nic neviděl.
    """
    return (sys.stdin is not None and sys.stdout is not None
            and sys.stdin.isatty() and sys.stdout.isatty())


def open_for_edit(path: Path) -> bool | None:
    """Otevřít soubor v Poznámkovém bloku.

    True = otevřeno, False = nepodařilo se (Poznámkový blok chybí – stalo se
    ve Windows Sandbox, Windows 11 ho nemusí mít nainstalovaný), None = ani
    se nezkoušelo (mimo Windows, nebo vypnuto přes JOBAGENT_NO_EDITOR).
    Volající podle toho rozhodne, jestli uživateli poradit otevřít soubor ručně.

    Proč notepad.exe a ne os.startfile: .toml nemá ve Windows obvykle
    přiřazený program, startfile by skončil chybou „žádná aplikace“.

    Pracovní adresář Poznámkového bloku je domovská složka, ne složka
    programu: Windows nedovolí smazat ani přesunout složku, kterou má
    nějaký proces jako pracovní adresář. Bez toho by kamarád nemohl složku
    s programem přesunout, dokud má config otevřený (a build by nemohl
    uklidit zkušební kopii). Proto taky absolutní cesta k souboru.
    """
    if sys.platform != "win32" or os.environ.get(NO_EDITOR_ENV):
        return None
    try:
        subprocess.Popen(["notepad.exe", str(path.resolve())], cwd=_neutral_dir())
    except OSError:  # FileNotFoundError: notepad.exe není
        return False
    return True


def _neutral_dir() -> str | None:
    """Složka, kterou nikdo nebude mazat: domovská složka uživatele (jinak Windows)."""
    for candidate in (Path.home(), Path(os.environ.get("SystemRoot", r"C:\Windows"))):
        if candidate.is_dir():
            return str(candidate)
    return None


def in_background() -> bool:
    """Běží program bez konzole (Plánovač, dvojklik na JobAgent-planovac.exe, pythonw)?"""
    return sys.stdout is None or sys.stderr is None


def silence_missing_streams() -> None:
    """Chybějící stdout/stderr nahradit os.devnull, aby zápis do nich nespadl."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 – žije do konce procesu
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def message_box(text: str, title: str = "Job Agent") -> None:
    """Okno se zprávou (jediný způsob, jak něco říct programu bez konzole)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        MB_ICONINFORMATION = 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, MB_ICONINFORMATION)
    except (AttributeError, OSError):
        pass
