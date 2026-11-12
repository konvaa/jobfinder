"""Odkud se berou šablony e-mailu: výchozí uvnitř programu, nebo upravené v templates/.

Výchozí šablony leží v balíčku (jobagent/templates/, v exe uvnitř _internal\\).
Když vedle programu existuje složka templates\\, použije se každý soubor z ní
místo výchozího; soubor, který tam chybí, se vezme výchozí. Složku vytvoří
příkaz `jobagent sablony` (existující soubory nepřepíše).

Proč ne automatické nahrazování: upravené šablony patří uživateli. Program je
nikdy nepřepíše, jen při každém běhu porovná zástupné symboly ($nabidky …)
s výchozí verzí a upozorní (log + e-mail):
- chybí symbol, který výchozí šablona má → šablona je nejspíš ze starší verze
  a v e-mailu by něco chybělo (příklad: $priloha přibyl ve fázi 4);
- symbol navíc, který výchozí nezná → překlep; v e-mailu by zůstal vidět
  doslova (vykresluje se přes safe_substitute, takže nic nespadne).

Kontroluje se text bez komentářů (<!-- --> v HTML, řádky „##“ v email.txt),
protože komentáře symboly jen vyjmenovávají jako návod.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from string import Template

DEFAULT_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_FILES = ("email.html", "nabidka.html", "ostatni.html", "vyrazeno.html", "email.txt")


class TemplateError(Exception):
    pass


def strip_comments(name: str, text: str) -> str:
    """Odstranit návod v šabloně, aby se nevykresloval do e-mailu."""
    if name.endswith(".txt"):
        return "\n".join(line for line in text.splitlines() if not line.startswith("##"))
    # Komentáře vyjmenovávají proměnné ($nabidky …). Kdyby zůstaly, Template by
    # je nahradil i tam a e-mail by obsahoval celou tabulku podruhé (skrytě).
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def placeholders(name: str, text: str) -> set[str]:
    return set(Template(strip_comments(name, text)).get_identifiers())


class Templates:
    """Šablony pro jeden běh. override_dir = templates\\ vedle programu (nemusí existovat)."""

    def __init__(self, override_dir: Path | None = None, default_dir: Path = DEFAULT_DIR):
        self.override_dir = override_dir
        self.default_dir = default_dir

    def source(self, name: str) -> Path:
        if self.override_dir is not None:
            custom = self.override_dir / name
            if custom.is_file():
                return custom
        return self.default_dir / name

    def raw(self, name: str) -> str:
        path = self.source(name)
        try:
            return path.read_text(encoding="utf-8-sig")  # -sig: Poznámkový blok umí uložit s BOM
        except FileNotFoundError:
            raise TemplateError(f"Chybí šablona {path}. Program je nejspíš poškozený, rozbal ho znovu.") from None

    def get(self, name: str) -> Template:
        return Template(strip_comments(name, self.raw(name)))

    def check(self) -> list[str]:
        """Varování k upraveným šablonám (prázdný seznam = vše v pořádku nebo nic upraveného)."""
        warnings: list[str] = []
        if self.override_dir is None or not self.override_dir.is_dir():
            return warnings
        for name in TEMPLATE_FILES:
            custom = self.override_dir / name
            if not custom.is_file():
                continue
            try:
                mine = placeholders(name, custom.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError) as exc:
                warnings.append(f"Šablonu {custom} nejde přečíst ({exc}); oprav ji nebo smaž, pak se použije výchozí.")
                continue
            default = placeholders(name, (self.default_dir / name).read_text(encoding="utf-8"))
            missing, unknown = sorted(default - mine), sorted(mine - default)
            if missing:
                warnings.append(
                    f"Šablona templates/{name} nemá {', '.join('$' + p for p in missing)} – nejspíš je ze starší "
                    "verze programu a v e-mailu tahle část chybí. Přenes úpravy do nové verze: soubor přejmenuj, "
                    "spusť `jobagent sablony` a úpravy do nového souboru zkopíruj."
                )
            if unknown:
                warnings.append(
                    f"Šablona templates/{name} obsahuje neznámé {', '.join('$' + p for p in unknown)} (překlep?); "
                    "v e-mailu zůstanou vidět doslova."
                )
        return warnings


def export_defaults(target_dir: Path, default_dir: Path = DEFAULT_DIR) -> tuple[list[Path], list[Path]]:
    """Zkopírovat výchozí šablony do target_dir. Existující nepřepíše. Vrací (vytvořené, přeskočené)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    created, skipped = [], []
    for name in TEMPLATE_FILES:
        target = target_dir / name
        if target.exists():
            skipped.append(target)
            continue
        shutil.copyfile(default_dir / name, target)
        created.append(target)
    return created, skipped
