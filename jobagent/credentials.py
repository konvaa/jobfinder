"""Heslo k e-mailu ve Správci pověření Windows (Credential Manager) místo v secrets.toml.

Proč: soubor s heslem v čistém textu se snadno rozšíří dál (kamarád pošle
složku, OneDrive ji zálohuje, na snímku obrazovky je otevřený Poznámkový blok).
Správce pověření drží heslo zašifrované pro tvůj účet Windows (DPAPI).
Poctivě: před škodlivým programem spuštěným pod tvým účtem to nechrání, ten
si heslo přečte stejně jako Job Agent.

Bez knihovny: přímo funkce Windows CredWriteW / CredReadW / CredDeleteW přes
ctypes. Rozdíly oproti běžnému Pythonu, na které je tu potřeba dát pozor:
- Paměť, kterou vrátí CredReadW, alokuje Windows a garbage collector o ní
  neví – musí se uvolnit voláním CredFree (v `finally`), jinak uniká.
- Chybový kód se čte přes ctypes.get_last_error() a knihovna se načítá
  s use_last_error=True. Obyčejné GetLastError() by mohlo vrátit kód
  z nějakého volání, které mezitím udělal sám Python.
- Heslo se ukládá jako UTF-16LE (Windows „wide“ řetězce), stejně jako ho
  ukládá `cmdkey` a správce v Ovládacích panelech.

Položka je „obecné pověření“ s názvem JobAgent/<uzivatel>@<server>, uložená
jen pro tento počítač (CRED_PERSIST_LOCAL_MACHINE, necestuje s profilem).
Najdeš ji v Ovládací panely → Správce přihlašovacích údajů → Přihlašovací
údaje systému Windows. Na jiném počítači je potřeba `nastav-heslo` znovu.

Mimo Windows modul nic neukládá (available() = False) a heslo se bere ze
secrets.toml – to je pro běh ze zdrojáků na Linuxu.
"""

from __future__ import annotations

import sys
from typing import Protocol

TARGET_PREFIX = "JobAgent/"
COMMENT = "Job Agent – heslo k odesílání souhrnu e-mailem"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_BLOB_BYTES = 5 * 512  # CRED_MAX_CREDENTIAL_BLOB_SIZE


class CredentialError(Exception):
    pass


class Store(Protocol):
    """Rozhraní úložiště hesel – v testech ho nahrazuje slovník."""

    def read(self, target: str) -> str | None: ...
    def write(self, target: str, user: str, secret: str) -> None: ...
    def delete(self, target: str) -> bool: ...


def target_name(user: str, server: str) -> str:
    # Server je v názvu záměrně: změna serveru (jiná schránka) = jiné heslo.
    return f"{TARGET_PREFIX}{user}@{server}"


def available() -> bool:
    return sys.platform == "win32"


class WindowsStore:
    """Správce pověření Windows přes ctypes. Vytvářet jen na Windows."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class CREDENTIALW(ctypes.Structure):
            # Pořadí a typy polí musí přesně odpovídat struktuře CREDENTIALW z wincred.h.
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._ctypes = ctypes
        self._CREDENTIALW = CREDENTIALW
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        PCRED = ctypes.POINTER(CREDENTIALW)

        self._write = advapi32.CredWriteW
        self._write.argtypes = [PCRED, wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._read = advapi32.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCRED)]
        self._read.restype = wintypes.BOOL
        self._free = advapi32.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None
        self._delete = advapi32.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._PCRED = PCRED

    def _error(self, action: str) -> CredentialError:
        code = self._ctypes.get_last_error()
        return CredentialError(f"Správce pověření Windows: {action} selhalo ({self._ctypes.WinError(code).strerror}, kód {code}).")

    def read(self, target: str) -> str | None:
        ctypes = self._ctypes
        pcred = self._PCRED()
        if not self._read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            if ctypes.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise self._error("čtení hesla")
        try:
            size = pcred.contents.CredentialBlobSize
            if not size:
                return ""
            blob = ctypes.string_at(pcred.contents.CredentialBlob, size)
        finally:
            self._free(pcred)  # paměť patří Windows, GC ji neuvolní
        return blob.decode("utf-16-le")

    def write(self, target: str, user: str, secret: str) -> None:
        ctypes = self._ctypes
        blob = secret.encode("utf-16-le")
        if len(blob) > MAX_BLOB_BYTES:
            raise CredentialError("Heslo je na Správce pověření příliš dlouhé.")
        buffer = (ctypes.c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
        cred = self._CREDENTIALW()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = COMMENT
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = user
        if not self._write(ctypes.byref(cred), 0):
            raise self._error("uložení hesla")

    def delete(self, target: str) -> bool:
        if self._delete(target, CRED_TYPE_GENERIC, 0):
            return True
        if self._ctypes.get_last_error() == ERROR_NOT_FOUND:
            return False
        raise self._error("smazání hesla")


def default_store() -> Store | None:
    return WindowsStore() if available() else None


def stored_password(store: Store | None, user: str, server: str) -> str | None:
    """Heslo ze Správce pověření, nebo None (není uložené / není Windows / chybí uživatel)."""
    if store is None or not user or not server:
        return None
    return store.read(target_name(user, server))
