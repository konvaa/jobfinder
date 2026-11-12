# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: jeden onedir build se dvěma exe nad společným _internal\\.

Spouští se přes tools\\build.py (čisté .venv-build, testy, zip), ručně:
    pyinstaller jobagent.spec --clean --noconfirm

Výsledek (dist\\JobAgent\\):
    JobAgent.exe            konzolové, pro ruční příkazy
    JobAgent-planovac.exe   bez okna (console=False), spouští ho Plánovač úloh
    _internal\\             Python, knihovny, výchozí šablony, vzor configu

Proč onedir a ne onefile (schváleno ve fázi 6): žádné rozbalování do %TEMP%
při každém startu, méně falešných poplachů antivirů, a console.owns_console()
(pauza po dvojkliku) spolehlivě funguje jen s jedním procesem.

Proč dvě exe: konzolová aplikace spuštěná z Plánovače otevře okno konzole
(problikne). Exe bez okna zase neumí vypisovat do příkazového řádku. Obě
sdílejí jeden Analysis i _internal\\, takže druhé exe přidá jen ~1–2 MB.

Tenhle soubor je Python, který spouští PyInstaller: SPECPATH a třídy
Analysis/PYZ/EXE/COLLECT dodává on, proto je neimportujeme.
"""

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821 – dodává PyInstaller
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "jobagent" / "__init__.py").read_text(encoding="utf-8")).group(1)
AUTHOR = "Libor Konvičný"

a = Analysis(  # noqa: F821
    [str(ROOT / "jobagent" / "__main__.py")],
    pathex=[str(ROOT)],
    # Data, která program čte z disku (paths.bundled_dir / templating.DEFAULT_DIR).
    # Cíl odpovídá cestám ze zdrojáků: jobagent/templates vedle modulů,
    # config.example.toml v kořeni _internal\.
    datas=[
        (str(ROOT / "jobagent" / "templates"), "jobagent/templates"),
        (str(ROOT / "config.example.toml"), "."),
    ],
    # Testovací a GUI moduly program nepotřebuje; vyřazení zmenší build.
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821


def version_info(file_name: str, description: str):
    """Údaje ve Vlastnosti → Podrobnosti. Na Linuxu (zkušební build) nic: PyInstaller je tam ignoruje."""
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    numbers = tuple(int(x) for x in re.findall(r"\d+", VERSION)[:3]) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0, OS=0x40004,
                          fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            # 0405 = čeština, 04B0 = Unicode. Windows pak ukáže diakritiku správně.
            StringFileInfo([StringTable("040504B0", [
                StringStruct("CompanyName", AUTHOR),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", file_name.removesuffix(".exe")),
                StringStruct("LegalCopyright", f"© 2026 {AUTHOR}, licence MIT"),
                StringStruct("OriginalFilename", file_name),
                StringStruct("ProductName", "Job Agent"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0405, 1200])]),
        ],
    )


# UTF-8 režim interpretu (PEP 540) v obou exe. Proč: zabalený program běží
# v izolované konfiguraci a proměnné PYTHON* (i PYTHONUTF8) ignoruje, takže
# výchozí kódování by záviselo na stroji – na českých Windows cp1250, na
# anglických cp1252. S UTF-8 režimem je výstup do roury/souboru i open() bez
# encoding= všude stejné. Výpis do okna konzole to nemění: Python tam píše
# přes WriteConsoleW (Unicode) v každém režimu.
# Pozor na zápis: zavaděč hledá klíč „utf8“ (jako `python -X utf8`). Starší
# zápis 'X utf8_mode=1' z changelogu PyInstalleru se dnes tiše ignoruje –
# hlídá to tests/test_build.py.
RUNTIME_OPTIONS = [("X utf8", None, "OPTION")]

common = dict(
    exclude_binaries=True,  # onedir: knihovny jdou do COLLECT, ne do exe
    debug=False,
    strip=False,
    upx=False,  # UPX komprese = víc falešných poplachů antivirů
)

console_exe = EXE(  # noqa: F821
    pyz, a.scripts, RUNTIME_OPTIONS,
    name="JobAgent",
    console=True,
    version=version_info("JobAgent.exe", "Job Agent – denní přehled nabídek práce"),
    **common,
)
planner_exe = EXE(  # noqa: F821
    pyz, a.scripts, RUNTIME_OPTIONS,  # volby jsou pro každé exe zvlášť
    name="JobAgent-planovac",
    console=False,  # bez okna: běh z Plánovače úloh nic neprobliká
    version=version_info("JobAgent-planovac.exe", "Job Agent – běh na pozadí (Plánovač úloh)"),
    **common,
)

coll = COLLECT(  # noqa: F821
    console_exe, planner_exe,
    a.binaries, a.datas,
    strip=False,
    upx=False,
    name="JobAgent",
)
