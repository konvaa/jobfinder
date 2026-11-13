"""Ověřování certifikátů (tls.py): truststore ve Windows, rady při selhání, chybějící editor.

Skutečné ověření řetězce přes Windows (CryptoAPI) se tady spustit nedá – to
ověří Windows Sandbox (čisté Windows bez předem stažených kořenů). Testy
hlídají, že se kontext opravdu dostane do urllib i smtplib, že se chyba
certifikátu neopakuje a že rada odpovídá příčině.
"""

import io
import ssl
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jobagent import cli, console, fetch, firstrun, mailer, paths, tls
from jobagent.log import close_logging

from tests import test_mailer as tm

CERT_ERROR = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")


class ContextTests(unittest.TestCase):
    def test_windows_uses_truststore(self):
        import truststore
        with mock.patch.object(tls, "_platform", return_value="win32"):
            ctx = tls.create_context()
            self.assertIsInstance(ctx, truststore.SSLContext)
            self.assertIn("truststore", tls.backend())
        self.assertTrue(ctx.check_hostname)  # jméno serveru se kontroluje
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_linux_uses_default_context(self):
        with mock.patch.object(tls, "_platform", return_value="linux"):
            ctx = tls.create_context()
            self.assertIs(type(ctx), ssl.SSLContext)
            self.assertIn("OpenSSL", tls.backend())
        self.assertTrue(ctx.check_hostname)

    def test_windows_without_truststore_falls_back_and_says_so(self):
        with mock.patch.object(tls, "_platform", return_value="win32"), mock.patch.dict(sys.modules, {"truststore": None}):
            self.assertIs(type(tls.create_context()), ssl.SSLContext)
            self.assertIn("chybí knihovna truststore", tls.backend())

    def test_is_cert_error(self):
        self.assertTrue(tls.is_cert_error(CERT_ERROR))
        self.assertTrue(tls.is_cert_error(urllib.error.URLError(CERT_ERROR)))
        self.assertFalse(tls.is_cert_error(urllib.error.URLError(TimeoutError())))
        self.assertFalse(tls.is_cert_error(ssl.SSLError(1, "WRONG_VERSION_NUMBER")))
        self.assertFalse(tls.is_cert_error(None))

    def test_advice(self):
        web = tls.cert_advice("www.jobscontact.cz", mail=False)
        mail = tls.cert_advice("smtp.seznam.cz", mail=True)
        for text in (web, mail):
            self.assertIn("kořenový certifikát", text)
            self.assertIn("Windows Update", text)
            self.assertIn("datum nebo čas", text)
        self.assertIn("Mail Shield", mail)
        self.assertIn("www.jobscontact.cz", web)
        self.assertNotIn("Mail Shield", web)


class FetchTlsTests(unittest.TestCase):
    def test_opener_uses_our_context(self):
        sentinel = ssl.create_default_context()
        with mock.patch.object(tls, "create_context", return_value=sentinel):
            fetcher = fetch.Fetcher(sleep=lambda s: None)
        handlers = [h for h in fetcher._opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertIs(handlers[0]._context, sentinel)
        fetcher.reset_session()  # nová návštěva (cookies) – kontext zůstává
        handlers = [h for h in fetcher._opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertIs(handlers[0]._context, sentinel)

    def test_cert_error_not_retried_and_explained(self):
        sleeps = []
        fetcher = fetch.Fetcher(sleep=sleeps.append, respect_robots=False)
        with mock.patch.object(fetcher, "_request", side_effect=urllib.error.URLError(CERT_ERROR)) as request:
            with self.assertRaises(fetch.FetchError) as ctx:
                fetcher.get("https://www.jobscontact.cz/prace/")
        self.assertEqual(request.call_count, 1)  # žádný zbytečný druhý pokus
        self.assertEqual(sleeps, [])
        self.assertIn("certifikát serveru www.jobscontact.cz", str(ctx.exception))

    def test_cert_error_on_robots_is_not_hidden(self):
        # Dřív: „robots.txt teď nejde načíst, přeskakuji“ – skutečná příčina se ztratila.
        fetcher = fetch.Fetcher(sleep=lambda s: None)
        with mock.patch.object(fetcher, "_request", side_effect=urllib.error.URLError(CERT_ERROR)):
            with self.assertRaises(fetch.FetchError) as ctx:
                fetcher.get("https://www.jobs.cz/prace/")
        self.assertNotIsInstance(ctx.exception, fetch.RobotsUnavailable)
        self.assertIn("kořenový certifikát", str(ctx.exception))

    def test_other_network_errors_still_retried(self):
        sleeps = []
        fetcher = fetch.Fetcher(sleep=sleeps.append, respect_robots=False)
        with mock.patch.object(fetcher, "_request", side_effect=urllib.error.URLError(TimeoutError())) as request:
            with self.assertRaises(fetch.FetchError):
                fetcher.get("https://www.jobs.cz/prace/")
        self.assertEqual(request.call_count, 2)


class MailTlsTests(unittest.TestCase):
    def setUp(self):
        tm.FakeSMTP.instances, tm.FakeSMTP.fail = [], []

    def test_both_security_modes_use_our_context(self):
        sentinel = ssl.create_default_context()
        seen = []

        class Recording(tm.FakeSMTP):
            def __init__(self, host, port, timeout=None, context=None):
                seen.append(("connect", context))
                super().__init__(host, port, timeout, context)

            def starttls(self, context=None):
                seen.append(("starttls", context))
                super().starttls(context)

        with mock.patch.object(tls, "create_context", return_value=sentinel):
            mailer.verify_login(tm.cfg(), "x", progress=lambda n: None, smtp_ssl=Recording, smtp_plain=Recording)
            mailer.verify_login(tm.cfg(port=587, security="starttls"), "x", progress=lambda n: None,
                                smtp_ssl=Recording, smtp_plain=Recording)
        self.assertEqual(seen[0], ("connect", sentinel))  # SMTP_SSL(context=…)
        self.assertIn(("starttls", sentinel), seen)       # starttls(context=…)

    def test_cert_error_advice(self):
        error = mailer.explain(CERT_ERROR, tm.cfg(smtp_server="smtp.seznam.cz"))
        self.assertFalse(error.transient)
        self.assertIn("kořenový certifikát", str(error))
        self.assertIn("Mail Shield", str(error))


class MissingNotepadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        patcher = mock.patch.object(paths, "app_dir", return_value=self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(close_logging)

    def first_run(self, opened):
        out = io.StringIO()
        with mock.patch.object(console, "can_ask", return_value=True), \
                mock.patch.object(console, "open_for_edit", return_value=opened), \
                mock.patch.object(console, "owns_console", return_value=False), \
                mock.patch("sys.stderr", new_callable=io.StringIO), redirect_stdout(out):
            self.assertEqual(cli.main([]), 2)
        return out.getvalue()

    def test_missing_notepad_gives_full_path(self):
        out = self.first_run(opened=False)
        self.assertIn("Poznámkový blok se nepodařilo spustit", out)
        self.assertIn(str((self.app / "config.toml").resolve()), out)
        self.assertNotIn("jsem otevřel", out)

    def test_opened(self):
        out = self.first_run(opened=True)
        self.assertIn("jsem otevřel v Poznámkovém bloku", out)
        self.assertNotIn("nepodařilo spustit", out)

    def test_not_attempted_says_nothing_extra(self):
        out = self.first_run(opened=None)
        self.assertNotIn("nepodařilo spustit", out)
        self.assertNotIn("jsem otevřel", out)

    def test_open_for_edit_states(self):
        target = self.app / "config.toml"
        with mock.patch.object(console.sys, "platform", "linux"):
            self.assertIsNone(console.open_for_edit(target))
        with mock.patch.object(console.sys, "platform", "win32"), \
                mock.patch.dict(console.os.environ, {console.NO_EDITOR_ENV: "1"}):
            self.assertIsNone(console.open_for_edit(target))
        with mock.patch.object(console.sys, "platform", "win32"), \
                mock.patch.object(console.subprocess, "Popen", side_effect=FileNotFoundError("notepad.exe")):
            console.os.environ.pop(console.NO_EDITOR_ENV, None)
            self.assertIs(console.open_for_edit(target), False)

    def test_manual_text(self):
        text = firstrun.manual_open_text(Path(r"C:\Users\x\JobAgent\config.toml"))
        self.assertIn(r"C:\Users\x\JobAgent\config.toml", text)
        self.assertIn("UTF-8", text)


if __name__ == "__main__":
    unittest.main()
