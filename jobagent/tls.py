"""TLS (HTTPS a šifrovaný e-mail): jak se ověřují certifikáty serverů.

Problém (zjištěno ve Windows Sandbox): OpenSSL v Pythonu jen čte kořenové
certifikáty, které už v úložišti Windows jsou. Čerstvé Windows ale většinu
kořenů nemá předem – stahuje je až na vyžádání, když řetězec ověřuje samo
(CryptoAPI: automatická aktualizace kořenů, dohledání mezilehlých
certifikátů přes AIA). Edge, .NET i pip proto fungují, Python s OpenSSL ne:
„unable to get local issuer certificate“. Ani návštěva webu v Edge
nepomůže spolehlivě – Windows sestaví řetězec jinou cestou, než ho pak
hledá OpenSSL (smtp.seznam.cz a jobscontact.cz v Sandboxu selhávaly dál).

Řešení: knihovna truststore (MIT, čistý Python přes ctypes, používá ji pip).
Spojení naváže OpenSSL jako dosud, ale řetězec certifikátů ověří Windows
(CertGetCertificateChain s výchozím enginem – stejně jako Edge), včetně
kontroly jména serveru. Stáhne si tedy i chybějící kořen.

Mimo Windows (zdrojáky na Linuxu) se používá ssl.create_default_context():
distribuční OpenSSL čte systémové úložiště správně a truststore by tam dělal
totéž. Import truststore je až ve funkci, takže zdrojáky na Linuxu běží i bez
něj. (PyInstaller import uvnitř funkce najde – prochází celý bajtkód.)
"""

from __future__ import annotations

import ssl
import sys

_USE_SYSTEM_TRUST = ("win32", "darwin")  # tam OpenSSL systémové úložiště sám nepoužije


def _platform() -> str:
    # Vlastní funkce kvůli testům: podvrhnout globální sys.platform by ovlivnilo
    # i modul ssl (začal by na Linuxu číst úložiště Windows).
    return sys.platform


def create_context() -> ssl.SSLContext:
    """Kontext pro klienta: ověřený certifikát i jméno serveru, TLS 1.2+."""
    if _platform() in _USE_SYSTEM_TRUST:
        try:
            import truststore
        except ImportError:
            pass  # běh ze zdrojáků bez nainstalovaného truststore – viz backend()
        else:
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


def backend() -> str:
    """Jak se certifikáty ověřují – do logu (diagnostika v Sandboxu / u kamaráda)."""
    if _platform() in _USE_SYSTEM_TRUST:
        try:
            import truststore  # noqa: F401
        except ImportError:
            return "OpenSSL – POZOR, chybí knihovna truststore (pip install -r requirements.txt)"
        return "systém (truststore, jako prohlížeč)"
    return "OpenSSL se systémovými certifikáty"


def is_cert_error(exc: BaseException | None) -> bool:
    """Selhalo ověření certifikátu? (i zabalené v urllib.error.URLError.reason)"""
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLCertVerificationError) or isinstance(exc, ssl.SSLCertVerificationError)


def cert_advice(host: str, *, mail: bool) -> str:
    """Srozumitelná rada k selhanému ověření certifikátu.

    Pořadí podle četnosti: antivir rozbalující šifrované spojení je doma
    nejčastější. Chybějící kořenový certifikát je s truststore vzácný –
    Windows si ho stáhne samo; zbývá počítač, kde to nejde (bez aktualizací,
    firemní zásady). Špatný čas třetí.
    """
    antivirus = ("antivir, který kontroluje poštu (např. „Mail Shield“ v Avastu/AVG): přidej výjimku pro "
                 f"{host} nebo kontrolu pošty vypni" if mail else
                 "antivir nebo firewall, který kontroluje šifrované stránky (např. „Kontrola HTTPS“): přidej "
                 f"výjimku pro {host}")
    return (
        f"Nepodařilo se ověřit certifikát serveru {host}. Možné příčiny: "
        f"1) {antivirus}; "
        "2) Windows nemá potřebný kořenový certifikát a nemůže si ho stáhnout (čerstvá instalace bez "
        "aktualizací, nebo firemní počítač, kde to správce zakázal) – připoj se k internetu, spusť "
        "Windows Update (Nastavení → Windows Update → Vyhledat aktualizace) a zkus to znovu, na firemním "
        "počítači se obrať na správce; "
        "3) v počítači je špatné datum nebo čas."
    )
