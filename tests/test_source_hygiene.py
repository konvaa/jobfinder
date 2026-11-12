"""Zdrojáky se musí přeložit bez varování (SyntaxWarning = chyba).

Proč test, a ne jen `python -W error -m compileall` v build skriptu: běžné
`python -m unittest` varování při překladu neukáže výrazně (a z __pycache__
vůbec), takže by se chyba typu '\\ ' v docstringu našla až u buildu nebo u
uživatele. Python 3.12 dnes neplatné escape sekvence jen varuje, budoucí verze
je mají hlásit jako SyntaxError – program by pak nešel spustit vůbec.

compile() se volá na čerstvý zdroják (ne přes import), takže varování přijde
vždy, i když už existuje .pyc. A projdou se všechny soubory, ne jen první
chybný jako u compileall.
"""

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


if __name__ == "__main__":
    unittest.main()
