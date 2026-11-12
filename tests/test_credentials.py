"""Fáze 6b: heslo ve Správci pověření Windows, příkazy nastav-heslo / smaz-heslo.

Správce pověření zastupuje FakeStore (slovník). Skutečné volání Windows API
ověřuje jen WindowsStoreTests – běží jen na Windows a po sobě položku smaže.
"""

import argparse
import io
import smtplib
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jobagent import cli, console, credentials, mailer, paths, secrets_file
from jobagent.config import load_config
from jobagent.log import close_logging

from tests import test_mailer as tm

ROOT = Path(__file__).resolve().parent.parent
SECRET = "tajne-heslo-123"


class FakeStore:
    def __init__(self, data=None, fail=False):
        self.data = dict(data or {})
        self.fail = fail
        self.users = {}

    def read(self, target):
        if self.fail:
            raise credentials.CredentialError("Správce pověření Windows: čtení hesla selhalo (test).")
        return self.data.get(target)

    def write(self, target, user, secret):
        self.data[target] = secret
        self.users[target] = user

    def delete(self, target):
        return self.data.pop(target, None) is not None


TARGET = credentials.target_name("agent@example.com", "smtp.example.com")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.email = tm.cfg()
        self.store = FakeStore()
        for p in (mock.patch.object(paths, "app_dir", return_value=self.app),
                  mock.patch.object(cli, "load_config", side_effect=lambda *a, **k: self.cfg),
                  mock.patch.object(credentials, "default_store", side_effect=lambda: self.store),
                  mock.patch.object(credentials, "available", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(close_logging)

    def write_secrets(self, text: str) -> Path:
        path = self.app / "secrets.toml"
        path.write_text(text, encoding="utf-8")
        return path


class ResolveTests(Base):
    def test_store_wins_and_names_source(self):
        self.store.data[TARGET] = SECRET
        cfg = cli._load_config_with_password()
        self.assertEqual(cfg.secrets.email_password, SECRET)
        self.assertEqual(cfg.secrets.email_password_source, "Správce pověření Windows")
        self.assertEqual(cfg.warnings, [])

    def test_both_places_warns(self):
        self.store.data[TARGET] = SECRET
        self.cfg.secrets.email_password = "stare"
        cfg = cli._load_config_with_password()
        self.assertEqual(cfg.secrets.email_password, SECRET)
        self.assertTrue(any("i v secrets.toml" in w for w in cfg.warnings))

    def test_nothing_stored_keeps_secrets_toml(self):
        self.cfg.secrets.email_password = "zesouboru"
        self.assertEqual(cli._load_config_with_password().secrets.email_password, "zesouboru")

    def test_store_error_falls_back_with_warning(self):
        self.store.fail = True
        self.cfg.secrets.email_password = "zesouboru"
        cfg = cli._load_config_with_password()
        self.assertEqual(cfg.secrets.email_password, "zesouboru")
        self.assertTrue(any("čtení hesla selhalo" in w for w in cfg.warnings))

    def test_other_server_is_other_password(self):
        self.store.data[TARGET] = SECRET
        self.cfg.email.smtp_server = "smtp.gmail.com"
        self.assertEqual(cli._load_config_with_password().secrets.email_password, "")

    def test_no_store_outside_windows(self):
        self.assertIsNone(credentials.stored_password(None, "a@b.cz", "smtp"))
        self.assertIsNone(credentials.stored_password(FakeStore({TARGET: "x"}), "", "smtp.example.com"))


class SetPasswordTests(Base):
    def run_cmd(self, password=f"  {SECRET}\n", answers=(), verify_error=None, bez_overeni=False, can_ask=True):
        out = io.StringIO()
        with mock.patch.object(console, "can_ask", return_value=can_ask), \
                mock.patch.object(cli.getpass, "getpass", return_value=password), \
                mock.patch("builtins.input", side_effect=list(answers)) as ask, \
                mock.patch.object(mailer, "verify_login", side_effect=verify_error) as verify, \
                redirect_stdout(out), self.assertLogs("jobagent", level="INFO") as logs:
            code = cli.cmd_set_password(argparse.Namespace(bez_overeni=bez_overeni))
        self.logs = logs.output + [out.getvalue()]
        self.assertFalse(any(SECRET in line for line in self.logs), "heslo se nesmí objevit ve výpisu")
        return code, verify, ask

    def test_verified_password_is_stored_trimmed(self):
        code, verify, ask = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertEqual(self.store.data[TARGET], SECRET)  # bez mezer a konce řádku z vložení
        self.assertEqual(self.store.users[TARGET], "agent@example.com")
        self.assertEqual(verify.call_args.args[1], SECRET)
        ask.assert_not_called()  # secrets.toml neexistuje → žádný dotaz

    def test_offers_to_clear_secrets_toml(self):
        path = self.write_secrets('# návod\n[email]\n# komentář\nheslo = "stare"\n\n[ai]\napi_klic = "k"\n')
        code, _, ask = self.run_cmd(answers=[""])  # Enter = výchozí ano
        self.assertEqual(code, 0)
        text = path.read_text(encoding="utf-8")
        self.assertIn('heslo = ""', text)
        self.assertNotIn("stare", text)
        self.assertIn("# komentář", text)
        self.assertIn('api_klic = "k"', text)
        self.assertFalse((self.app / "secrets.toml.zaloha").exists())  # záloha by heslo jen přesunula

    def test_keep_secrets_toml_when_declined(self):
        path = self.write_secrets('[email]\nheslo = "stare"\n')
        self.run_cmd(answers=["n"])
        self.assertIn("stare", path.read_text(encoding="utf-8"))
        self.assertTrue(any("dál obsahuje" in line for line in self.logs))

    def test_wrong_password_not_stored(self):
        error = mailer.MailError("Server odmítl přihlášení.", transient=False)
        code, _, ask = self.run_cmd(verify_error=error)
        self.assertEqual(code, 3)
        self.assertEqual(self.store.data, {})
        ask.assert_not_called()  # u špatného hesla se neptá

    def test_network_down_asks_default_no(self):
        error = mailer.MailError("Server neodpovídá.", transient=True)
        code, _, _ = self.run_cmd(verify_error=error, answers=[""])
        self.assertEqual(code, 3)
        self.assertEqual(self.store.data, {})

    def test_network_down_store_anyway(self):
        error = mailer.MailError("Server neodpovídá.", transient=True)
        code, _, _ = self.run_cmd(verify_error=error, answers=["a"])
        self.assertEqual(code, 0)
        self.assertEqual(self.store.data[TARGET], SECRET)

    def test_without_verification(self):
        code, verify, _ = self.run_cmd(bez_overeni=True)
        self.assertEqual(code, 0)
        verify.assert_not_called()

    def test_empty_input(self):
        code, verify, _ = self.run_cmd(password="   ")
        self.assertEqual(code, 1)
        verify.assert_not_called()
        self.assertEqual(self.store.data, {})

    def test_needs_user_in_config(self):
        self.cfg.email.user = ""
        with self.assertLogs("jobagent", level="ERROR"):
            self.assertEqual(cli.cmd_set_password(argparse.Namespace(bez_overeni=False)), 2)

    def test_needs_window(self):
        code, _, _ = self.run_cmd(can_ask=False)
        self.assertEqual(code, 1)
        self.assertEqual(self.store.data, {})

    def test_not_windows(self):
        with mock.patch.object(credentials, "available", return_value=False), \
                self.assertLogs("jobagent", level="ERROR") as logs:
            self.assertEqual(cli.cmd_set_password(argparse.Namespace(bez_overeni=False)), 2)
        self.assertTrue(any("secrets.toml" in line for line in logs.output))


class DeletePasswordTests(Base):
    def test_delete(self):
        self.store.data[TARGET] = SECRET
        with self.assertLogs("jobagent", level="INFO") as logs:
            self.assertEqual(cli.cmd_delete_password(argparse.Namespace()), 0)
        self.assertEqual(self.store.data, {})
        self.assertTrue(any("smazáno" in line for line in logs.output))

    def test_delete_nothing(self):
        with self.assertLogs("jobagent", level="INFO") as logs:
            self.assertEqual(cli.cmd_delete_password(argparse.Namespace()), 0)
        self.assertTrue(any("žádné heslo" in line for line in logs.output))


class SecretsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "secrets.toml"

    def test_example_has_no_password(self):
        self.assertFalse(secrets_file.has_email_password(ROOT / "secrets.example.toml"))

    def test_clears_only_email_password_keeps_crlf_and_bom(self):
        original = '# heslo = "v komentáři"\r\n[ai]\r\nheslo = "jine"\r\n[email]\r\nheslo = "x"  # moje\r\n'
        self.path.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
        self.assertTrue(secrets_file.clear_email_password(self.path))
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn('[ai]\r\nheslo = "jine"\r\n', text)  # jiná sekce beze změny
        self.assertIn('# heslo = "v komentáři"', text)
        self.assertIn(secrets_file.CLEARED_LINE + "\r\n", text)
        self.assertFalse(secrets_file.has_email_password(self.path))

    def test_nothing_to_clear(self):
        self.path.write_text('[email]\nheslo = ""\n', encoding="utf-8")
        self.assertFalse(secrets_file.clear_email_password(self.path))
        self.assertFalse(secrets_file.clear_email_password(self.path.with_name("neni.toml")))

    def test_cp1250_file_left_alone(self):
        text = '# poznámka\n[email]\nheslo = "žluťoučký"\n'
        self.path.write_bytes(text.encode("cp1250"))
        self.assertFalse(secrets_file.has_email_password(self.path))
        self.assertFalse(secrets_file.clear_email_password(self.path))
        self.assertEqual(self.path.read_bytes(), text.encode("cp1250"))

    def test_multiline_string_left_alone(self):
        text = '[email]\nheslo = """x\ny"""\n'
        self.path.write_text(text, encoding="utf-8")
        self.assertFalse(secrets_file.clear_email_password(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)


class VerifyLoginTests(unittest.TestCase):
    def setUp(self):
        tm.FakeSMTP.instances, tm.FakeSMTP.fail = [], []

    def test_login_only_nothing_sent(self):
        notes = []
        mailer.verify_login(tm.cfg(), "heslo", progress=notes.append, smtp_ssl=tm.FakeSMTP, smtp_plain=tm.FakeSMTP)
        self.assertEqual(tm.FakeSMTP.instances[0].calls, [("login", "agent@example.com"), "quit"])
        self.assertTrue(any("Přihlášení" in n for n in notes))

    def test_auth_error_explained(self):
        tm.FakeSMTP.fail = [smtplib.SMTPAuthenticationError(535, b"bad")]
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.verify_login(tm.cfg(), "x", progress=lambda n: None, smtp_ssl=tm.FakeSMTP, smtp_plain=tm.FakeSMTP)
        self.assertFalse(ctx.exception.transient)

    def test_login_failure_still_quits(self):
        class FailingLogin(tm.FakeSMTP):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"bad")
        with self.assertRaises(mailer.MailError):
            mailer.verify_login(tm.cfg(), "x", progress=lambda n: None, smtp_ssl=FailingLogin, smtp_plain=FailingLogin)
        self.assertIn("quit", tm.FakeSMTP.instances[0].calls)


@unittest.skipUnless(sys.platform == "win32", "Správce pověření je jen ve Windows")
class WindowsStoreTests(unittest.TestCase):
    """Skutečné volání Windows API. Zapíše dočasnou položku a hned ji smaže."""

    def test_roundtrip(self):
        store = credentials.WindowsStore()
        target = f"JobAgent-unittest/{uuid.uuid4()}"
        self.addCleanup(lambda: store.delete(target))
        self.assertIsNone(store.read(target))
        store.write(target, "test@example.com", "Žluťoučký kůň 123")  # diakritika = UTF-16
        self.assertEqual(store.read(target), "Žluťoučký kůň 123")
        store.write(target, "test@example.com", "jine")  # přepsání
        self.assertEqual(store.read(target), "jine")
        self.assertTrue(store.delete(target))
        self.assertFalse(store.delete(target))
        self.assertIsNone(store.read(target))


if __name__ == "__main__":
    unittest.main()
