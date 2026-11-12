"""Fáze 6d: kontrola obsahu balíku v tools/build.py.

Nejdůležitější krok buildu z pohledu bezpečnosti: do zipu pro kamarády se
nesmí dostat config.toml, secrets.toml, databáze, logy ani výstupy – ani když
by je někdo omylem nechal ve složce dist\\JobAgent\\. Samotný PyInstaller se
v testech nespouští (trvá dlouho a potřebuje .venv-build).
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_tool", ROOT / "tools" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


class ContentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "JobAgent"
        (self.app / "_internal" / "jobagent" / "templates").mkdir(parents=True)
        (self.app / "_internal" / "jobagent" / "templates" / "email.html").write_text("x", encoding="utf-8")
        (self.app / "_internal" / "config.example.toml").write_text("x", encoding="utf-8")
        for name in build.TOP_LEVEL - {"_internal"}:
            (self.app / name).write_text("x", encoding="utf-8")
        patcher = mock.patch.object(build, "APP", self.app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self):
        with mock.patch("builtins.print"):
            build.check_contents()

    def test_clean_package_passes(self):
        self.check()

    def test_personal_files_fail(self):
        for name in ("config.toml", "secrets.toml", "secrets.local.toml", "config.toml.zaloha", "jobagent.db",
                     "email_2026-11-09_101010.eml"):
            with self.subTest(name=name):
                path = self.app / name
                path.write_text("tajne", encoding="utf-8")
                with self.assertRaises(build.BuildError) as ctx:
                    self.check()
                self.assertIn(name, str(ctx.exception))
                path.unlink()

    def test_runtime_folders_fail(self):
        for folder in ("logs", "vystup", "templates", "licence"):
            with self.subTest(folder=folder):
                (self.app / folder).mkdir()
                with self.assertRaises(build.BuildError):
                    self.check()
                (self.app / folder).rmdir()

    def test_missing_bundled_data_fails(self):
        (self.app / "_internal" / "config.example.toml").unlink()
        with self.assertRaises(build.BuildError) as ctx:
            self.check()
        self.assertIn("config.example.toml", str(ctx.exception))

    def test_unexpected_top_level_file_fails(self):
        # Povolující seznam: i neškodně vypadající soubor navíc nahoře build zastaví.
        (self.app / "poznamky.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(build.BuildError) as ctx:
            self.check()
        self.assertIn("poznamky.txt", str(ctx.exception))

    def test_missing_guide_or_planner_fails(self):
        for name in (build.GUIDE, f"JobAgent-planovac{build.EXE_SUFFIX}"):
            with self.subTest(name=name):
                (self.app / name).unlink()
                with self.assertRaises(build.BuildError) as ctx:
                    self.check()
                self.assertIn(f"CHYBÍ {name}", str(ctx.exception))
                (self.app / name).write_text("x", encoding="utf-8")

    def test_secrets_example_deep_inside_allowed(self):
        (self.app / "_internal" / "secrets.example.toml").write_text("x", encoding="utf-8")
        self.check()


class GuideTests(unittest.TestCase):
    def test_guide_is_notepad_friendly(self):
        data = build.guide_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))  # BOM: Poznámkový blok pozná UTF-8
        body = data[3:]
        self.assertNotIn(b"\n", body.replace(b"\r\n", b""))  # jen CRLF
        self.assertIn("Extrahovat vše".encode("utf-8"), body)


class Utf8ModeTests(unittest.TestCase):
    """Exe má UTF-8 režim (jobagent.spec) a build ho ověřuje (run_exe, log exe bez okna)."""

    def test_spec_enables_utf8_mode_for_both_exes(self):
        spec = (ROOT / "jobagent.spec").read_text(encoding="utf-8")
        self.assertIn('RUNTIME_OPTIONS = [("X utf8", None, "OPTION")]', spec)
        # Volby jsou pro každé exe zvlášť – musí je dostat konzolové i to bez okna.
        self.assertEqual(spec.count("pyz, a.scripts, RUNTIME_OPTIONS,"), 2)
        # 'X utf8_mode=1' dnešní zavaděč tiše ignoruje; smí být jen v komentáři.
        code = "\n".join(line.split("#")[0] for line in spec.splitlines())
        self.assertNotIn("utf8_mode", code)

    def test_strict_decoding_rejects_cp1250(self):
        fake = mock.Mock(returncode=0, stdout="Příkazy: \"Hledač práce.exe\"".encode("cp1250"), stderr=b"")
        with mock.patch.object(build.subprocess, "run", return_value=fake):
            with self.assertRaises(build.BuildError) as ctx:
                build.run_exe([Path("JobAgent.exe")], ROOT)
        self.assertIn("X utf8", str(ctx.exception))

    def test_strict_decoding_accepts_utf8(self):
        fake = mock.Mock(returncode=0, stdout="Příkazy".encode("utf-8"), stderr=b"")
        with mock.patch.object(build.subprocess, "run", return_value=fake):
            self.assertEqual(build.run_exe([Path("JobAgent.exe")], ROOT), (0, "Příkazy"))

    def test_log_line_matches_build_check(self):
        # build hledá v logu „(JobAgent-planovac.exe, UTF-8 režim: ano)“.
        cli = (ROOT / "jobagent" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('"jobagent %s (%s, UTF-8 režim: %s), příkaz %s, složka %s"', cli)
        self.assertIn("UTF-8 režim: ano)", (ROOT / "tools" / "build.py").read_text(encoding="utf-8"))


class MiscTests(unittest.TestCase):
    def test_version_matches_package(self):
        from jobagent import __version__
        self.assertEqual(build.version(), __version__)

    def test_notepad_check_matches_cli_message(self):
        # Zkouška exe v buildu pozná otevřený Poznámkový blok podle věty z cli.py.
        # Když se věta v cli.py změní, kontrola by potichu přestala fungovat.
        phrase = "jsem otevřel v Poznámkovém bloku"
        self.assertIn(phrase, (ROOT / "tools" / "build.py").read_text(encoding="utf-8"))
        self.assertIn(phrase, (ROOT / "jobagent" / "cli.py").read_text(encoding="utf-8"))

    def test_sandbox_config_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(build, "DIST", Path(tmp)):
            build.write_sandbox_config()
            text = (Path(tmp) / "test-sandbox.wsb").read_text(encoding="utf-8")
        self.assertIn("<ReadOnly>true</ReadOnly>", text)
        self.assertIn("WDAGUtilityAccount", text)


if __name__ == "__main__":
    unittest.main()
