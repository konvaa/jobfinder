"""Build exe a zipu pro kamarády jedním příkazem:  py -3.12 tools\\build.py

Kroky (každý selhaný krok build zastaví):
  1. git bez necommitnutých změn – zip má vždy odpovídat konkrétnímu commitu
     (zkušební build: --necisty);
  2. čisté .venv-build (smazat a vytvořit znovu) + requirements-dev.txt –
     tvoje vývojové .venv se nemění a do buildu se nepřimíchá nic navíc;
  3. kontrola zdrojáků s varováními jako chybami (python -W error -m compileall);
  4. testy v tomtéž čistém prostředí (ověří i připnuté verze knihoven);
  5. PyInstaller podle jobagent.spec (onedir, dvě exe);
  6. doplnění souborů vedle exe (vzor configu, NAVOD.txt, licence);
  7. kontrola obsahu – v balíku nesmí být config.toml, secrets*.toml, DB,
     logy, výstupy ani upravené šablony;
  8. zkouška hotového exe v dočasné kopii (--version, první spuštění,
     sablony) – ověří, že se přibalila data;
  9. zip + SHA256 + test-sandbox.wsb (Windows Sandbox, viz docs/NAVOD.md).

Proč skript v Pythonu, ne .bat/.ps1: stejný jazyk jako projekt, žádné potíže
s politikou spouštění PowerShell skriptů a logika (verze, kontrola obsahu)
se dá číst i upravit bez dalšího jazyka. Používá jen standardní knihovnu,
takže běží dřív, než existuje jakékoli venv.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv-build"
DIST = ROOT / "dist"
WORK = ROOT / "build"
APP = DIST / "JobAgent"
IS_WIN = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WIN else ""
WANTED_PYTHON = (3, 12)

# V balíku nesmí být nic osobního ani nic, co vzniká až za běhu.
FORBIDDEN_NAMES = {"config.toml", "jobagent.db", "logs", "vystup", "samples", "prompts"}
FORBIDDEN_PATTERNS = [re.compile(p) for p in (r"^secrets(?!\.example\.toml$).*\.toml$", r"^config\.toml\..*", r".*\.db$", r".*\.eml$")]
# Licence přibalených knihoven (MIT/PSF vyžadují přiložit upozornění).
LICENSED_PACKAGES = ("beautifulsoup4", "soupsieve", "typing_extensions")


class BuildError(Exception):
    pass


def utf8_env(**extra: str) -> dict[str, str]:
    """Prostředí pro podprocesy: Python v režimu UTF-8 bez ohledu na kódovou stránku stroje.

    Proč: na českých Windows je výchozí kódování cp1250. pip by bez toho četl
    requirements v cp1250, testy a PyInstaller by zapisovaly/četly soubory bez
    uvedeného kódování jinak než na Linuxu. PYTHONUTF8=1 (PEP 540) přepne
    výchozí kódování na UTF-8 pro open(), roury i proměnné prostředí.
    """
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **extra}


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def run(args: list[str | Path], **kwargs) -> subprocess.CompletedProcess:
    printable = " ".join(str(a) for a in args)
    print(f"  > {printable}", flush=True)
    kwargs.setdefault("env", utf8_env())
    result = subprocess.run([str(a) for a in args], **kwargs)
    if result.returncode != 0:
        raise BuildError(f"Příkaz skončil kódem {result.returncode}: {printable}")
    return result


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def version() -> str:
    text = (ROOT / "jobagent" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def check_git(allow_dirty: bool) -> str:
    step("1/9 Git")
    # encoding výslovně: text=True by jinak dekódoval výstup gitu kódováním stroje (cp1250).
    git = dict(cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    status = subprocess.run(["git", "status", "--porcelain"], **git)
    if status.returncode != 0:
        raise BuildError("git status selhal – je to repozitář?")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], **git).stdout.strip()
    if status.stdout.strip():
        if not allow_dirty:
            raise BuildError("V repozitáři jsou necommitnuté změny. Commitni je, nebo pro zkušební build "
                             "použij --necisty.\n" + status.stdout)
        print("  POZOR: necommitnuté změny, zkušební build (nepublikovat).")
        commit += "-necisty"
    print(f"  commit {commit}, verze {version()}")
    return commit


def fresh_venv() -> None:
    step("2/9 Čisté prostředí .venv-build")
    if sys.version_info[:2] != WANTED_PYTHON:
        print(f"  POZOR: build běží v Pythonu {sys.version.split()[0]}, program se testuje ve "
              f"{'.'.join(map(str, WANTED_PYTHON))}. Spusť: py -{'.'.join(map(str, WANTED_PYTHON))} tools\\build.py")
    if VENV.exists():
        shutil.rmtree(VENV)
    run([sys.executable, "-m", "venv", VENV])
    run([venv_python(), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
         ROOT / "requirements-dev.txt"], cwd=ROOT)


def check_sources() -> None:
    step("3/9 Zdrojáky bez varování (python -W error -m compileall)")
    run([venv_python(), "-W", "error", "-m", "compileall", "-q", "-f", "jobagent", "tests", "tools"], cwd=ROOT)


def run_tests() -> None:
    step("4/9 Testy (dvakrát: s UTF-8 a jako u kamaráda bez něj)")
    # 1) S PYTHONUTF8=1 jako všechno ostatní v buildu.
    run([venv_python(), "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=ROOT)
    # 2) BEZ režimu UTF-8 a s EncodingWarning jako chybou: tak poběží exe u kamaráda
    #    (Windows s cp1250). Chytí každé open()/read_text() bez encoding= v kódu,
    #    kterým testy projdou. Statická kontrola v tests/test_source_hygiene.py
    #    hlídá i zbytek.
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    run([venv_python(), "-X", "warn_default_encoding", "-W", "error::EncodingWarning",
         "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=ROOT, env=env)


def pyinstaller() -> None:
    step("5/9 PyInstaller")
    for folder in (APP, WORK):
        if folder.exists():
            shutil.rmtree(folder)
    run([venv_python(), "-m", "PyInstaller", "jobagent.spec", "--clean", "--noconfirm",
         "--distpath", DIST, "--workpath", WORK, "--log-level", "WARN"], cwd=ROOT)
    for name in ("JobAgent", "JobAgent-planovac"):
        if not (APP / f"{name}{EXE_SUFFIX}").is_file():
            raise BuildError(f"PyInstaller nevytvořil {name}{EXE_SUFFIX}.")


def add_files() -> None:
    step("6/9 Soubory vedle exe")
    shutil.copyfile(ROOT / "config.example.toml", APP / "config.example.toml")
    navod = ROOT / "docs" / "NAVOD.md"
    if navod.is_file():
        # .txt: dvojklik otevře Poznámkový blok; .md by Windows nevěděla čím otevřít.
        # CRLF, aby se starší Poznámkový blok nezalamoval do jednoho řádku.
        text = navod.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
        (APP / "NAVOD.txt").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    else:
        print("  POZOR: docs/NAVOD.md zatím neexistuje (vznikne v 6e), NAVOD.txt nebude v balíku.")
    licences = APP / "licence"
    licences.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", licences / "JobAgent-LICENSE.txt")
    # Licence knihoven a Pythonu z čistého prostředí: přesně ty verze, které jsou v exe.
    script = (
        "import sys, pathlib, importlib.metadata as m\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "for name in sys.argv[2:]:\n"
        "    files = [f for f in (m.files(name) or []) if 'licen' in f.name.lower() or 'copying' in f.name.lower()]\n"
        "    for f in files:\n"
        "        (out / f'{name}-{f.name}.txt').write_bytes(pathlib.Path(f.locate()).read_bytes())\n"
        "base = pathlib.Path(sys.base_prefix)\n"
        "for c in (base / 'LICENSE.txt', base / 'LICENSE', base / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'LICENSE.txt'):\n"
        "    if c.is_file():\n"
        "        (out / 'Python-LICENSE.txt').write_bytes(c.read_bytes()); break\n"
    )
    run([venv_python(), "-c", script, licences, *LICENSED_PACKAGES])
    print("  licence: " + ", ".join(sorted(p.name for p in licences.iterdir())))


def check_contents() -> None:
    step("7/9 Kontrola obsahu balíku")
    problems = []
    for path in APP.rglob("*"):
        rel = path.relative_to(APP)
        if path.name in FORBIDDEN_NAMES or any(p.match(path.name) for p in FORBIDDEN_PATTERNS):
            problems.append(str(rel))
    if (APP / "templates").exists():
        problems.append("templates (upravené šablony patří uživateli, výchozí jsou v _internal)")
    if not (APP / "_internal" / "jobagent" / "templates" / "email.html").is_file():
        problems.append("CHYBÍ _internal/jobagent/templates (výchozí šablony)")
    if not (APP / "_internal" / "config.example.toml").is_file():
        problems.append("CHYBÍ _internal/config.example.toml (vzor pro první spuštění)")
    if problems:
        raise BuildError("Balík obsahuje, co nemá (nebo mu něco chybí):\n  " + "\n  ".join(problems))
    print("  v pořádku")


def smoke_test() -> None:
    step("8/9 Zkouška hotového exe (v dočasné kopii, dist zůstane čistý)")
    # ignore_cleanup_errors: úklid dočasné kopie nesmí shodit build, když zkouška
    # prošla (na Windows může složku chvíli držet antivirus nebo jiný proces).
    with tempfile.TemporaryDirectory(prefix="jobagent-zkouska-", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "JobAgent"
        shutil.copytree(APP, copy)
        exe = copy / f"JobAgent{EXE_SUFFIX}"
        # Zabalený program nemusí PYTHONUTF8/PYTHONIOENCODING respektovat a do
        # roury pak píše v cp1250; errors="replace" zajistí, že zkouška ohlásí
        # skutečnou chybu, ne UnicodeDecodeError na české uvítací zprávě.
        # JOBAGENT_NO_EDITOR: první spuštění nesmí otevřít Poznámkový blok (zůstal by
        # viset a držel by dočasnou složku). Druhá pojistka: stdin=DEVNULL, bez terminálu
        # se program na nic neptá a nic neotevírá (console.can_ask).
        env = utf8_env(JOBAGENT_NO_EDITOR="1")
        quiet = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                     stdin=subprocess.DEVNULL)
        out = run([exe, "--version"], **quiet).stdout
        if version() not in out:
            raise BuildError(f"--version vypsal {out!r}, čekal jsem {version()}.")
        # Bez parametrů = první spuštění: má vzniknout config.toml ze vzoru a kód 2.
        first = subprocess.run([str(exe)], cwd=copy, **quiet)
        if first.returncode != 2 or not (copy / "config.toml").is_file():
            raise BuildError(f"První spuštění: kód {first.returncode}, config.toml "
                             f"{'vznikl' if (copy / 'config.toml').exists() else 'nevznikl'}.\n{first.stdout}{first.stderr}")
        if "jsem otevřel v Poznámkovém bloku" in first.stdout:  # věta z cli._first_run_if_needed
            raise BuildError("První spuštění při zkoušce otevřelo Poznámkový blok – exe nerespektuje "
                             "JOBAGENT_NO_EDITOR / chybějící terminál.")
        run([exe, "sablony"], cwd=copy, **quiet)
        if len(list((copy / "templates").glob("*"))) < 5:
            raise BuildError("sablony nevyexportoval všechny šablony – chybí v _internal?")
        print("  --version, první spuštění (bez Poznámkového bloku) a sablony: v pořádku")
    if Path(tmp).exists():
        print(f"  POZOR: dočasnou kopii {tmp} se nepodařilo smazat (drží ji jiný proces). "
              "Build je v pořádku, složku smaž později ručně.")


def package(commit: str) -> Path:
    step("9/9 Zip")
    platform = "windows" if IS_WIN else sys.platform
    zip_path = DIST / f"JobAgent-{version()}-{platform}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(APP.rglob("*")):
            zf.write(path, Path("JobAgent") / path.relative_to(APP))  # v zipu jedna složka JobAgent\
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    # Formát „hash  soubor“ přečte sha256sum i PowerShell (porovnáním s Get-FileHash).
    zip_path.with_name(zip_path.name + ".sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    (DIST / "BUILD-INFO.txt").write_text(
        f"Job Agent {version()}\ncommit {commit}\nPython {sys.version.split()[0]}\n"
        f"vytvořeno {time.strftime('%Y-%m-%d %H:%M')}\nSHA256 {digest}\n", encoding="utf-8")
    write_sandbox_config()
    return zip_path


def write_sandbox_config() -> None:
    """Windows Sandbox: dist\\ připojený jen pro čtení na plochu, síť zapnutá."""
    host = str(DIST.resolve())
    (DIST / "test-sandbox.wsb").write_text(f"""<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{host}</HostFolder>
      <SandboxFolder>C:\\Users\\WDAGUtilityAccount\\Desktop\\dist</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <Networking>Enable</Networking>
</Configuration>
""", encoding="utf-8")


def size(path: Path) -> str:
    total = path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return f"{total / 1024 / 1024:.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Job Agenta: exe + zip pro kamarády.")
    parser.add_argument("--necisty", action="store_true", help="povolit build s necommitnutými změnami (zkušební)")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        commit = check_git(args.necisty)
        fresh_venv()
        check_sources()
        run_tests()
        pyinstaller()
        add_files()
        check_contents()
        smoke_test()
        zip_path = package(commit)
    except BuildError as exc:
        print(f"\nBUILD SELHAL: {exc}", file=sys.stderr)
        return 1
    print(f"\nHotovo za {time.monotonic() - started:.0f} s.")
    print(f"  složka   {APP}  ({size(APP)})")
    for name in ("JobAgent", "JobAgent-planovac"):
        print(f"  exe      {name}{EXE_SUFFIX}  ({size(APP / f'{name}{EXE_SUFFIX}')})")
    print(f"  zip      {zip_path}  ({size(zip_path)})")
    print(f"  sha256   {zip_path.name}.sha256")
    print(f"  sandbox  {DIST / 'test-sandbox.wsb'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
