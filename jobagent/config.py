"""Načtení a kontrola config.toml + secrets.toml.

Proč tolik kontroly: config upravují i neprogramátoři v Poznámkovém bloku.
Překlep v názvu klíče by jinak tiše znamenal „použij výchozí hodnotu“
a uživatel by nechápal, proč nastavení nefunguje. Proto:
  * neznámé klíče hlásíme jako varování („překlep?“),
  * špatné typy hlásíme všechny najednou, ne po jednom,
  * chyby jsou česky a s cestou ke klíči (např. filtry.min_mzda).

Proč české klíče v TOML (vyradit, min_mzda): config je pro uživatele, ne pro
kód. Bez diakritiky, protože TOML povoluje v holých klíčích jen ASCII.
V kódu se pak pracuje s anglickými názvy polí dataclass.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobagent.text import variants, wildcard_problem


class ConfigError(Exception):
    """Config nejde použít. Zpráva je určená přímo uživateli."""


# ---------------------------------------------------------------- dataclassy


@dataclass
class General:
    max_pages: int = 3
    detail_limit: int = 40
    delay_min: float = 1.0
    delay_max: float = 3.0
    timeout: int = 20


@dataclass
class Search:
    name: str
    url: str
    portal: str | None = None  # None = pozná se podle domény v URL
    max_pages: int | None = None  # None = obecne.max_stranek


@dataclass
class PlaceRule:
    contains: str
    min_salary: int


@dataclass
class Filters:
    max_age_days: int | None = 7
    # Pro hledání, které ještě nemělo ostrý běh (nové v configu nebo úplně
    # první spuštění): delší okno, aby se neztratily starší zajímavé nabídky.
    max_age_first_run: int | None = 30
    min_salary: int | None = None
    hours_per_month: int = 173  # 40 h týdně × 52 / 12
    reject_words: list[str] = field(default_factory=list)  # kdekoli v inzerátu
    reject_title_words: list[str] = field(default_factory=list)  # jen v názvu pozice
    places: list[PlaceRule] = field(default_factory=list)


@dataclass
class Scoring:
    # Kladné body přidávají, záporné ubírají. Bývalé „penalizovat“ je jen
    # záporné skóre, takže samostatný seznam nebyl potřeba a každé slovo
    # má teď vlastní srážku.
    words: dict[str, int] = field(default_factory=dict)  # název (× násobek), štítky, popis
    title_words: dict[str, int] = field(default_factory=dict)  # jen název (krátká slova jako „IT“)
    title_multiplier: int = 2
    min_score: int | None = None
    # Body za skrytou mzdu „pravděpodobně pod minimem“ (podle filtru mzdy
    # portálu). Záporné číslo, 0 = jen označit v e-mailu, bez srážky.
    hidden_salary_penalty: int = 0


@dataclass
class EmailCfg:
    smtp_server: str = "smtp.seznam.cz"
    port: int = 465
    # "ssl" = šifrované od začátku (port 465), "starttls" = šifrování se zapne
    # po připojení (port 587). Nešifrované SMTP program neumí: heslo by šlo sítí čitelně.
    security: str = "ssl"
    user: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    subject: str = "Job Agent"  # předpona předmětu: „Job Agent: 5 nových (2 v hlavní tabulce)“


@dataclass
class Summary:
    score_threshold: int | None = None  # nad hranicí hlavní tabulka, pod ní sbalená sekce „ostatní“
    show_rejected: bool = True
    max_rejected: int = 30


@dataclass
class AiCfg:
    enabled: bool = False
    model: str = "claude-haiku-5-5"
    max_jobs: int = 20


@dataclass
class Secrets:
    email_password: str = ""
    api_key: str = ""


@dataclass
class Config:
    general: General
    searches: list[Search]
    filters: Filters
    scoring: Scoring
    email: EmailCfg
    summary: Summary
    ai: AiCfg
    secrets: Secrets
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- čtení sekcí


class _Section:
    """Obal nad jednou tabulkou TOML, který si pamatuje přečtené klíče.

    Díky tomu na konci víme, které klíče v souboru byly, ale kód je nezná
    (= nejspíš překlep), a můžeme na ně upozornit.
    """

    def __init__(self, data: Any, path: str, errors: list[str], warnings: list[str]):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            errors.append(f"{path}: má to být sekce [{path}], ne jedna hodnota.")
            data = {}
        self.data = data
        self.path = path
        self.errors = errors
        self.warnings = warnings
        self.used: set[str] = set()

    def get(self, key: str, kind: type | tuple, default: Any = None, *, required: bool = False) -> Any:
        self.used.add(key)
        where = f"{self.path}.{key}" if self.path else key
        if key not in self.data:
            if required:
                self.errors.append(f"{where}: chybí povinná hodnota.")
            return default
        value = self.data[key]
        # V Pythonu je bool podtřída int; „ssl = 1“ nechceme brát jako bool a „port = true“ jako číslo.
        if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
            self.errors.append(f"{where}: má to být celé číslo, je tam {value!r}.")
            return default
        if kind is float and (isinstance(value, bool) or not isinstance(value, (int, float))):
            self.errors.append(f"{where}: má to být číslo, je tam {value!r}.")
            return default
        if kind is float:
            return float(value)
        if kind is bool and not isinstance(value, bool):
            self.errors.append(f"{where}: má to být true nebo false (malými písmeny, bez uvozovek), je tam {value!r}.")
            return default
        if kind is str and not isinstance(value, str):
            self.errors.append(f"{where}: má to být text v uvozovkách, je tam {value!r}.")
            return default
        return value

    def get_str_list(self, key: str, default: list[str] | None = None, *, keywords: bool = False) -> list[str]:
        value = self.get(key, list, None)
        where = f"{self.path}.{key}"
        if value is None:
            return list(default or [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.errors.append(f'{where}: má to být seznam textů, např. ["první", "druhý"].')
            return list(default or [])
        if not keywords:
            return [v for v in value if v.strip()]
        cleaned = (_clean_key(v, f"{self.path}.{key}", self.warnings) for v in value if v.strip())
        return [v for v in cleaned if v]

    def sub(self, key: str) -> "_Section":
        self.used.add(key)
        return _Section(self.data.get(key), f"{self.path}.{key}" if self.path else key, self.errors, self.warnings)

    def finish(self) -> None:
        for key in self.data:
            if key not in self.used:
                where = f"{self.path}.{key}" if self.path else key
                self.warnings.append(f"Neznámé nastavení „{where}“ se ignoruje (překlep?).")


def _read_toml(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    try:
        # utf-8-sig: Poznámkový blok umí uložit „UTF-8 s BOM“ a tomllib by
        # na neviditelném BOM znaku na začátku souboru spadl.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path.name}: soubor není uložený v kódování UTF-8. "
            "V Poznámkovém bloku dej Uložit jako → Kódování: UTF-8."
        )
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: chyba zápisu – {exc}. Zkontroluj uvozovky, závorky a čárky na tom řádku.")


# ---------------------------------------------------------------- hlavní funkce


def load_config(config_path: Path, secrets_path: Path | None = None) -> Config:
    if not config_path.exists():
        raise ConfigError(
            f"Nenašel jsem {config_path.name} ve složce {config_path.parent}. "
            "Zkopíruj config.example.toml, přejmenuj kopii na config.toml a uprav ji."
        )
    data = _read_toml(config_path)
    errors: list[str] = []
    warnings: list[str] = []
    root = _Section(data, "", errors, warnings)

    g = root.sub("obecne")
    general = General(
        max_pages=g.get("max_stranek", int, 3),
        detail_limit=g.get("limit_detailu", int, 40),
        delay_min=g.get("prodleva_od", float, 1.0),
        delay_max=g.get("prodleva_do", float, 3.0),
        timeout=g.get("timeout", int, 20),
    )
    g.finish()
    if general.delay_min < 0.5:
        # Pod půl sekundy už nejde o slušné scrapování a riskujeme blokaci IP.
        warnings.append("obecne.prodleva_od je příliš malá, používám 0.5 s.")
        general.delay_min = 0.5
    if general.delay_max < general.delay_min:
        general.delay_max = general.delay_min

    root.used.add("hledani")
    raw_searches = data.get("hledani", [])
    if not isinstance(raw_searches, list):
        errors.append("hledani: každé hledání musí začínat řádkem [[hledani]] (dvojité hranaté závorky).")
        raw_searches = []
    searches: list[Search] = []
    for i, item in enumerate(raw_searches, start=1):
        s = _Section(item, f"hledani[{i}]", errors, warnings)
        url = s.get("url", str, "", required=True)
        searches.append(
            Search(
                name=s.get("nazev", str, f"Hledání {i}"),
                url=url.strip(),
                portal=s.get("portal", str, None),
                max_pages=s.get("max_stranek", int, None),
            )
        )
        s.finish()
        if url and not url.strip().lower().startswith(("http://", "https://")):
            errors.append(f"hledani[{i}].url: musí začínat https:// (zkopíruj celou adresu z prohlížeče).")
    if not searches:
        errors.append("Není nastavené žádné hledání. Přidej aspoň jeden blok [[hledani]] s url.")

    f = root.sub("filtry")
    places: list[PlaceRule] = []
    f.used.add("mista")
    raw_places = f.data.get("mista", [])
    if not isinstance(raw_places, list):
        errors.append("filtry.mista: každé místo musí začínat řádkem [[filtry.mista]].")
        raw_places = []
    for i, item in enumerate(raw_places, start=1):
        p = _Section(item, f"filtry.mista[{i}]", errors, warnings)
        contains = p.get("obsahuje", str, "", required=True)
        min_salary = p.get("min_mzda", int, None, required=True)
        p.finish()
        if contains and min_salary is not None:
            places.append(PlaceRule(contains=contains, min_salary=min_salary))
    max_age = f.get("max_stari_dni", int, 7)
    max_age_first = f.get("max_stari_prvni_beh", int, 30)
    filters = Filters(
        max_age_days=max_age if max_age and max_age > 0 else None,  # 0 = filtr vypnutý
        max_age_first_run=max_age_first if max_age_first and max_age_first > 0 else None,
        min_salary=f.get("min_mzda", int, None) or None,
        hours_per_month=f.get("hodin_mesicne", int, 173),
        reject_words=f.get_str_list("vyradit", keywords=True),
        reject_title_words=f.get_str_list("vyradit_v_nazvu", keywords=True),
        places=places,
    )
    # Zrušené klíče: konkrétní návod místo obecného „překlep?“, jinak by
    # srážky ze staršího configu tiše přestaly platit.
    for old in ("penalizovat", "penalizace"):
        if old in f.data:
            f.used.add(old)
            warnings.append(
                f"filtry.{old} už neexistuje a ignoruje se. Penalizační slova přesuň do "
                '[hodnoceni.slova] se záporným počtem bodů, např. "směn*" = -30.'
            )
    f.finish()

    h = root.sub("hodnoceni")
    scoring = Scoring(
        words=_word_points(h.sub("slova"), errors),
        title_words=_word_points(h.sub("slova_v_nazvu"), errors),
        title_multiplier=h.get("nasobek_v_nazvu", int, 2),
        min_score=h.get("min_skore", int, None),
        hidden_salary_penalty=h.get("srazka_skryta_mzda", int, 0),
    )
    if scoring.hidden_salary_penalty > 0:
        warnings.append(
            f"hodnoceni.srazka_skryta_mzda = {scoring.hidden_salary_penalty} je kladné číslo, tedy bonus. "
            f"Srážka má být záporná, beru {-scoring.hidden_salary_penalty}."
        )
        scoring.hidden_salary_penalty = -scoring.hidden_salary_penalty
    h.finish()

    e = root.sub("email")
    email = EmailCfg(
        smtp_server=e.get("smtp_server", str, "smtp.seznam.cz"),
        port=e.get("port", int, 465),
        security=_email_security(e, warnings, errors),
        user=e.get("uzivatel", str, ""),
        sender=e.get("odesilatel", str, ""),
        recipients=e.get_str_list("prijemci"),
        subject=e.get("predmet", str, "Job Agent"),
    )
    e.finish()
    _check_email(email, warnings)

    o = root.sub("souhrn")
    summary = Summary(
        score_threshold=o.get("hranice_skore", int, None),
        show_rejected=o.get("zobrazit_vyrazene", bool, True),
        max_rejected=o.get("max_vyrazenych", int, 30),
    )
    o.finish()

    a = root.sub("ai")
    ai = AiCfg(
        enabled=a.get("zapnuto", bool, False),
        model=a.get("model", str, "claude-haiku-5-5"),
        max_jobs=a.get("max_inzeratu", int, 20),
    )
    a.finish()

    root.finish()

    secrets = load_secrets(secrets_path, errors, warnings) if secrets_path else Secrets()

    if errors:
        raise ConfigError("V nastavení jsou chyby:\n  - " + "\n  - ".join(errors))

    return Config(
        general=general,
        searches=searches,
        filters=filters,
        scoring=scoring,
        email=email,
        summary=summary,
        ai=ai,
        secrets=secrets,
        warnings=warnings,
    )


def _clean_key(key: str, where: str, warnings: list[str]) -> str | None:
    """Zkontroluje slovo nebo skupinu „a | b | c“; vrátí platnou podobu, nebo None.

    Pravidla pro hvězdičky platí pro každou variantu zvlášť. Neplatná varianta
    se vynechá s varováním a zbytek skupiny dál platí. Varování, ne chyba:
    kvůli jednomu slovu nemá denní běh spadnout, ale uživatel se to musí
    dozvědět (log i e-mail).
    """
    valid = []
    for variant in variants(key):
        problem = wildcard_problem(variant)
        if problem:
            warnings.append(f"{where}: „{variant}“ se ignoruje – {problem}.")
        else:
            valid.append(variant)
    return " | ".join(valid) if valid else None


def _word_points(section: _Section, errors: list[str]) -> dict[str, int]:
    """Sekce ve tvaru  "slovo" = body  (body můžou být záporné)."""
    words: dict[str, int] = {}
    for word, points in section.data.items():
        section.used.add(word)
        if isinstance(points, bool) or not isinstance(points, int):
            errors.append(f'{section.path}: u „{word}“ má být celé číslo bodů, např. "{word}" = 10 nebo -10.')
            continue
        key = _clean_key(word, section.path, section.warnings)
        if key:
            words[key] = points
    return words


SECURITY_PORTS = {"ssl": 465, "starttls": 587}


def _email_security(e: "_Section", warnings: list[str], errors: list[str]) -> str:
    """email.zabezpeceni („ssl“ / „starttls“). Starý klíč ssl = true/false ještě funguje.

    Proč změna: true/false neumí říct STARTTLS, který je potřeba pro záložní
    port 587 (když poskytovatel internetu nebo antivir blokuje 465).
    """
    legacy = e.get("ssl", bool, None)
    value = e.get("zabezpeceni", str, None)
    if legacy is not None:
        new = "ssl" if legacy else "starttls"
        warnings.append(
            f'email.ssl = {str(legacy).lower()} je starý zápis. Nahraď ho řádkem  zabezpeceni = "{new}"  '
            '("ssl" pro port 465, "starttls" pro port 587).'
            + ("" if legacy else " Nešifrované odesílání program neumí, beru starttls.")
        )
        if value is None:
            value = new
    value = (value or "ssl").strip().lower()
    if value not in SECURITY_PORTS:
        errors.append(f'email.zabezpeceni: má být "ssl" nebo "starttls", je tam "{value}".')
        return "ssl"
    return value


def _check_email(email: EmailCfg, warnings: list[str]) -> None:
    """Varování, ne chyby: config se má načíst i bez e-mailu (dry-run, vysvetli)."""
    expected = SECURITY_PORTS[email.security]
    other = next(s for s, p in SECURITY_PORTS.items() if p == email.port) if email.port in SECURITY_PORTS.values() else None
    if other and other != email.security:
        warnings.append(
            f'email: port {email.port} obvykle patří k zabezpeceni = "{other}", nastaveno je "{email.security}" '
            f"(pro {email.security} bývá port {expected}). Když odeslání selže, sjednoť je."
        )
    if email.sender and email.user and email.sender.strip().lower() != email.user.strip().lower():
        warnings.append(
            f"email: odesilatel ({email.sender}) je jiný než uzivatel ({email.user}). Seznam i Gmail "
            "obvykle povolí odesílat jen z adresy účtu, pod kterým se program přihlašuje."
        )


def load_secrets(path: Path, errors: list[str], warnings: list[str]) -> Secrets:
    """secrets.toml je nepovinný: bez něj nejde jen odeslat e-mail a AI.

    Proč samostatný soubor: config.toml se dá poslat kamarádovi jako vzor,
    aniž by s ním odešlo heslo k e-mailu nebo API klíč.
    """
    if not path.exists():
        return Secrets()
    data = _read_toml(path)
    root = _Section(data, "", errors, warnings)
    e = root.sub("email")
    a = root.sub("ai")
    secrets = Secrets(
        email_password=e.get("heslo", str, ""),
        api_key=a.get("api_klic", str, ""),
    )
    e.finish()
    a.finish()
    root.finish()
    return secrets
