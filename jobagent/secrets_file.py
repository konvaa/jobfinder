"""Úprava secrets.toml po přesunu hesla do Správce pověření: vymazat jen hodnotu hesla.

Proč textová úprava, a ne načíst TOML a zapsat znovu: tomllib umí jen číst
a zápis by stejně ztratil komentáře (návod v souboru). Mění se proto jediný
řádek `heslo = "…"` v sekci [email], všechno ostatní zůstává bajt po bajtu.

Proč bez zálohy (na rozdíl od config.toml): záloha by heslo jen přestěhovala
do dalšího souboru v čistém textu – a `secrets.toml.zaloha` by navíc
nezachytil vzor `secrets*.toml` v .gitignore. Heslo se maže až poté, co je
ve Správci pověření a přihlášení s ním prošlo, takže se nic neztratí.

Pojistka: výsledek se znovu přečte přes tomllib. Kdyby nebyl platný nebo by
heslo nebylo prázdné, soubor se nezmění.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

CLEARED_LINE = 'heslo = ""  # uloženo ve Správci pověření Windows (příkaz nastav-heslo)'
_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_PASSWORD = re.compile(r"^\s*heslo\s*=")


def has_email_password(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # cp1250 („ANSI“) nebo poškozený soubor: radši nesahat. Při běhu na
        # kódování upozorní load_config srozumitelnou chybou.
        return False
    return bool(str(data.get("email", {}).get("heslo", "")).strip())


def clear_email_password(path: Path) -> bool:
    """Vymazat [email] heslo. True = změněno. False = nebylo co měnit nebo to nejde bezpečně."""
    if not has_email_password(path):
        return False
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"  # Poznámkový blok ukládá CRLF – zachovat
    lines = text.split(newline)
    section, changed = "", False
    for i, line in enumerate(lines):
        match = _SECTION.match(line)
        if match:
            section = match.group(1).strip()
            continue
        if section == "email" and _PASSWORD.match(line):
            if '"""' in line or "'''" in line:
                return False  # víceřádkový řetězec – radši nesahat, poradí si uživatel
            lines[i] = CLEARED_LINE
            changed = True
    if not changed:
        return False
    new_text = newline.join(lines)
    try:
        data = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return False
    if data.get("email", {}).get("heslo", "") != "":
        return False
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    path.write_bytes(bom + new_text.encode("utf-8"))
    return True
