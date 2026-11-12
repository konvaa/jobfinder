"""Fáze 6c: Plánovač úloh (XML úlohy, install/uninstall přes schtasks) a běh na pozadí.

schtasks se nespouští: FakeSchtasks si zapamatuje parametry a u /Create
přečte vygenerovaný XML soubor (musí existovat jen po dobu volání).
Skutečné přijetí XML Plánovačem ověří až test v Sandboxu.
"""

import argparse
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest import mock

from jobagent import cli, console, paths, schedule
from jobagent.config import load_config
from jobagent.log import close_logging

from tests import test_mailer as tm

ROOT = Path(__file__).resolve().parent.parent
NS = schedule.NS
CMD = schedule.Command(Path(r"C:\Users\Uzivatel\Stažené soubory\Job Agent & spol\JobAgent-planovac.exe"), "run",
                       Path(r"C:\Users\Uzivatel\Stažené soubory\Job Agent & spol"))


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def text(root: ET.Element, path: str) -> str:
    return root.findtext(path, None, NS)


class FakeSchtasks:
    def __init__(self, existing_xml: str | None = None, create_code=0, delete_code=0):
        self.calls, self.created_xml = [], None
        self.existing_xml = existing_xml
        self.create_code, self.delete_code = create_code, delete_code

    def __call__(self, args, capture_output=True):
        self.calls.append(args)
        action = args[1]
        if action == "/Create":
            xml_path = Path(args[args.index("/XML") + 1])
            raw = xml_path.read_bytes()
            assert raw.startswith(b"\xff\xfe"), "XML má být UTF-16 s BOM"
            self.created_xml = raw.decode("utf-16")
            err = "CHYBA: Přístup byl odepřen.".encode("cp852") if self.create_code else b""
            return subprocess.CompletedProcess(args, self.create_code, b"", err)
        if action == "/Query":
            if self.existing_xml is None:
                return subprocess.CompletedProcess(args, 1, b"", b"ERROR: The system cannot find the file specified.")
            return subprocess.CompletedProcess(args, 0, self.existing_xml.encode("utf-16"), b"")
        if action == "/Delete":
            return subprocess.CompletedProcess(args, self.delete_code, b"", b"")
        raise AssertionError(args)


class TimeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(schedule.parse_time(None), (7, 30))
        self.assertEqual(schedule.parse_time("7:30"), (7, 30))
        self.assertEqual(schedule.parse_time("07:05"), (7, 5))
        self.assertEqual(schedule.parse_time("18.45"), (18, 45))
        self.assertEqual(schedule.parse_time(" 0:00 "), (0, 0))

    def test_invalid(self):
        for bad in ("24:00", "7:60", "7", "sedm", "7:5", "7:30:00"):
            with self.assertRaises(schedule.ScheduleError, msg=bad):
                schedule.parse_time(bad)


class XmlTests(unittest.TestCase):
    def setUp(self):
        self.root = parse(schedule.task_xml(CMD, 7, 5, start_day=date(2026, 11, 12), user="PC\\uzivatel"))

    def test_trigger_daily_with_random_delay(self):
        self.assertEqual(text(self.root, ".//t:CalendarTrigger/t:StartBoundary"), "2026-11-12T07:05:00")
        self.assertEqual(text(self.root, ".//t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"), "1")
        self.assertEqual(text(self.root, ".//t:CalendarTrigger/t:RandomDelay"), "PT15M")

    def test_settings_from_plan(self):
        expected = {
            "StartWhenAvailable": "true",          # zmeškaný start proběhne po zapnutí
            "DisallowStartIfOnBatteries": "false",  # notebook na baterii
            "StopIfGoingOnBatteries": "false",
            "MultipleInstancesPolicy": "IgnoreNew",
            "ExecutionTimeLimit": "PT2H",
            "RunOnlyIfNetworkAvailable": "false",
            "Enabled": "true",
        }
        for name, value in expected.items():
            self.assertEqual(text(self.root, f"./t:Settings/t:{name}"), value, name)

    def test_logged_on_only_without_admin(self):
        self.assertEqual(text(self.root, ".//t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(text(self.root, ".//t:Principal/t:RunLevel"), "LeastPrivilege")
        self.assertEqual(text(self.root, ".//t:Principal/t:UserId"), "PC\\uzivatel")
        self.assertNotIn("Password", schedule.task_xml(CMD, 7, 5))

    def test_paths_with_spaces_diacritics_ampersand(self):
        self.assertEqual(text(self.root, ".//t:Exec/t:Command"), f'"{CMD.program}"')
        self.assertEqual(text(self.root, ".//t:Exec/t:Arguments"), "run")
        self.assertEqual(text(self.root, ".//t:Exec/t:WorkingDirectory"), str(CMD.workdir))

    def test_without_user(self):
        root = parse(schedule.task_xml(CMD, 7, 30, user=None))
        self.assertIsNone(root.find(".//t:Principal/t:UserId", NS))


class InstallTests(unittest.TestCase):
    def test_install_passes_xml_file_and_cleans_up(self):
        fake = FakeSchtasks()
        schedule.install(CMD, 7, 30, runner=fake, user="PC\\uzivatel")
        args = fake.calls[0]
        self.assertEqual(args[:4], ["schtasks", "/Create", "/TN", "Job Agent"])
        self.assertIn("/F", args)  # přepsat existující (nový čas / přesunutá složka)
        self.assertFalse(Path(args[args.index("/XML") + 1]).exists())  # dočasný soubor smazán
        self.assertEqual(text(parse(fake.created_xml), ".//t:CalendarTrigger/t:StartBoundary")[11:], "07:30:00")

    def test_install_failure_shows_message(self):
        with self.assertRaises(schedule.ScheduleError) as ctx:
            schedule.install(CMD, 7, 30, runner=FakeSchtasks(create_code=1), user=None)
        self.assertIn("Přístup byl odepřen", str(ctx.exception))  # cp852 z české konzole

    def test_query_reads_back_task(self):
        fake = FakeSchtasks(existing_xml=schedule.task_xml(CMD, 18, 5))
        info = schedule.query(runner=fake)
        self.assertEqual(info.program, str(CMD.program))  # bez uvozovek
        self.assertEqual(info.time, "18:05")
        self.assertEqual(fake.calls[0], ["schtasks", "/Query", "/TN", "Job Agent", "/XML"])

    def test_query_missing(self):
        self.assertIsNone(schedule.query(runner=FakeSchtasks()))

    def test_uninstall(self):
        fake = FakeSchtasks(existing_xml=schedule.task_xml(CMD, 7, 30))
        self.assertTrue(schedule.uninstall(runner=fake))
        self.assertEqual(fake.calls[-1], ["schtasks", "/Delete", "/TN", "Job Agent", "/F"])

    def test_uninstall_nothing(self):
        fake = FakeSchtasks()
        self.assertFalse(schedule.uninstall(runner=fake))
        self.assertEqual(len(fake.calls), 1)  # jen dotaz, žádné mazání

    @unittest.skipIf(sys.platform == "win32", "mimo Windows")
    def test_not_windows(self):
        with self.assertRaises(schedule.ScheduleError):
            schedule.query()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_frozen_uses_planner_exe(self):
        (self.dir / "JobAgent.exe").write_bytes(b"")
        (self.dir / schedule.PLANNER_EXE).write_bytes(b"")
        command = schedule.planner_command(True, self.dir / "JobAgent.exe", self.dir)
        self.assertEqual(command.program, (self.dir / schedule.PLANNER_EXE).resolve())
        self.assertEqual(command.arguments, "run")
        self.assertEqual(command.workdir, self.dir.resolve())

    def test_frozen_without_planner_exe(self):
        with self.assertRaises(schedule.ScheduleError) as ctx:
            schedule.planner_command(True, self.dir / "JobAgent.exe", self.dir)
        self.assertIn("nesmí se přejmenovat", str(ctx.exception))

    def test_source_prefers_pythonw(self):
        (self.dir / "python.exe").write_bytes(b"")
        (self.dir / "pythonw.exe").write_bytes(b"")
        command = schedule.planner_command(False, self.dir / "python.exe", Path("/projekt"))
        self.assertEqual(command.program.name, "pythonw.exe")
        self.assertEqual(command.arguments, "-m jobagent run")
        self.assertEqual(command.workdir, Path("/projekt"))

    def test_source_without_pythonw(self):
        (self.dir / "python3").write_bytes(b"")
        self.assertEqual(schedule.planner_command(False, self.dir / "python3", self.dir).program.name, "python3")

    def test_moved_warning(self):
        info = schedule.TaskInfo(program=r"D:\stara\JobAgent-planovac.exe", time="07:30")
        self.assertIn("install-schedule", schedule.moved_warning(info, Path(r"F:\nova\JobAgent-planovac.exe")))
        same = schedule.TaskInfo(program=str(CMD.program), time="07:30")
        self.assertIsNone(schedule.moved_warning(same, CMD.program))
        self.assertIsNone(schedule.moved_warning(None, CMD.program))


class CliScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.email = tm.cfg()
        self.cfg.secrets.email_password = "x"
        for p in (mock.patch.object(paths, "app_dir", return_value=Path(self.tmp.name)),
                  mock.patch.object(cli, "load_config", side_effect=lambda *a, **k: self.cfg),
                  mock.patch.object(cli.credentials, "default_store", return_value=None),
                  mock.patch.object(schedule, "planner_command", return_value=CMD)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(close_logging)

    def test_install_default_time(self):
        with mock.patch.object(schedule, "install") as install, self.assertLogs("jobagent", level="INFO") as logs:
            self.assertEqual(cli.cmd_install_schedule(argparse.Namespace(cas=None)), 0)
        self.assertEqual(install.call_args.args[1:], (7, 30))
        self.assertTrue(any("každý den v 7:30" in line for line in logs.output))
        self.assertFalse(any("WARNING" in line for line in logs.output))

    def test_install_warns_when_mail_not_ready(self):
        self.cfg.secrets.email_password = ""
        with mock.patch.object(schedule, "install"), self.assertLogs("jobagent", level="WARNING") as logs:
            self.assertEqual(cli.cmd_install_schedule(argparse.Namespace(cas="6:15")), 0)
        self.assertTrue(any("nepůjde odeslat" in line and "nastav-heslo" in line for line in logs.output))

    def test_install_bad_time(self):
        with mock.patch.object(schedule, "install") as install, self.assertLogs("jobagent", level="ERROR"):
            self.assertEqual(cli.cmd_install_schedule(argparse.Namespace(cas="25:00")), 2)
        install.assert_not_called()

    def test_install_failure(self):
        error = schedule.ScheduleError("Plánovač úloh úlohu nevytvořil: …")
        with mock.patch.object(schedule, "install", side_effect=error), self.assertLogs("jobagent", level="ERROR"):
            self.assertEqual(cli.cmd_install_schedule(argparse.Namespace(cas=None)), 1)

    def test_uninstall_messages(self):
        with mock.patch.object(schedule, "uninstall", return_value=False), \
                self.assertLogs("jobagent", level="INFO") as logs:
            self.assertEqual(cli.cmd_uninstall_schedule(argparse.Namespace()), 0)
        self.assertTrue(any("není co rušit" in line for line in logs.output))

    def test_moved_check_only_for_exe(self):
        moved = schedule.TaskInfo(program=r"D:\stara\JobAgent-planovac.exe", time="07:30")
        with mock.patch.object(schedule, "query", return_value=moved) as query:
            self.assertIsNone(cli._schedule_moved_warning())  # ze zdrojáků se nekontroluje
            query.assert_not_called()
            with mock.patch.object(cli.sys, "frozen", True, create=True), \
                    mock.patch.object(cli.sys, "executable", str(Path(self.tmp.name) / "JobAgent.exe")):
                self.assertIn("install-schedule", cli._schedule_moved_warning())

    def test_moved_check_never_breaks_run(self):
        with mock.patch.object(cli.sys, "frozen", True, create=True), \
                mock.patch.object(schedule, "query", side_effect=schedule.ScheduleError("x")):
            self.assertIsNone(cli._schedule_moved_warning())


class BackgroundTests(unittest.TestCase):
    def test_double_click_on_planner_exe_shows_message(self):
        with mock.patch.object(console, "in_background", return_value=True), \
                mock.patch.object(console, "silence_missing_streams"), \
                mock.patch.object(console, "message_box") as box, \
                mock.patch.object(cli, "_main") as real_main:
            self.assertEqual(cli.main([]), 0)
        self.assertIn("JobAgent.exe", box.call_args.args[0])
        real_main.assert_not_called()

    def test_scheduled_run_goes_on(self):
        with mock.patch.object(console, "in_background", return_value=True), \
                mock.patch.object(console, "silence_missing_streams"), \
                mock.patch.object(console, "message_box") as box, \
                mock.patch.object(cli, "_main", return_value=0) as real_main:
            self.assertEqual(cli.main(["run"]), 0)
        box.assert_not_called()
        real_main.assert_called_once_with(["run"])

    def test_missing_streams_replaced(self):
        with mock.patch.object(console.sys, "stdout", None), mock.patch.object(console.sys, "stderr", None):
            self.assertTrue(console.in_background())
            console.silence_missing_streams()
            print("nic", file=console.sys.stderr)  # nesmí spadnout
            self.assertFalse(console.in_background())
            console.sys.stdout.close()
            console.sys.stderr.close()


if __name__ == "__main__":
    unittest.main()
