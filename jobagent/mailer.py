"""Odeslání souhrnu přes SMTP a srozumitelné české vysvětlení chyb.

Jen standardní knihovna: `email.message.EmailMessage` (MIME) a `smtplib`.

Zpráva je multipart/alternative: čistý text první, HTML druhé. Klient
zobrazí poslední verzi, kterou umí, tedy HTML. Když je souhrn velký, přibude
celý jako příloha (multipart/mixed), viz emailer.build_bodies.

Proč vlastní překlad chyb: smtplib hlásí věci jako „(535, b'5.7.8 Error:
authentication failed')“ nebo „[SSL: WRONG_VERSION_NUMBER]“. Pro kamaráda bez
programování je potřeba říct, co udělat. MailError nese i příznak
`transient`: opakovat má smysl jen u výpadku sítě nebo dočasné chyby serveru
(kód 4xx), ne u špatného hesla.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import time
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from jobagent import __version__, tls
from jobagent.config import EmailCfg

log = logging.getLogger(__name__)

TIMEOUT = 30  # s; Plánovač úloh nesmí viset na mrtvém spojení
RETRY_DELAY = 60  # s mezi prvním a jediným opakovaným pokusem
SENDER_NAME = "Job Agent"


class MailError(Exception):
    """Odeslání selhalo. str(e) = vysvětlení pro člověka, `technical` = původní chyba."""

    def __init__(self, message: str, *, transient: bool, technical: str = ""):
        super().__init__(message)
        self.transient = transient
        self.technical = technical


def build_message(cfg: EmailCfg, subject: str, text: str, html: str,
                  attachment_html: str | None = None, attachment_name: str = "souhrn.html",
                  now: datetime | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_NAME, cfg.sender or cfg.user))
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate((now or datetime.now()).timestamp(), localtime=True)
    domain = (cfg.sender or cfg.user).rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid("jobagent", domain=domain)
    msg["X-Mailer"] = f"JobAgent/{__version__}"
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    if attachment_html is not None:
        # Jako text s charset=utf-8, ne jako bajty: bez charsetu by poštovní
        # program diakritiku v příloze zobrazil rozsypaně.
        msg.add_attachment(attachment_html, subtype="html", charset="utf-8", filename=attachment_name)
    return msg


def check_settings(cfg: EmailCfg, password: str) -> None:
    """Chyby, kvůli kterým nemá smysl se vůbec připojovat."""
    missing = [name for name, value in (("smtp_server", cfg.smtp_server), ("uzivatel", cfg.user)) if not value]
    if missing:
        raise MailError(f"V config.toml v sekci [email] chybí: {', '.join(missing)}.", transient=False)
    if not cfg.recipients:
        raise MailError("V config.toml v sekci [email] chybí prijemci = [\"adresa@…\"].", transient=False)
    if not password:
        raise MailError(
            "Chybí heslo k e-mailu. Ve Windows ho ulož příkazem nastav-heslo (uloží se do Správce "
            "pověření), jinde do secrets.toml ([email] heslo = \"…\"). U Seznamu se zapnutým "
            "dvoufázovým ověřením a u Gmailu je to heslo pro aplikace, ne heslo k účtu.",
            transient=False,
        )


def send(msg: EmailMessage, cfg: EmailCfg, password: str, *, retries: int = 1,
         retry_delay: float = RETRY_DELAY, progress: Callable[[str], None] | None = None,
         smtp_ssl: type = smtplib.SMTP_SSL, smtp_plain: type = smtplib.SMTP,
         sleep: Callable[[float], None] = time.sleep) -> None:
    """Odešle zprávu. Při přechodné chybě zkusí nejvýš `retries`× znovu po `retry_delay` s.

    Vyhodí MailError s českým vysvětlením. Třídy SMTP jdou podvrhnout (testy bez sítě).
    """
    check_settings(cfg, password)
    note = progress or (lambda message: log.info("%s", message))
    attempt = 0
    while True:
        attempt += 1
        try:
            _send_once(msg, cfg, password, note, smtp_ssl, smtp_plain)
            return
        except MailError:
            raise
        except Exception as exc:  # noqa: BLE001 – každou chybu přeložíme, nic nesmí uniknout bez vysvětlení
            error = explain(exc, cfg)
            if error.transient and attempt <= retries:
                note(f"Odeslání selhalo ({error}). Zkusím to znovu za {retry_delay:.0f} s.")
                sleep(retry_delay)
                continue
            raise error from exc


def _login(cfg: EmailCfg, password: str, note: Callable[[str], None], smtp_ssl: type, smtp_plain: type):
    """Připojit a přihlásit. Vrací otevřené spojení; volající ho musí zavřít (_quit)."""
    context = tls.create_context()  # ověření certifikátu přes Windows, viz tls.py
    mode = "SSL" if cfg.security == "ssl" else "STARTTLS"
    note(f"Připojuji se k {cfg.smtp_server}:{cfg.port} ({mode})…")
    if cfg.security == "ssl":
        server = smtp_ssl(cfg.smtp_server, cfg.port, timeout=TIMEOUT, context=context)
    else:
        server = smtp_plain(cfg.smtp_server, cfg.port, timeout=TIMEOUT)
    try:
        if cfg.security == "starttls":
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(cfg.user, password)
    except BaseException:
        _quit(server)
        raise
    note(f"Přihlášení jako {cfg.user}: OK.")
    return server


def _quit(server) -> None:
    try:
        server.quit()
    except Exception:  # noqa: BLE001 – zpráva už odešla nebo spojení padlo; quit je jen zdvořilost
        pass


def _send_once(msg: EmailMessage, cfg: EmailCfg, password: str, note: Callable[[str], None],
               smtp_ssl: type, smtp_plain: type) -> None:
    server = _login(cfg, password, note, smtp_ssl, smtp_plain)
    try:
        refused = server.send_message(msg)
        if refused:
            # Část příjemců server odmítl, ostatním zpráva odešla. Neopakovat (byla by duplicita).
            note("Některým příjemcům server zprávu nepřijal: " + ", ".join(refused))
        note(f"Odesláno na: {msg['To']}.")
    finally:
        _quit(server)


def verify_login(cfg: EmailCfg, password: str, *, progress: Callable[[str], None] | None = None,
                 smtp_ssl: type = smtplib.SMTP_SSL, smtp_plain: type = smtplib.SMTP) -> None:
    """Jen se přihlásit a odhlásit, nic neodeslat (pro nastav-heslo). Vyhodí MailError."""
    note = progress or (lambda text: log.info("%s", text))
    try:
        _quit(_login(cfg, password, note, smtp_ssl, smtp_plain))
    except MailError:
        raise
    except Exception as exc:  # noqa: BLE001 – stejný překlad chyb jako u odesílání
        raise explain(exc, cfg) from exc


def explain(exc: BaseException, cfg: EmailCfg) -> MailError:
    """Překlad výjimky smtplib/ssl/socket na srozumitelnou radu."""
    tech = f"{type(exc).__name__}: {exc}"
    host = f"{cfg.smtp_server}:{cfg.port}"
    other = "starttls" if cfg.security == "ssl" else "ssl"
    other_port = 587 if cfg.security == "ssl" else 465

    def err(message: str, transient: bool) -> MailError:
        return MailError(message, transient=transient, technical=tech)

    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return err(
            f"Server odmítl přihlášení (uživatel {cfg.user}). Zkontroluj: uzivatel je CELÁ e-mailová adresa; "
            "heslo je správné (znovu ho ulož příkazem nastav-heslo, nebo oprav v secrets.toml). Máš-li "
            "dvoufázové ověření (Seznam), nebo jde-li o Gmail, je to heslo pro aplikace, ne heslo k účtu.", False)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return err(
            f"Server odmítl odesílatele {exc.sender!r} (kód {exc.smtp_code}). Nastav odesilatel stejně jako "
            "uzivatel: poskytovatel obvykle nedovolí odesílat z jiné adresy.", _is_4xx(exc.smtp_code))
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return err("Server odmítl všechny příjemce: " + ", ".join(exc.recipients)
                   + ". Zkontroluj adresy v [email] prijemci.", False)
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return err(f'Server {host} nepodporuje STARTTLS. Zkus zabezpeceni = "ssl" a port 465.', False)
    if isinstance(exc, smtplib.SMTPResponseException):
        code = exc.smtp_code
        if _is_4xx(code):
            return err(f"Server zprávu dočasně odmítl (kód {code}). Obvykle stačí zkusit později.", True)
        return err(f"Server zprávu odmítl (kód {code}: {_decode(exc.smtp_error)}). Může jít o spamový filtr "
                   "nebo limit velikosti či počtu zpráv.", False)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return err(f"Server {host} ukončil spojení. Když se to opakuje, zkontroluj port a zabezpečení.", True)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return err(tls.cert_advice(cfg.smtp_server, mail=True), False)
    if isinstance(exc, ssl.SSLError):
        if "WRONG_VERSION_NUMBER" in str(exc) or "wrong version number" in str(exc).lower():
            return err(
                f'Port {cfg.port} a zabezpeceni = "{cfg.security}" k sobě nesedí. Seznam i Gmail: port 465 = '
                f'"ssl", port 587 = "starttls". Zkus zabezpeceni = "{other}" (nebo port {other_port}).', False)
        return err(f"Chyba šifrovaného spojení se serverem {host} ({exc}).", True)
    if isinstance(exc, socket.gaierror):
        return err(f"Server {cfg.smtp_server} se nepodařilo najít. Překlep v smtp_server, nebo počítač zrovna "
                   "není připojený k internetu.", True)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return err(
            f"Server {host} neodpovídá. Port nejspíš blokuje firewall, antivir nebo síť (typicky firemní). "
            f'Zkus port {other_port} se zabezpeceni = "{other}".', True)
    if isinstance(exc, ConnectionRefusedError):
        return err(f"Spojení se serverem {host} bylo odmítnuto. Zkontroluj smtp_server a port (Seznam: 465).", True)
    if isinstance(exc, OSError):
        return err(f"Spojení se serverem {host} selhalo ({exc}). Zkontroluj připojení k internetu.", True)
    return err(f"Neočekávaná chyba při odesílání: {tech}", False)


TEST_PREFIX = "TEST – "


def as_test(msg: EmailMessage, cfg: EmailCfg, now: datetime | None = None) -> EmailMessage:
    """Uloženou zprávu (.eml) připravit k opětovnému odeslání jako test.

    Obsah (tělo, textová verze, příloha) zůstává beze změny – o to v testu jde.
    Mění se jen hlavičky:
    - Message-ID nové: Gmail zprávu s Message-ID, které už ve schránce má,
      tiše zahodí, takže by opakovaně poslaný e-mail nikdy nedorazil.
    - From/To podle současného configu: .eml z --dry-run nemusí mít příjemce
      a nastavení se mohlo od uložení změnit.
    - Předmět s „TEST – “, ať se v poště neplete se skutečným souhrnem.
    """
    subject = str(msg.get("Subject", "")).removeprefix(TEST_PREFIX)
    for header in ("Subject", "From", "To", "Cc", "Bcc", "Date", "Message-ID"):
        del msg[header]  # del na chybějící hlavičku je v email.message bez chyby
    msg["Subject"] = TEST_PREFIX + subject
    msg["From"] = formataddr((SENDER_NAME, cfg.sender or cfg.user))
    msg["To"] = ", ".join(cfg.recipients)
    msg["Date"] = formatdate((now or datetime.now()).timestamp(), localtime=True)
    msg["Message-ID"] = make_msgid("jobagent", domain=(cfg.sender or cfg.user).rpartition("@")[2] or None)
    return msg


def _is_4xx(code: int | None) -> bool:
    return code is not None and 400 <= code < 500


def _decode(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
