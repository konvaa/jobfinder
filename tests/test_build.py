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
        (self.app / "JobAgent.exe").write_bytes(b"")
        (self.app / "config.example.toml").write_text("x", encoding="utf-8")
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
        for folder in ("logs", "vystup", "templates"):
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

    def test_example_files_allowed(self):
        (self.app / "secrets.example.toml").write_text("x", encoding="utf-8")
        self.check()


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
