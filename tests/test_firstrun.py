"""Fáze 6a: první spuštění, spuštění bez parametrů (dvojklik) a šablony mimo program."""

import argparse
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jobagent import cli, console, firstrun, paths
from jobagent.log import close_logging
from jobagent.templating import DEFAULT_DIR, TEMPLATE_FILES, Templates, export_defaults, placeholders

ROOT = Path(__file__).resolve().parent.parent


class TempApp(unittest.TestCase):
    """Složka aplikace v dočasném adresáři, jako by tam ležel JobAgent.exe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        patcher = mock.patch.object(paths, "app_dir", return_value=self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Až po tmp.cleanup zaregistrované = proběhne dřív (LIFO): log se zavře
        # dřív, než se maže složka (na Windows by jinak byl zamčený).
        self.addCleanup(close_logging)

    def main(self, *argv, own_window=False):
        out = io.StringIO()
        with mock.patch.object(console, "owns_console", return_value=own_window), \
                mock.patch.object(console, "open_for_edit", return_value=False), redirect_stdout(out), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            code = cli.main(list(argv))
        return code, out.getvalue()


class FirstRunTests(TempApp):
    def test_no_arguments_creates_config_and_explains(self):
        code, out = self.main()
        self.assertEqual(code, 2)
        config = self.app / "config.toml"
        self.assertEqual(config.read_bytes(), (ROOT / "config.example.toml").read_bytes())
        self.assertIn("Vítej v Job Agentovi", out)
        self.assertIn(str(config), out)
        self.assertIn("test-email", out)
        self.assertFalse((self.app / "secrets.toml").exists())  # heslo patří do Správce pověření (6b)

    def test_second_start_only_shows_help(self):
        self.main()
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertNotIn("Vítej", out)
        self.assertIn("run --dry-run", out)

    def test_existing_config_never_overwritten(self):
        config = self.app / "config.toml"
        config.write_text("# můj\n", encoding="utf-8")
        self.assertFalse(firstrun.ensure_config(config, ROOT / "config.example.toml"))
        self.assertEqual(config.read_text(encoding="utf-8"), "# můj\n")

    def test_run_without_config_stops_before_collecting(self):
        with mock.patch.object(cli, "cmd_run") as cmd_run:
            code, out = self.main("run", "--dry-run")
        self.assertEqual(code, 2)
        cmd_run.assert_not_called()
        self.assertTrue((self.app / "config.toml").exists())

    def test_sample_does_not_create_config(self):
        with mock.patch.object(cli, "cmd_sample", return_value=0):
            self.main("sample", "https://www.jobs.cz/rpd/1/")
        self.assertFalse((self.app / "config.toml").exists())

    def test_command_name_in_messages(self):
        self.assertEqual(firstrun.command_name(True), "JobAgent.exe")
        self.assertIn("JobAgent.exe test-email", firstrun.welcome_text(Path("C:/x/config.toml"), "JobAgent.exe"))


class OwnWindowTests(TempApp):
    def test_pause_when_started_by_double_click(self):
        (self.app / "config.toml").write_bytes((ROOT / "config.example.toml").read_bytes())
        with mock.patch("builtins.input", return_value="") as pause:
            self.main(own_window=True)
        pause.assert_called_once()

    def test_pause_even_after_argparse_error(self):
        with mock.patch("builtins.input", return_value="") as pause, self.assertRaises(SystemExit):
            self.main("neexistujici-prikaz", own_window=True)
        pause.assert_called_once()

    def test_no_pause_from_command_line(self):
        with mock.patch("builtins.input") as pause:
            self.main()
        pause.assert_not_called()

    def test_console_detection_outside_windows(self):
        with mock.patch.object(console.sys, "platform", "linux"):
            self.assertFalse(console.owns_console())
            self.assertFalse(console.open_for_edit(Path("x.toml")))


class TemplatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.custom = Path(self.tmp.name) / "templates"

    def test_defaults_without_override(self):
        templates = Templates(self.custom)
        self.assertEqual(templates.source("email.html"), DEFAULT_DIR / "email.html")
        self.assertEqual(templates.check(), [])

    def test_exported_copy_is_clean(self):
        created, skipped = export_defaults(self.custom)
        self.assertEqual([p.name for p in created], list(TEMPLATE_FILES))
        self.assertEqual(skipped, [])
        self.assertEqual(Templates(self.custom).check(), [])

    def test_export_keeps_existing(self):
        self.custom.mkdir()
        (self.custom / "nabidka.html").write_text("moje", encoding="utf-8")
        created, skipped = export_defaults(self.custom)
        self.assertEqual([p.name for p in skipped], ["nabidka.html"])
        self.assertEqual((self.custom / "nabidka.html").read_text(encoding="utf-8"), "moje")
        self.assertEqual(len(created), len(TEMPLATE_FILES) - 1)

    def test_override_per_file(self):
        self.custom.mkdir()
        (self.custom / "ostatni.html").write_text("$pozice", encoding="utf-8")
        templates = Templates(self.custom)
        self.assertEqual(templates.source("ostatni.html"), self.custom / "ostatni.html")
        self.assertEqual(templates.source("email.html"), DEFAULT_DIR / "email.html")

    def test_missing_placeholder_warns(self):
        export_defaults(self.custom)
        page = self.custom / "email.html"
        page.write_text(page.read_text(encoding="utf-8").replace("$priloha", ""), encoding="utf-8")
        warnings = Templates(self.custom).check()
        self.assertEqual(len(warnings), 1)
        self.assertIn("email.html nemá $priloha", warnings[0])
        self.assertIn("sablony", warnings[0])

    def test_unknown_placeholder_warns(self):
        export_defaults(self.custom)
        row = self.custom / "nabidka.html"
        row.write_text(row.read_text(encoding="utf-8") + "$firmaa", encoding="utf-8")
        warnings = Templates(self.custom).check()
        self.assertTrue(any("neznámé $firmaa" in w for w in warnings))

    def test_comments_do_not_count(self):
        # Symbol jen v komentáři (návod) se nepočítá: v e-mailu by nebyl.
        default = placeholders("email.html", (DEFAULT_DIR / "email.html").read_text(encoding="utf-8"))
        only_comment = "<!-- " + " ".join("$" + p for p in default) + " -->"
        self.assertEqual(placeholders("email.html", only_comment), set())
        self.assertEqual(placeholders("email.txt", "## $nabidky\n$datum"), {"datum"})

    def test_notepad_bom_is_fine(self):
        export_defaults(self.custom)
        row = self.custom / "nabidka.html"
        row.write_bytes(b"\xef\xbb\xbf" + row.read_bytes())
        templates = Templates(self.custom)
        self.assertEqual(templates.check(), [])
        self.assertFalse(templates.raw("nabidka.html").startswith("\ufeff"))

    def test_unreadable_file_warns(self):
        self.custom.mkdir()
        (self.custom / "vyrazeno.html").write_bytes(b"\xff\xfe\x00neplatne")
        warnings = Templates(self.custom).check()
        self.assertTrue(any("nejde přečíst" in w for w in warnings))


class TemplatesCommandTests(TempApp):
    def test_sablony_command(self):
        with self.assertLogs("jobagent", level="INFO"):
            code = cli.cmd_templates(argparse.Namespace())
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in (self.app / "templates").iterdir()), sorted(TEMPLATE_FILES))
        # Druhé spuštění nic nepřepíše.
        (self.app / "templates" / "email.txt").write_text("upraveno $datum", encoding="utf-8")
        with self.assertLogs("jobagent", level="INFO") as logs:
            cli.cmd_templates(argparse.Namespace())
        self.assertEqual((self.app / "templates" / "email.txt").read_text(encoding="utf-8"), "upraveno $datum")
        self.assertTrue(any("Už existuje" in line for line in logs.output))


class BundledTests(unittest.TestCase):
    def test_example_config_is_bundled_root(self):
        self.assertEqual(paths.example_config(), ROOT / "config.example.toml")
        self.assertTrue(paths.example_config().exists())

    def test_default_templates_in_package(self):
        self.assertEqual(DEFAULT_DIR, ROOT / "jobagent" / "templates")
        for name in TEMPLATE_FILES:
            self.assertTrue((DEFAULT_DIR / name).is_file(), name)

    def test_frozen_paths(self):
        with mock.patch.object(paths.sys, "frozen", True, create=True), \
                mock.patch.object(paths.sys, "executable", "/opt/JobAgent/JobAgent.exe"), \
                mock.patch.object(paths.sys, "_MEIPASS", "/opt/JobAgent/_internal", create=True):
            self.assertEqual(paths.app_dir(), Path("/opt/JobAgent").resolve())
            self.assertEqual(paths.user_templates_dir(), Path("/opt/JobAgent").resolve() / "templates")
            self.assertEqual(paths.example_config(), Path("/opt/JobAgent/_internal/config.example.toml"))


if __name__ == "__main__":
    unittest.main()
