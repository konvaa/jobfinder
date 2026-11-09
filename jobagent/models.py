"""Společný datový objekt inzerátu, který vrací každý collector.

Proč dataclass a ne dict: všechny portály musí vracet stejný tvar dat.
Dataclass to vynutí (překlep v názvu pole spadne hned, ne až v e-mailu)
a editor napovídá. Výsledky vyhodnocení (skóre, důvod vyřazení) jsou přímo
na objektu, protože je pipeline nastavuje postupně a šablona e-mailu je čte
z jednoho místa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Salary:
    """Rozparsovaná mzda. None znamená „nevíme“, ne nulu."""

    low: int | None
    high: int | None
    period: str | None  # "month" | "hour" | "year" | None = jednotka neuvedena
    currency: str | None  # "CZK" | "EUR" | None = neuvedeno
    raw: str = ""


@dataclass
class HiddenSalary:
    """Co o skryté mzdě prozradil filtr mzdy portálu (inzerát sám mzdu neukazuje).

    Portály jobs.cz a prace.cz filtrují i podle mzdy, kterou firma zadala
    a nezveřejnila. Hledání se mzdou ≥ minimum, které inzerát nevrátí, tak
    říká „skrytá mzda je pravděpodobně pod minimem“. Jistota to není, proto
    srážka, ne vyřazení.
    """

    below: bool  # True = filtr portálu inzerát vyřadil, False = prošel
    portals: list[str]  # kdo to tvrdí („jobs.cz“, „jobs.cz a prace.cz“)
    minimum: int  # částka, se kterou se hledalo

    @property
    def label(self) -> str:
        from jobagent.text import format_czk  # lokálně: models nemá záviset na zbytku

        who = " a ".join(self.portals)
        if self.below:
            return f"skrytá, podle {who} pravděpodobně pod minimem"
        return f"skrytá, podle {who} ≥ {format_czk(self.minimum)} Kč"

    def to_json(self) -> str:
        import json

        return json.dumps({"pod": self.below, "portaly": self.portals, "minimum": self.minimum}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "HiddenSalary | None":
        import json

        if not raw:
            return None
        data = json.loads(raw)
        return cls(below=bool(data["pod"]), portals=list(data["portaly"]), minimum=int(data["minimum"]))


@dataclass
class Job:
    portal: str  # "jobs.cz"
    native_id: str  # ID inzerátu na portálu (stabilní napříč běhy)
    url: str
    title: str
    company: str = ""
    location: str = ""
    salary_text: str = ""  # text mzdy tak, jak ho portál ukazuje
    posted: date | None = None  # zveřejnění nebo aktualizace
    tags: list[str] = field(default_factory=list)  # štítky typu „Home office“
    description: str = ""  # SUROVÝ text z detailu (prázdný, dokud detail nestáhneme)
    search_name: str = ""  # první hledání z configu, které inzerát našlo
    # Identita stejného inzerátu na jiném portálu, např. "jobs.cz:2001502919"
    # u inzerátu z prace.cz. Podle ní se pozná duplicita bez stahování detailu.
    alt_key: str = ""
    # Další portály, kde se stejný inzerát našel (pro e-mail „jobs.cz + prace.cz“).
    also_on: list[str] = field(default_factory=list)
    # Sloučené duplicity (jiné portály). Jen za běhu: slouží jako záloha textu.
    duplicates: list["Job"] = field(default_factory=list, repr=False, compare=False)
    # Výsledek hledání se mzdou za běhu: {portál: prošel filtrem mzdy?}.
    # Plní pipeline jen tam, kde je odpověď jistá (viz pipeline._salary_pass).
    salary_checks: dict[str, bool] = field(default_factory=dict, repr=False)
    # Vyhodnocená skrytá mzda (jen u inzerátů bez viditelné mzdy), jinak None.
    hidden_salary: HiddenSalary | None = None
    detail_fetched: bool = False
    # Proč inzerát nemá popis, i když by měl (stránka bez textu, limit…).
    # Zobrazí se v e-mailu, aby nízké skóre nevypadalo jako „nic nesedí“.
    detail_note: str = ""

    # --- výsledky vyhodnocení (plní pipeline) ---
    reject_reason: str | None = None
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    # Body jen za slova v názvu. Řadí se podle nich nabídky bez popisu, jejichž
    # celkové skóre se s ostatními nedá srovnat (chybí body z popisu).
    title_score: int = 0

    # Mezipaměť pro text: (surový popis, očištěný popis). Mimo porovnávání a výpis.
    _clean_cache: tuple[str, str] | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def text(self) -> str:
        """Popis po očištění (cleaning.clean_description).

        Podle něj se filtruje, boduje i (ve fázi 5) posílá do AI. Surový popis
        zůstává v `description` a v DB, takže lepší pravidla čištění zafungují
        i na starších inzerátech. Mezipaměť se obnoví, když se popis změní.
        """
        from jobagent.cleaning import clean_description  # lokálně: models nemá záviset na zbytku

        if self._clean_cache is None or self._clean_cache[0] != self.description:
            self._clean_cache = (self.description, clean_description(self.description)[0])
        return self._clean_cache[1]

    @property
    def key(self) -> str:
        """Primární klíč v DB. Kombinace s portálem, protože ID se mezi portály můžou překrývat."""
        return f"{self.portal}:{self.native_id}"

    @property
    def identity(self) -> str:
        """Totožnost inzerátu napříč portály: ID jobs.cz, když ho známe, jinak vlastní klíč."""
        return self.alt_key or self.key
