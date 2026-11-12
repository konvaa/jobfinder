"""Zdrojáky se musí přeložit bez varování (SyntaxWarning = chyba).

Proč test, a ne jen `python -W error -m compileall` v build skriptu: běžné
`python -m unittest` varování při překladu neukáže výrazně (a z __pycache__
vůbec), takže by se chyba typu '\\ ' v docstringu našla až u buildu nebo u
uživatele. Python 3.12 dnes neplatné escape sekvence jen varuje, budoucí verze
je mají hlásit jako SyntaxError – program by pak nešel spustit vůbec.

compile() se volá na čerstvý zdroják (ne přes import), takže varování přijde
vždy, i když už existuje .pyc. A projdou se všechny soubory, ne jen první
chybný jako u compileall.

Kódování (ExplicitEncodingTests): bez encoding= čte a zapisuje Python
soubory kódováním stroje – na Linuxu UTF-8, na českých Windows cp1250. Testy
v kontejneru by chybu nikdy neukázaly, exe u kamaráda ano. Proto statická
kontrola každého volání open() / read_text() / write_text() / subprocess
s text=True a NamedTemporaryFile v textovém režimu. Dynamickou kontrolu
(-X warn_default_encoding -W error::EncodingWarning) dělá tools/build.py
při druhém běhu testů – ta ale prověří jen kód, kterým testy projdou.
"""

import ast
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKED_DIRS = ("jobagent", "tests", "tools")


def compile_strict(source: str, filename: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # SyntaxWarning, DeprecationWarning při překladu → výjimka
        compile(source, filename, "exec", dont_inherit=True)


class SourceHygieneTests(unittest.TestCase):
    def test_all_sources_compile_without_warnings(self):
        problems = []
        files = sorted(p for d in CHECKED_DIRS for p in (ROOT / d).rglob("*.py"))
        self.assertGreater(len(files), 20)  # pojistka, že se opravdu něco kontroluje
        for path in files:
            try:
                compile_strict(path.read_text(encoding="utf-8"), str(path))
            except (SyntaxError, SyntaxWarning, DeprecationWarning) as exc:
                problems.append(f"{path.relative_to(ROOT)}:{getattr(exc, 'lineno', '?')}: {exc}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_check_catches_invalid_escape(self):
        # Přesně chyba z paths.py: zpětné lomítko před mezerou v docstringu.
        with self.assertRaises((SyntaxError, SyntaxWarning)):
            compile_strict('"""U exe je to _internal\\ (sys._MEIPASS)."""\n', "ukazka.py")
        compile_strict('r"""U exe je to _internal\\ (sys._MEIPASS)."""\n', "ukazka.py")  # raw = v pořádku


def _mode(call: ast.Call, position: int) -> str | None:
    """Režim souboru z volání (pozičně nebo mode=), pokud je to řetězec v kódu."""
    node = next((k.value for k in call.keywords if k.arg == "mode"), None)
    if node is None and len(call.args) > position:
        node = call.args[position]
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _is_true(call: ast.Call, name: str) -> bool:
    return any(k.arg == name and isinstance(k.value, ast.Constant) and k.value.value is True for k in call.keywords)


def missing_encoding(source: str, filename: str) -> list[str]:
    """Řádky, kde se soubor nebo roura otevírá v textovém režimu bez encoding=."""
    problems = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if _has_kw(node, "encoding"):
            continue
        problem = None
        if name == "open" and isinstance(func, ast.Name):
            mode = _mode(node, 1)
            if mode is None or "b" not in mode:  # bez režimu = "r" = textový
                problem = "open() bez encoding="
        elif name == "open" and isinstance(func, ast.Attribute):
            # Path.open("w") apod. Jiná .open (urllib opener, webbrowser) režim nemají – nehlásit.
            mode = _mode(node, 0)
            if mode is not None and "b" not in mode and set(mode) <= set("rwax+t"):
                problem = ".open() v textovém režimu bez encoding="
        elif name == "read_text" and not node.args:
            problem = "read_text() bez encoding="
        elif name == "write_text" and len(node.args) < 2:
            problem = "write_text() bez encoding="
        elif name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "TextIOWrapper"):
            mode = _mode(node, 0)
            if name == "TextIOWrapper" or (mode is not None and "b" not in mode):
                problem = f"{name}() v textovém režimu bez encoding="
        elif _is_true(node, "text") or _is_true(node, "universal_newlines"):
            problem = "podproces s text=True bez encoding= (dekódoval by výstup kódováním stroje)"
        if problem:
            problems.append(f"{filename}:{node.lineno}: {problem}")
    return problems


class ExplicitEncodingTests(unittest.TestCase):
    def test_every_text_file_access_has_encoding(self):
        problems = []
        for path in sorted(p for d in CHECKED_DIRS for p in (ROOT / d).rglob("*.py")):
            problems += missing_encoding(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        problems += missing_encoding((ROOT / "jobagent.spec").read_text(encoding="utf-8"), "jobagent.spec")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_checker_catches_typical_mistakes(self):
        bad = """
open("a.txt")
open("a.txt", "w")
Path("a").read_text()
Path("a").write_text("x")
Path("a").open("w")
subprocess.run(["git"], text=True)
tempfile.NamedTemporaryFile("w")
"""
        self.assertEqual(len(missing_encoding(bad, "ukazka.py")), 7)
        good = """
open("a.bin", "rb")
open("a.txt", encoding="utf-8")
Path("a").read_text(encoding="utf-8")
Path("a").read_text("utf-8")
Path("a").write_text("x", encoding="utf-8")
Path("a").read_bytes()
opener.open(request, timeout=5)
webbrowser.open(url)
subprocess.run(["git"], capture_output=True)
subprocess.run(["git"], text=True, encoding="utf-8")
tempfile.NamedTemporaryFile()
"""
        self.assertEqual(missing_encoding(good, "ukazka.py"), [])


class RequirementsAsciiTests(unittest.TestCase):
    """pip čte requirements kódováním systému (cp1250 na českých Windows) – jen ASCII."""

    def test_requirements_are_ascii(self):
        files = sorted(ROOT.glob("requirements*.txt"))
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            for number, line in enumerate(path.read_bytes().splitlines(), 1):
                with self.subTest(file=path.name, line=number):
                    self.assertTrue(line.isascii(), f"{path.name}:{number}: znak mimo ASCII: {line!r}")


if __name__ == "__main__":
    unittest.main()
