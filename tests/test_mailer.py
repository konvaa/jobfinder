"""Fáze 4: sestavení e-mailu, odeslání (bez sítě), překlad chyb a ostrý běh.

Skutečný server se v testech nepoužívá: třídy SMTP jsou podvržené. Ověřuje se
hlavně to, na čem záleží při selhání – nic se neoznačí, nic se neztratí,
přechodná chyba se zkusí jednou znovu, chyba přihlášení ne.
"""

import argparse
import email
import os
import smtplib
import socket
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta
from email import policy
from pathlib import Path
from unittest import mock

from jobagent import cli, emailer, mailer, outputs, paths
from jobagent.config import EmailCfg, load_config
from jobagent.models import Job
from jobagent.pipeline import RunReport, SearchStat
from jobagent.storage import Storage

from tests.helpers import SAMPLES
from tests.test_pipeline import LISTING_URL, make_web

ROOT = SAMPLES.parent
TEMPLATES = ROOT / "templates"
NOW = datetime(2026, 11, 4, 10, 0)


def cfg(**changes) -> EmailCfg:
    values = dict(smtp_server="smtp.example.cz", port=465, security="ssl", user="agent@example.com",
                  sender="agent@example.com", recipients=["ja@example.com", "kamarad@example.com"])
    values.update(changes)
    return EmailCfg(**values)


def job(n: int, score: int = 10) -> Job:
    j = Job(portal="jobs.cz", native_id=str(n), url=f"https://www.jobs.cz/rpd/{n}/", title=f"Pozice {n}",
            company="Firma s.r.o.", location="Brno")
    j.score = score
    return j


def report(main=0, others=0, stats=None) -> RunReport:
    r = RunReport(started=NOW, dry_run=False)
    r.new_jobs = [job(i, 30) for i in range(main)] + [job(1000 + i, 1) for i in range(others)]
    r.stats = stats or [SearchStat(name="IT Brno", url=LISTING_URL, portal="jobs.cz", found=main + others)]
    return r


class FakeSMTP:
    """Záznam volání místo síťového spojení. `fail` = výjimky pro jednotlivé pokusy."""

    instances: list["FakeSMTP"] = []
    fail: list[BaseException | None] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls = host, port, []
        FakeSMTP.instances.append(self)
        error = FakeSMTP.fail.pop(0) if FakeSMTP.fail else None
        if error is not None:
            raise error

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append("send")
        return {}

    def quit(self):
        self.calls.append("quit")


class MessageTests(unittest.TestCase):
    def test_text_and_html_alternative(self):
        msg = mailer.build_message(cfg(), "Předmět ✓", "text", "<p>html</p>", now=NOW)
        parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        self.assertEqual(parsed["Subject"], "Předmět ✓")
        self.assertIn("Job Agent", parsed["From"])
        self.assertIn("agent@example.com", parsed["From"])
        self.assertEqual(parsed["To"], "ja@example.com, kamarad@example.com")
        self.assertEqual(parsed.get_body(("plain",)).get_content().strip(), "text")
        self.assertEqual(parsed.get_body(("html",)).get_content().strip(), "<p>html</p>")

    def test_attachment_makes_mixed(self):
        msg = mailer.build_message(cfg(), "S", "t", "<p>h</p>", attachment_html="<p>celý</p>",
                                   attachment_name=emailer.ATTACHMENT_NAME, now=NOW)
        parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
        self.assertEqual(parsed.get_content_type(), "multipart/mixed")
        attachments = list(parsed.iter_attachments())
        self.assertEqual([a.get_filename() for a in attachments], [emailer.ATTACHMENT_NAME])
        self.assertEqual(attachments[0].get_content().strip(), "<p>celý</p>")

    def test_sender_falls_back_to_user(self):
        msg = mailer.build_message(cfg(sender=""), "S", "t", "h", now=NOW)
        self.assertIn("agent@example.com", msg["From"])


class SubjectTests(unittest.TestCase):
    def test_czech_plurals(self):
        self.assertEqual(emailer.subject(report(1), "Job Agent", 20), "Job Agent: 1 nová (1 v hlavní tabulce)")
        self.assertEqual(emailer.subject(report(2, 1), "Job Agent", 20), "Job Agent: 3 nové (2 v hlavní tabulce)")
        self.assertEqual(emailer.subject(report(0, 5), "Job Agent", 20), "Job Agent: 5 nových (0 v hlavní tabulce)")

    def test_nothing_new_still_says_it_ran(self):
        self.assertEqual(emailer.subject(report(), "Práce", 20), "Práce: dnes nic nového")

    def test_failed_portal_gets_warning_sign(self):
        stats = [SearchStat(name="A", url="u", portal="jobs.cz", found=2),
                 SearchStat(name="B", url="u", portal="prace.cz", error="HTTP 500"),
                 SearchStat(name="C", url="u", portal="jobscontact.cz", skipped="robots.txt")]
        text = emailer.subject(report(2, stats=stats), "Job Agent", 20)
        self.assertTrue(text.startswith("⚠ Job Agent: 2 nové"))
        self.assertTrue(text.endswith("– chyba: jobscontact.cz, prace.cz"))


class BodyTests(unittest.TestCase):
    def test_small_summary_has_no_attachment(self):
        html_body, text_body, attachment = emailer.build_bodies(report(2, 3), TEMPLATES, NOW, 20)
        self.assertIsNone(attachment)
        self.assertIn("Pozice 0", html_body)
        self.assertIn("Pozice 0", text_body)
        self.assertIn("https://www.jobs.cz/rpd/1002/", text_body)
        self.assertNotIn("##", text_body)  # návod ze šablony do e-mailu nepatří
        self.assertNotIn("\n\n\n", text_body)
        self.assert_plain_others(html_body, 3)

    def assert_plain_others(self, html_body, count):
        """Webmail Seznamu maže <details> i s obsahem: v těle nesmí být, nadpis ano."""
        self.assertNotIn("<details", html_body)
        self.assertNotIn("<summary", html_body)
        self.assertIn(f"Ostatní nabídky ({count})", html_body)
        self.assertRegex(html_body, rf"<h3[^>]*>Ostatní nabídky \({count}\)")

    def test_large_summary_goes_to_attachment(self):
        r = report(1, 400)
        with mock.patch.object(emailer, "BODY_LIMIT_BYTES", 20_000):
            html_body, text_body, attachment = emailer.build_bodies(r, TEMPLATES, NOW, 20)
        self.assertIsNotNone(attachment)
        self.assertIn("Pozice 1399", attachment)  # příloha má všechno
        self.assertNotIn("Pozice 1399", html_body)
        self.assertIn("Pozice 1019", html_body)  # prvních BODY_OTHER_ROWS ostatních
        self.assertNotIn("Pozice 1020", html_body)
        self.assertIn("a dalších 380", text_body)
        self.assertIn(emailer.ATTACHMENT_NAME, html_body)
        self.assert_plain_others(html_body, 400)
        # Příloha se otevírá v prohlížeči: tam má dlouhý seznam zůstat sbalitelný.
        self.assertIn("<details", attachment)
        self.assertIn("Ostatní nabídky (400)", attachment)

    def test_render_default_is_safe_for_mail(self):
        self.assertNotIn("<details", emailer.render(report(1, 5), TEMPLATES, NOW, 20))


class SendTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances, FakeSMTP.fail = [], []
        self.msg = mailer.build_message(cfg(), "S", "t", "h", now=NOW)
        self.sleeps = []

    def send(self, config=None, **kwargs):
        notes = []
        mailer.send(self.msg, config or cfg(), "heslo", smtp_ssl=FakeSMTP, smtp_plain=FakeSMTP,
                    sleep=self.sleeps.append, progress=notes.append, **kwargs)
        return notes

    def test_ssl_success(self):
        notes = self.send()
        self.assertEqual(FakeSMTP.instances[0].calls, [("login", "agent@example.com"), "send", "quit"])
        self.assertTrue(any("Odesláno" in n for n in notes))
        self.assertFalse(any("heslo" in n for n in notes))

    def test_starttls_upgrades_before_login(self):
        self.send(cfg(port=587, security="starttls"))
        calls = FakeSMTP.instances[0].calls
        self.assertLess(calls.index("starttls"), calls.index(("login", "agent@example.com")))

    def test_auth_error_is_not_retried(self):
        FakeSMTP.fail = [smtplib.SMTPAuthenticationError(535, b"bad credentials")]
        with self.assertRaises(mailer.MailError) as ctx:
            self.send()
        self.assertFalse(ctx.exception.transient)
        self.assertIn("heslo pro aplikace", str(ctx.exception))
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertEqual(self.sleeps, [])

    def test_transient_error_retried_once(self):
        FakeSMTP.fail = [TimeoutError(), None]
        notes = self.send()
        self.assertEqual(len(FakeSMTP.instances), 2)
        self.assertEqual(self.sleeps, [mailer.RETRY_DELAY])
        self.assertTrue(any("Zkusím to znovu" in n for n in notes))

    def test_transient_error_gives_up_after_retry(self):
        FakeSMTP.fail = [TimeoutError(), TimeoutError()]
        with self.assertRaises(mailer.MailError) as ctx:
            self.send()
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(len(FakeSMTP.instances), 2)

    def test_no_retry_when_disabled(self):
        FakeSMTP.fail = [TimeoutError()]
        with self.assertRaises(mailer.MailError):
            self.send(retries=0)
        self.assertEqual(self.sleeps, [])

    def test_missing_password_fails_before_connecting(self):
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.send(self.msg, cfg(), "", smtp_ssl=FakeSMTP, smtp_plain=FakeSMTP)
        self.assertIn("secrets.toml", str(ctx.exception))
        self.assertEqual(FakeSMTP.instances, [])

    def test_missing_recipients(self):
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.check_settings(cfg(recipients=[]), "heslo")
        self.assertIn("prijemci", str(ctx.exception))


class ExplainTests(unittest.TestCase):
    def explain(self, exc, **changes):
        return mailer.explain(exc, cfg(**changes))

    def test_wrong_version_means_port_mismatch(self):
        error = self.explain(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), port=587)
        self.assertFalse(error.transient)
        self.assertIn('zabezpeceni = "starttls"', str(error))

    def test_certificate_error_mentions_antivirus(self):
        error = self.explain(ssl.SSLCertVerificationError("certificate verify failed"))
        self.assertIn("antivir", str(error))

    def test_unknown_host(self):
        error = self.explain(socket.gaierror(11001, "getaddrinfo failed"))
        self.assertTrue(error.transient)
        self.assertIn("smtp.example.cz", str(error))

    def test_4xx_is_transient_5xx_is_not(self):
        self.assertTrue(self.explain(smtplib.SMTPDataError(451, b"try later")).transient)
        permanent = self.explain(smtplib.SMTPDataError(554, b"spam"))
        self.assertFalse(permanent.transient)
        self.assertIn("554", str(permanent))

    def test_recipients_refused(self):
        error = self.explain(smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no such user")}))
        self.assertIn("x@example.com", str(error))
        self.assertFalse(error.transient)

    def test_technical_detail_kept(self):
        self.assertIn("TimeoutError", self.explain(TimeoutError("timed out")).technical)


class OutputsTests(unittest.TestCase):
    """Soubory ve vystup/: dvojice .html + .eml a úklid jen vlastních souborů."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_saves_html_and_full_eml(self):
        msg = mailer.build_message(cfg(), "S", "t", "<p>h</p>", attachment_html="<p>celý</p>",
                                   attachment_name=emailer.ATTACHMENT_NAME, now=NOW)
        html_path, eml_path = outputs.save_summary(msg, "<p>celý</p>", self.out, NOW)
        self.assertEqual((html_path.name, eml_path.name), ("email_2026-11-04_100000.html", "email_2026-11-04_100000.eml"))
        self.assertEqual(html_path.read_text(encoding="utf-8"), "<p>celý</p>")
        parsed = email.message_from_bytes(eml_path.read_bytes(), policy=policy.default)
        self.assertEqual(parsed["Subject"], "S")
        self.assertEqual(parsed.get_body(("plain",)).get_content().strip(), "t")  # textová verze
        self.assertEqual(parsed.get_body(("html",)).get_content().strip(), "<p>h</p>")  # tělo
        self.assertEqual([a.get_filename() for a in parsed.iter_attachments()], [emailer.ATTACHMENT_NAME])

    def test_unsent_suffix(self):
        msg = mailer.build_message(cfg(), "S", "t", "h", now=NOW)
        html_path, eml_path = outputs.save_summary(msg, "h", self.out, NOW, unsent=True)
        self.assertEqual(eml_path.name, "email_2026-11-04_100000_NEODESLANO.eml")
        self.assertEqual(html_path.stem, eml_path.stem)

    def age(self, name: str, days: float) -> Path:
        path = self.out / name
        path.write_text("x", encoding="utf-8")
        stamp = (NOW - timedelta(days=days)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def test_cleanup_removes_only_old_own_files(self):
        old_html = self.age("email_2026-09-01_080000.html", 40)
        old_eml = self.age("email_2026-09-01_080000_NEODESLANO.eml", 40)
        fresh = self.age("email_2026-11-01_080000.eml", 3)
        foreign = self.age("moje_poznamky.txt", 400)  # cizí soubor uživatele
        foreign_html = self.age("vlastni.html", 400)
        (self.out / "archiv").mkdir()
        nested = self.out / "archiv" / "email_2025-01-01_080000.html"
        nested.write_text("x", encoding="utf-8")
        os.utime(nested, (1, 1))

        self.assertEqual(outputs.cleanup(self.out, 30, NOW), 2)
        self.assertFalse(old_html.exists())
        self.assertFalse(old_eml.exists())
        for kept in (fresh, foreign, foreign_html, nested):
            self.assertTrue(kept.exists(), kept.name)

    def test_cleanup_zero_days_keeps_everything(self):
        old = self.age("email_2020-01-01_080000.html", 2000)
        self.assertEqual(outputs.cleanup(self.out, 0, NOW), 0)
        self.assertTrue(old.exists())

    def test_cleanup_missing_dir(self):
        self.assertEqual(outputs.cleanup(self.out / "neni", 30, NOW), 0)

    def test_latest_by_mtime(self):
        self.age("email_2026-11-05_080000.eml", 5)  # jméno novější, soubor starší
        newest = self.age("email_2026-11-01_080000.eml", 1)
        self.age("email_2026-11-09_080000.html", 0)
        self.assertEqual(outputs.latest(self.out, ".eml"), newest)
        self.assertIsNone(outputs.latest(self.out / "neni", ".eml"))


class AsTestTests(unittest.TestCase):
    def test_headers_replaced_content_kept(self):
        original = mailer.build_message(cfg(recipients=[]), "Job Agent: 5 nových", "t", "<p>h</p>",
                                        attachment_html="<p>celý</p>", attachment_name="a.html", now=NOW)
        parsed = email.message_from_bytes(original.as_bytes(), policy=policy.default)
        msg = mailer.as_test(parsed, cfg(recipients=["novy@example.com"]), datetime(2026, 11, 9, 18))
        self.assertEqual(msg["Subject"], "TEST – Job Agent: 5 nových")
        self.assertEqual(msg["To"], "novy@example.com")
        self.assertNotEqual(msg["Message-ID"], original["Message-ID"])  # jinak by ho Gmail zahodil
        self.assertEqual(len(msg.get_all("Message-ID")), 1)
        self.assertEqual(msg.get_body(("html",)).get_content().strip(), "<p>h</p>")
        self.assertEqual(len(list(msg.iter_attachments())), 1)
        # Podruhé poslaný test nemá „TEST – TEST – “.
        again = mailer.as_test(msg, cfg(), NOW)
        self.assertEqual(again["Subject"], "TEST – Job Agent: 5 nových")


class LiveRunTests(unittest.TestCase):
    """`jobagent run` bez --dry-run: označit až po odeslání, při selhání kód 3."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = self.cfg.searches[:1]
        self.cfg.searches[0].url = LISTING_URL
        self.cfg.email = cfg()
        self.cfg.secrets.email_password = "heslo"
        self.patches = [
            mock.patch.object(paths, "app_dir", return_value=self.app),
            mock.patch.object(paths, "templates_dir", return_value=TEMPLATES),
            mock.patch.object(cli, "load_config", return_value=self.cfg),
            mock.patch.object(cli, "_make_fetcher", side_effect=lambda c: make_web()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, send_side_effect=None):
        with mock.patch.object(mailer, "send", side_effect=send_side_effect) as send, \
                self.assertLogs("jobagent", level="INFO") as logs:
            code = cli.cmd_run(argparse.Namespace(dry_run=False, no_open=True))
        return code, send, logs.output

    def sent_count(self):
        storage = Storage(self.app / "jobagent.db")
        try:
            jobs = storage.conn.execute("SELECT COUNT(*) FROM jobs WHERE sent_at IS NOT NULL").fetchone()[0]
            delivered = storage.conn.execute("SELECT COUNT(*) FROM runs WHERE delivered=1").fetchone()[0]
            return jobs, delivered
        finally:
            storage.close()

    def test_success_marks_sent(self):
        code, send, logs = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 1)
        jobs, delivered = self.sent_count()
        self.assertGreater(jobs, 0)
        self.assertEqual(delivered, 1)
        out = self.app / "vystup"
        emls = list(out.glob("email_*.eml"))
        self.assertEqual(len(emls), 1)
        self.assertNotIn("NEODESLANO", emls[0].name)
        self.assertTrue(emls[0].with_suffix(".html").exists())
        saved = email.message_from_bytes(emls[0].read_bytes(), policy=policy.default)
        self.assertEqual(saved["Subject"], send.call_args.args[0]["Subject"])  # uložené = odeslané
        self.assertFalse(any("heslo" in line for line in logs))

    def test_dry_run_saves_eml_without_sending(self):
        with mock.patch.object(mailer, "send") as send, self.assertLogs("jobagent", level="INFO"):
            code = cli.cmd_run(argparse.Namespace(dry_run=True, no_open=True))
        self.assertEqual(code, 0)
        send.assert_not_called()
        self.assertEqual(self.sent_count(), (0, 0))
        emls = list((self.app / "vystup").glob("email_*.eml"))
        self.assertEqual(len(emls), 1)
        saved = email.message_from_bytes(emls[0].read_bytes(), policy=policy.default)
        self.assertIn("NÁHLED (dry-run)", saved.get_body(("html",)).get_content())
        self.assertNotIn("<details", saved.get_body(("html",)).get_content())

    def test_run_cleans_old_files(self):
        out = self.app / "vystup"
        out.mkdir()
        old = out / "email_2026-01-01_080000.eml"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (1, 1))
        self.cfg.general.keep_output_days = 30
        self.run_cli()
        self.assertFalse(old.exists())

    def test_failure_marks_nothing_and_returns_3(self):
        code, _, logs = self.run_cli(mailer.MailError("Server odmítl přihlášení.", transient=False))
        self.assertEqual(code, 3)
        self.assertEqual(self.sent_count(), (0, 0))
        self.assertTrue(list((self.app / "vystup").glob("*_NEODESLANO.html")))
        self.assertTrue(list((self.app / "vystup").glob("*_NEODESLANO.eml")))
        # Neodeslaný se neuloží ještě jednou jako „odeslaný“.
        self.assertFalse([p for p in (self.app / "vystup").glob("email_*.eml") if "NEODESLANO" not in p.name])
        self.assertTrue(any("test-email" in line for line in logs))

    def test_failed_run_resends_same_jobs(self):
        self.run_cli(mailer.MailError("x", transient=True))
        _, send, _ = self.run_cli()
        msg = send.call_args.args[0]
        self.assertNotEqual(msg["Subject"].split(":")[1].strip(), "dnes nic nového")

    def test_missing_password_stops_before_collecting(self):
        self.cfg.secrets.email_password = ""
        with self.assertLogs("jobagent", level="ERROR"):
            code = cli.cmd_run(argparse.Namespace(dry_run=False, no_open=True))
        self.assertEqual(code, 2)
        self.assertFalse((self.app / "jobagent.db").exists())


class TestEmailCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.email = cfg()
        self.cfg.secrets.email_password = "tajne-heslo"
        for p in (mock.patch.object(paths, "app_dir", return_value=self.app),
                  mock.patch.object(cli, "load_config", return_value=self.cfg)):
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, nahled=False, side_effect=None):
        with mock.patch.object(mailer, "send", side_effect=side_effect) as send, \
                self.assertLogs("jobagent", level="INFO") as logs:
            code = cli.cmd_test_email(argparse.Namespace(nahled=nahled))
        return code, send, logs.output

    def test_short_message_without_retry(self):
        code, send, logs = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertEqual(send.call_args.kwargs["retries"], 0)
        self.assertIn("zkušební", send.call_args.args[0]["Subject"])
        self.assertFalse(any("tajne-heslo" in line for line in logs))
        self.assertTrue(any("heslo: vyplněné" in line for line in logs))

    def test_failure_returns_3(self):
        code, _, _ = self.run_cmd(side_effect=mailer.MailError("nejde", transient=False, technical="T"))
        self.assertEqual(code, 3)

    def test_preview_resends_latest_eml(self):
        out = self.app / "vystup"
        out.mkdir()
        sent = mailer.build_message(cfg(), "Job Agent: 3 nové", "text", "<p>tělo</p>",
                                    attachment_html="<p>celý</p>", attachment_name=emailer.ATTACHMENT_NAME, now=NOW)
        outputs.save_summary(sent, "<p>celý</p>", out, NOW)
        older = out / "email_2026-11-01_080000.eml"
        older.write_bytes(mailer.build_message(cfg(), "Starý", "t", "h", now=NOW).as_bytes())
        os.utime(older, (1, 1))
        code, send, logs = self.run_cmd(nahled=True)
        self.assertEqual(code, 0)
        msg = send.call_args.args[0]
        self.assertEqual(msg["Subject"], "TEST – Job Agent: 3 nové")
        self.assertEqual(msg["To"], "ja@example.com, kamarad@example.com")
        self.assertNotEqual(msg["Message-ID"], sent["Message-ID"])
        self.assertEqual(msg.get_body(("html",)).get_content().strip(), "<p>tělo</p>")
        self.assertEqual([a.get_filename() for a in msg.iter_attachments()], [emailer.ATTACHMENT_NAME])
        self.assertEqual(send.call_args.kwargs["retries"], 0)
        self.assertFalse(any("Gmail" in line for line in logs))  # malé tělo = bez varování

    def test_preview_falls_back_to_html(self):
        out = self.app / "vystup"
        out.mkdir()
        old, new = out / "email_2026-11-01_080000.html", out / "email_2026-11-04_080000.html"
        old.write_text("<p>starý</p>", encoding="utf-8")
        new.write_text("<p>nový</p>" + "x" * 120_000, encoding="utf-8")
        os.utime(old, (1, 1))
        code, send, logs = self.run_cmd(nahled=True)
        self.assertEqual(code, 0)
        msg = send.call_args.args[0]
        self.assertTrue(msg["Subject"].startswith("TEST – "))
        self.assertIn("2026-11-04_080000", msg["Subject"])
        self.assertIn("<p>nový</p>", msg.get_body(("html",)).get_content())
        self.assertTrue(any("Gmail" in line for line in logs))  # varování o velikosti

    def test_preview_without_files(self):
        with self.assertLogs("jobagent", level="ERROR"):
            code = cli.cmd_test_email(argparse.Namespace(nahled=True))
        self.assertEqual(code, 1)


class EmailConfigTests(unittest.TestCase):
    def load(self, email_section: str):
        base = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        start = base.index("[email]")
        end = base.index("\n[", start + 1)
        text = base[:start] + "[email]\n" + email_section + base[end:]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return load_config(Path(tmp.name))

    def test_example_is_ssl_465(self):
        e = load_config(ROOT / "config.example.toml").email
        self.assertEqual((e.smtp_server, e.port, e.security, e.subject), ("smtp.seznam.cz", 465, "ssl", "Job Agent"))

    def test_starttls(self):
        e = self.load('smtp_server = "smtp.gmail.com"\nport = 587\nzabezpeceni = "starttls"\n'
                      'uzivatel = "a@example.com"\nprijemci = ["a@example.com"]\n').email
        self.assertEqual(e.security, "starttls")

    def test_legacy_ssl_key_warns(self):
        c = self.load('port = 587\nssl = false\nuzivatel = "a@b.cz"\nprijemci = ["a@b.cz"]\n')
        self.assertEqual(c.email.security, "starttls")
        self.assertTrue(any("starý zápis" in w for w in c.warnings))

    def test_invalid_security_is_error(self):
        from jobagent.config import ConfigError
        with self.assertRaises(ConfigError) as ctx:
            self.load('zabezpeceni = "tls"\nuzivatel = "a@b.cz"\nprijemci = ["a@b.cz"]\n')
        self.assertIn("zabezpeceni", str(ctx.exception))

    def test_port_mismatch_warns(self):
        c = self.load('port = 587\nzabezpeceni = "ssl"\nuzivatel = "a@b.cz"\nprijemci = ["a@b.cz"]\n')
        self.assertTrue(any("587" in w for w in c.warnings))

    def test_keep_output_days(self):
        self.assertEqual(load_config(ROOT / "config.example.toml").general.keep_output_days, 30)

    def test_negative_keep_days_warns(self):
        text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        text = text.replace("uchovat_vystup_dni = 30", "uchovat_vystup_dni = -5")
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        self.addCleanup(lambda: (tmp.unlink(), tmp.parent.rmdir()))
        tmp.write_text(text, encoding="utf-8")
        c = load_config(tmp)
        self.assertEqual(c.general.keep_output_days, 30)
        self.assertTrue(any("uchovat_vystup_dni" in w for w in c.warnings))

    def test_sender_differs_from_user_warns(self):
        c = self.load('uzivatel = "a@b.cz"\nodesilatel = "jiny@example.com"\nprijemci = ["a@b.cz"]\n')
        self.assertTrue(any("jiny@example.com" in w for w in c.warnings))


if __name__ == "__main__":
    unittest.main()
