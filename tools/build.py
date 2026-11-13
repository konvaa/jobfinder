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
  7. kontrola obsahu – nahoře ve složce jen povolené položky; nikde
     config.toml, secrets*.toml, DB, logy, výstupy ani upravené šablony;
  8. zkouška hotového exe v dočasné kopii (--version, první spuštění,
     sablony, přejmenované exe) – ověří, že se přibalila data;
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
LICENSED_PACKAGES = ("beautifulsoup4", "soupsieve", "typing_extensions", "truststore")
# Co smí být nahoře ve složce JobAgent\. Jen to, na co má kamarád sahat;
# všechno ostatní (Python, knihovny, licence) je v _internal\. Seznam je
# povolující: cokoli navíc build zastaví (pojistka proti omylem přibaleným souborům).
TOP_LEVEL = {"_internal", f"JobAgent{EXE_SUFFIX}", f"JobAgent-planovac{EXE_SUFFIX}", "NAVOD.txt",
             "config.example.toml"}
GUIDE = "NAVOD.txt"
# Moduly, které musí být v exe. truststore se importuje až uvnitř funkce
# (tls.create_context) a jen ve Windows – kdyby ho PyInstaller nepřibalil,
# exe by tiše ověřovalo přes OpenSSL a na čerstvých Windows selhávalo.
REQUIRED_MODULES = ("bs4", "truststore", "truststore._windows")


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
    check_bundled_modules(WORK / "jobagent" / "PYZ-00.toc")


def check_bundled_modules(toc: Path) -> None:
    """V seznamu modulů, které PyInstaller zabalil (PYZ-00.toc), musí být REQUIRED_MODULES."""
    if not toc.is_file():
        raise BuildError(f"Chybí {toc} – nejde ověřit, co PyInstaller zabalil.")
    text = toc.read_text(encoding="utf-8", errors="replace")
    missing = [m for m in REQUIRED_MODULES if f"'{m}'" not in text]
    if missing:
        raise BuildError("PyInstaller nezabalil: " + ", ".join(missing) + ". Je v requirements.txt a v .venv-build?")
    print("  zabalené moduly: " + ", ".join(REQUIRED_MODULES))


def add_files() -> None:
    step("6/9 Soubory vedle exe")
    shutil.copyfile(ROOT / "config.example.toml", APP / "config.example.toml")
    (APP / GUIDE).write_bytes(guide_bytes())
    # Licence do _internal\: upozornění musí být přiložené, ne vystavené nahoře
    # mezi soubory, na které má kamarád sahat.
    licences = APP / "_internal" / "licence"
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


def guide_bytes() -> bytes:
    """docs/NAVOD.md jako NAVOD.txt: UTF-8 s BOM a CRLF.

    .txt: dvojklik otevře Poznámkový blok; .md by Windows nevěděla čím otevřít.
    BOM: Poznámkový blok podle něj pozná UTF-8 i na starších Windows. CRLF:
    starší Poznámkový blok by jinak ukázal všechno na jednom řádku.
    """
    navod = ROOT / "docs" / "NAVOD.md"
    if not navod.is_file():
        raise BuildError("Chybí docs/NAVOD.md – bez návodu se balík pro kamarády nevydává.")
    text = navod.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def check_contents() -> None:
    step("7/9 Kontrola obsahu balíku")
    problems = []
    present = {p.name for p in APP.iterdir()}
    for extra in sorted(present - TOP_LEVEL):
        problems.append(f"{extra} (nahoře smí být jen: {', '.join(sorted(TOP_LEVEL))})")
    for missing in sorted(TOP_LEVEL - present):
        problems.append(f"CHYBÍ {missing}")
    for path in APP.rglob("*"):
        rel = path.relative_to(APP)
        if path.name in FORBIDDEN_NAMES or any(p.match(path.name) for p in FORBIDDEN_PATTERNS):
            problems.append(str(rel))
    if not (APP / "_internal" / "jobagent" / "templates" / "email.html").is_file():
        problems.append("CHYBÍ _internal/jobagent/templates (výchozí šablony)")
    if not (APP / "_internal" / "config.example.toml").is_file():
        problems.append("CHYBÍ _internal/config.example.toml (vzor pro první spuštění)")
    if problems:
        raise BuildError("Balík obsahuje, co nemá (nebo mu něco chybí):\n  " + "\n  ".join(problems))
    print("  v pořádku")


def run_exe(args: list, cwd: Path) -> tuple[int, str]:
    """Spustit hotové exe bez terminálu a vrátit (kód, výstup) dekódovaný STRIKTNĚ jako UTF-8.

    Striktně, ne errors="replace": exe má zapnutý UTF-8 režim (jobagent.spec,
    RUNTIME_OPTIONS), takže výstup musí být platné UTF-8 na každých Windows.
    Kdyby volba chyběla nebo nezabrala, exe by do roury psalo v cp1250 a tady
    by to skončilo jasnou chybou místo rozsypaných znaků („Hleda� pr�ce“).
    stdin=DEVNULL + JOBAGENT_NO_EDITOR: program se na nic neptá a nic neotevírá
    (Poznámkový blok by zůstal viset a držel dočasnou složku).
    """
    result = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, stdin=subprocess.DEVNULL,
                            env=utf8_env(JOBAGENT_NO_EDITOR="1"))
    raw = result.stdout + result.stderr
    try:
        return result.returncode, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"Výstup {Path(str(args[0])).name} není platné UTF-8 ({exc}). Chybí v jobagent.spec "
                         f"volba ('X utf8', None, 'OPTION')?\n{raw.decode('utf-8', 'replace')}") from None


def smoke_test() -> None:
    step("8/9 Zkouška hotového exe (v dočasné kopii, dist zůstane čistý)")
    # ignore_cleanup_errors: úklid dočasné kopie nesmí shodit build, když zkouška
    # prošla (na Windows může složku chvíli držet antivirus nebo jiný proces).
    with tempfile.TemporaryDirectory(prefix="jobagent-zkouska-", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "JobAgent"
        shutil.copytree(APP, copy)
        exe = copy / f"JobAgent{EXE_SUFFIX}"
        planner = copy / f"JobAgent-planovac{EXE_SUFFIX}"

        code, out = run_exe([exe, "--version"], copy)
        if code != 0 or version() not in out:
            raise BuildError(f"--version: kód {code}, vypsal {out!r}, čekal jsem {version()}.")

        # Bez parametrů = první spuštění: má vzniknout config.toml ze vzoru a kód 2.
        code, out = run_exe([exe], copy)
        if code != 2 or not (copy / "config.toml").is_file():
            raise BuildError(f"První spuštění: kód {code}, config.toml "
                             f"{'vznikl' if (copy / 'config.toml').exists() else 'nevznikl'}.\n{out}")
        if "jsem otevřel v Poznámkovém bloku" in out:  # věta z cli._first_run_if_needed
            raise BuildError("První spuštění při zkoušce otevřelo Poznámkový blok – exe nerespektuje "
                             "JOBAGENT_NO_EDITOR / chybějící terminál.")

        # Druhé spuštění = nápověda. Diakritika ověří kódování i bez přejmenování.
        code, out = run_exe([exe], copy)
        if code != 0 or "Příkazy" not in out:
            raise BuildError(f"Nápověda: kód {code}, chybí v ní „Příkazy“ (diakritika?).\n{out}")

        code, out = run_exe([exe, "sablony"], copy)
        if code != 0 or len(list((copy / "templates").glob("*"))) < 5:
            raise BuildError(f"sablony: kód {code}, nevyexportoval všechny šablony – chybí v _internal?\n{out}")

        # Exe bez okna nemá výstup – UTF-8 režim a jméno ověří úvodní řádek logu.
        code, _ = run_exe([planner, "sablony"], copy)
        log_text = (copy / "logs" / "jobagent.log").read_text(encoding="utf-8")
        if code != 0 or f"(JobAgent-planovac{EXE_SUFFIX}, UTF-8 režim: ano)" not in log_text:
            raise BuildError(f"JobAgent-planovac: kód {code}, v logu chybí „UTF-8 režim: ano“.")
        # Ve Windows musí exe ověřovat certifikáty přes systém (truststore), jinak by na
        # čerstvých Windows selhalo spojení s portály i se SMTP (viz jobagent/tls.py).
        expected_tls = "Ověřování certifikátů: systém (truststore" if IS_WIN else "Ověřování certifikátů: OpenSSL"
        if expected_tls not in log_text:
            raise BuildError(f"V logu exe chybí „{expected_tls}…“ – ověřování certifikátů není nastavené, jak má.")

        # Kamarád si exe může přejmenovat: musí běžet dál a radit příkaz se svým jménem.
        renamed = copy / f"Hledač práce{EXE_SUFFIX}"
        shutil.copy2(exe, renamed)
        code, out = run_exe([renamed], copy)
        if code != 0 or f'"{renamed.name}" run' not in out:
            raise BuildError(f"Přejmenované exe: kód {code}, nápověda neradí \"{renamed.name}\".\n{out}")
        print("  --version, první spuštění (bez Poznámkového bloku), nápověda v UTF-8, sablony, "
              "exe bez okna v UTF-8 režimu, přejmenované exe: v pořádku")
    if Path(tmp).exists():
        print(f"  POZOR: dočasnou kopii {tmp} se nepodařilo smazat (drží ji jiný proces). "
              "Build je v pořádku, složku smaž později ručně.")


def package(commit: str) -> Path:
    step("9/9 Zip")
    platform = "windows" if IS_WIN else sys.platform
    zip_path = DIST / f"JobAgent-{version()}-{platform}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # Kořen zipu: NAVOD.txt vedle složky JobAgent\. Po rozbalení je návod první,
        # co kamarád uvidí – ještě než otevře složku s programem.
        zf.writestr(GUIDE, guide_bytes())
        for path in sorted(APP.rglob("*")):
            zf.write(path, Path("JobAgent") / path.relative_to(APP))
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
