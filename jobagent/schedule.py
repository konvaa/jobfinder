"""Denní spouštění přes Plánovač úloh Windows: install-schedule / uninstall-schedule.

Proč XML a ne `schtasks /Create /SC DAILY …`: volbu „spustit co nejdříve po
zmeškaném startu“ (StartWhenAvailable) ani běh na baterii z příkazové řádky
nastavit nejde, jen v XML definici úlohy. Program XML vygeneruje a předá ho
`schtasks /Create /XML`.

Nastavení úlohy (schváleno ve fázi 6, viz docs/ARCHITEKTURA.md):
- jen když je uživatel přihlášen (InteractiveToken): bez hesla Windows, bez
  práv správce, funguje Správce pověření (DPAPI má klíče jen pro přihlášeného);
- denně v zadaný čas + náhodné zpoždění 0–15 min (kamarádi nestahují portály
  ve stejnou vteřinu);
- zmeškaný start (vypnutý počítač) proběhne po zapnutí/přihlášení – Plánovač
  ho pustí zhruba do 10 minut;
- běží i na baterii (výchozí nastavení Windows by běh na notebooku vynechalo);
- limit 2 h jako pojistka proti zaseknutí; druhá instance se nespustí;
- spouští exe BEZ okna (JobAgent-planovac.exe), aby nic neproblikávalo.

Výstup schtasks se nikdy nečte: je lokalizovaný (česky u tebe, anglicky
v Sandboxu). Rozhoduje jen návratový kód; text chyby se jen předá uživateli.
Úlohu program čte zpátky jako XML (`/Query /XML`), to lokalizované není.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

TASK_NAME = "Job Agent"
DEFAULT_TIME = (7, 30)
RANDOM_DELAY_MINUTES = 15
TIME_LIMIT = "PT2H"
PLANNER_EXE = "JobAgent-planovac.exe"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

Runner = Callable[..., subprocess.CompletedProcess]


class ScheduleError(Exception):
    pass


@dataclass
class Command:
    """Co Plánovač spustí."""

    program: Path
    arguments: str
    workdir: Path


@dataclass
class TaskInfo:
    """Co program zjistí o existující úloze (z jejího XML)."""

    program: str
    time: str  # "07:30"


def parse_time(text: str | None) -> tuple[int, int]:
    """'7:30', '07:30' i '7.30' → (7, 30). Bez času výchozí 7:30."""
    if not text:
        return DEFAULT_TIME
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ScheduleError(f"Čas „{text}“ nerozumím. Zadej ho jako hodiny:minuty, např. 7:30 nebo 18:05.")
    return int(match.group(1)), int(match.group(2))


def planner_command(frozen: bool, executable: Path, app_dir: Path) -> Command:
    """Příkaz pro Plánovač: u exe JobAgent-planovac.exe, ze zdrojáků pythonw.exe -m jobagent."""
    if frozen:
        program = executable.resolve().parent / PLANNER_EXE
        if not program.is_file():
            raise ScheduleError(f"Vedle programu chybí {PLANNER_EXE}. Rozbal znovu celý zip (ne jen JobAgent.exe).")
        return Command(program, "run", program.parent)
    # pythonw.exe = Python bez okna konzole, leží vedle python.exe (i ve .venv\Scripts).
    pythonw = executable.resolve().with_name("pythonw.exe")
    program = pythonw if pythonw.is_file() else executable.resolve()
    return Command(program, "-m jobagent run", app_dir)


def current_user() -> str | None:
    """DOMÉNA\\uživatel pro <UserId>. Bez něj by úloha nemusela mít jasného vlastníka."""
    user = os.environ.get("USERNAME")
    if not user:
        return None
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def task_xml(command: Command, hour: int, minute: int, *, start_day: date | None = None,
             user: str | None = None) -> str:
    """XML definice úlohy (schéma Task Scheduler 1.2 = Windows 7 a novější)."""
    start = f"{(start_day or date.today()).isoformat()}T{hour:02d}:{minute:02d}:00"  # bez zóny = místní čas
    user_line = f"\n      <UserId>{escape(user)}</UserId>" if user else ""
    # Uvozovky kolem cesty: složka může obsahovat mezery („Job Agent“, „Stažené soubory“).
    program = escape(f'"{command.program}"')
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Job Agent: denní souhrn nabídek práce e-mailem. Vytvořeno příkazem install-schedule, smazání: uninstall-schedule.</Description>
    <URI>\\{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <RandomDelay>PT{RANDOM_DELAY_MINUTES}M</RandomDelay>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{user_line}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{TIME_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{program}</Command>
      <Arguments>{escape(command.arguments)}</Arguments>
      <WorkingDirectory>{escape(str(command.workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(args: list[str], runner: Runner) -> subprocess.CompletedProcess:
    if sys.platform != "win32" and runner is subprocess.run:
        raise ScheduleError("Plánovač úloh je jen ve Windows.")
    try:
        # Bez text=True: výstup je v kódové stránce konzole (cp852/cp1250), dekóduje se až pro výpis.
        return runner(["schtasks", *args], capture_output=True)
    except FileNotFoundError:
        raise ScheduleError("Program schtasks nebyl nalezen – je to Windows?") from None


def _message(result: subprocess.CompletedProcess) -> str:
    raw = (result.stderr or b"") + (result.stdout or b"")
    if isinstance(raw, str):
        return raw.strip()
    for encoding in ("utf-8", "cp852", "cp1250"):  # konzole Windows bývá cp852 (čeština)
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("ascii", "replace").strip()


def install(command: Command, hour: int, minute: int, *, runner: Runner = subprocess.run,
            user: str | None = None) -> None:
    xml = task_xml(command, hour, minute, user=user if user is not None else current_user())
    # schtasks chce soubor, ne vstup. UTF-16 s BOM: tak ho Plánovač sám exportuje
    # a cesty s diakritikou („Plocha“, „Stažené soubory“) projdou beze ztráty.
    handle, name = tempfile.mkstemp(prefix="jobagent-uloha-", suffix=".xml")
    os.close(handle)
    path = Path(name)
    try:
        path.write_text(xml, encoding="utf-16")
        result = _schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"], runner)
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise ScheduleError(f"Plánovač úloh úlohu nevytvořil: {_message(result)}")


def query(*, runner: Runner = subprocess.run) -> TaskInfo | None:
    """Existující úloha, nebo None."""
    result = _schtasks(["/Query", "/TN", TASK_NAME, "/XML"], runner)
    if result.returncode != 0:
        return None
    raw = result.stdout
    text = raw if isinstance(raw, str) else _decode_xml(raw)
    try:
        root = ET.fromstring(text)  # deklaraci encoding="UTF-16" u textu (str) ElementTree ignoruje
    except ET.ParseError:
        return None
    program = (root.findtext(".//t:Exec/t:Command", "", NS) or "").strip().strip('"')
    start = root.findtext(".//t:CalendarTrigger/t:StartBoundary", "", NS) or ""
    return TaskInfo(program=program, time=start[11:16])


def _decode_xml(raw: bytes) -> str:
    # schtasks /XML vypisuje UTF-16 (s BOM), někdy UTF-8 – podle BOM / nulových bajtů.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:100]:
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", "replace")


def uninstall(*, runner: Runner = subprocess.run) -> bool:
    """Smazat úlohu. False = žádná nebyla."""
    if query(runner=runner) is None:
        return False
    result = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"], runner)
    if result.returncode != 0:
        raise ScheduleError(f"Plánovač úloh úlohu nesmazal: {_message(result)}")
    return True


def moved_warning(info: TaskInfo | None, expected: Path) -> str | None:
    """Úloha ukazuje na jiné místo (složka se přesunula / kopie programu)? → text varování."""
    if info is None or not info.program:
        return None
    if os.path.normcase(info.program) == os.path.normcase(str(expected)):
        return None
    return (f"Naplánovaná úloha „{TASK_NAME}“ spouští {info.program}, ale program teď leží v {expected.parent}. "
            "Pokud jsi složku přesunul, spusť znovu install-schedule, jinak se denní souhrn přestane posílat.")
