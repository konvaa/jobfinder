"""Vykreslení e-mailu ze šablon a náhled pro `--dry-run`.

Šablony leží ve složce templates/ vedle programu a používají string.Template
ze standardní knihovny ($pozice, $firma …). Proč ne Jinja2: další závislost,
a hlavně v šabloně nejde napsat kód, takže úprava od neprogramátora nic
nerozbije. Neznámá proměnná zůstane v e-mailu vidět jako „$preklep“
(safe_substitute), takže se chyba dá najít okem a program kvůli ní nespadne.

Proč víc souborů (email, nabidka, ostatni, vyrazeno): string.Template neumí
cykly, proto se řádek vykreslí zvlášť pro každý inzerát a výsledek se vloží
do kostry.

Sekce „Zajímavé podle názvu“ má nabídky bez textu detailu (kariérní weby
s JavaScriptem apod.) s kladnými body v názvu, řazené podle bodů v názvu.
Jejich celkové skóre se s ostatními nedá srovnat, proto vlastní sekce.

Sekce „Ostatní“ (pod hranicí skóre + bez popisu bez bodů v názvu) je v <details>. Sbalit ji umí
Thunderbird a Apple Mail. Gmail, Outlook a webmaily značku ignorují
a ukážou obsah rozbalený, proto jsou tam úsporné jednořádkové záznamy,
ne celá tabulka.

Odesílání přes SMTP přibude ve fázi 4.
"""

from __future__ import annotations

import html
import logging
import re
import webbrowser
from datetime import datetime
from pathlib import Path
from string import Template

from jobagent import collectors
from jobagent.models import Job
from jobagent.pipeline import RunReport

log = logging.getLogger(__name__)

TEMPLATE_FILES = ("email.html", "nabidka.html", "ostatni.html", "vyrazeno.html")


class TemplateError(Exception):
    pass


def _load(templates_dir: Path, name: str) -> Template:
    path = templates_dir / name
    if not path.exists():
        raise TemplateError(
            f"Chybí šablona {path}. Složka templates/ musí ležet vedle programu (zkopíruj ji z původního balíčku)."
        )
    text = path.read_text(encoding="utf-8-sig")
    # Komentáře v šabloně slouží jako návod a vyjmenovávají proměnné ($nabidky …).
    # Kdyby zůstaly, Template by je nahradil i tam a e-mail by obsahoval
    # celou tabulku podruhé (skrytě v komentáři). Proto je před vykreslením odstraníme.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return Template(text)


def _e(value: object) -> str:
    """HTML escape. Text inzerátu přichází z cizího webu a nesmí rozbít (ani zneužít) HTML e-mailu."""
    return html.escape(str(value), quote=True)


def split_by_threshold(jobs: list[Job], threshold: int | None) -> tuple[list[Job], list[Job]]:
    if threshold is None:
        return jobs, []
    return [j for j in jobs if j.score >= threshold], [j for j in jobs if j.score < threshold]


def render(report: RunReport, templates_dir: Path, now: datetime | None = None, threshold: int | None = None) -> str:
    now = now or datetime.now()
    page = _load(templates_dir, "email.html")
    row = _load(templates_dir, "nabidka.html")
    other_row = _load(templates_dir, "ostatni.html")
    rejected_row = _load(templates_dir, "vyrazeno.html")

    main, others = split_by_threshold(report.new_jobs, threshold)
    others = others + report.no_text_other  # bez popisu a bez bodů v názvu: jen pro úplnost
    if main:
        rows = "\n".join(row.safe_substitute(_job_values(job)) for job in main)
    elif others or report.title_jobs:
        rows = ('<tr><td colspan="7" style="padding:12px;color:#555">V hlavní tabulce dnes nic. '
                "Podívej se do sekcí níže.</td></tr>")
    else:
        rows = '<tr><td colspan="7" style="padding:12px;color:#555">Dnes nic nového. Program proběhl v pořádku.</td></tr>'

    title_html = ""
    if report.title_jobs:
        title_rows = "\n".join(row.safe_substitute(_job_values(job, score=job.title_score)) for job in report.title_jobs)
        title_html = (
            f'<h3 style="margin:28px 0 4px;font-size:16px">Zajímavé podle názvu – bez popisu, otevři ručně ({len(report.title_jobs)})</h3>'
            '<p style="margin:0 0 8px;color:#777;font-size:12px">Text inzerátu se nepodařilo získat '
            "(kariérní web firmy načítaný JavaScriptem nebo opakovaná chyba stažení, důvod je u každé nabídky). "
            "Skóre = jen body za slova v názvu, s hlavní tabulkou se nedá srovnat.</p>"
            '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px">'
            '<tr style="background:#546e7a;color:#fff;text-align:left"><th style="padding:6px 8px">Pozice</th>'
            '<th style="padding:6px 8px">Firma</th><th style="padding:6px 8px">Místo</th><th style="padding:6px 8px">Mzda</th>'
            '<th style="padding:6px 8px">Body v názvu</th><th style="padding:6px 8px">Proč</th><th style="padding:6px 8px">Portál</th></tr>'
            f"{title_rows}</table>"
        )

    others_html = ""
    if others:
        other_rows = "\n".join(other_row.safe_substitute(_job_values(job)) for job in others)
        others_html = (
            '<details style="margin:24px 0 0"><summary style="cursor:pointer;font-size:15px;font-weight:bold;color:#333">'
            f"Ostatní nabídky ({len(others)})"
            + (f" – skóre pod {threshold} nebo bez popisu" if threshold is not None else " – bez popisu")
            + "</summary>"
            f'<div style="margin-top:8px">{other_rows}</div></details>'
        )

    rejected_html = ""
    if report.rejected:
        rejected_rows = "\n".join(rejected_row.safe_substitute(_job_values(job)) for job in report.rejected)
        shown = len(report.rejected)
        more = f" (zobrazeno {shown} z {report.rejected_total})" if report.rejected_total > shown else ""
        rejected_html = (
            f'<h3 style="margin:28px 0 8px;font-size:15px;color:#555">Vyřazené inzeráty{more}</h3>'
            '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:13px;color:#555">'
            f"{rejected_rows}</table>"
        )

    return page.safe_substitute(
        datum=_e(now.strftime("%d. %m. %Y %H:%M")),
        stav=_status_line(report, len(main), len(others)),
        detaily=_details_line(report),
        varovani=_warnings_html(report),
        pocet_novych=str(len(main)),
        nabidky=rows,
        podle_nazvu=title_html,
        ostatni=others_html,
        vyrazene=rejected_html,
        hodnoceni=_e(report.scoring_method),
        rezim="NÁHLED (dry-run) – nic nebylo odesláno" if report.dry_run else "",
    )


def _job_values(job: Job, score: int | None = None) -> dict[str, str]:
    return {
        "pozice": _e(job.title),
        "firma": _e(job.company or "—"),
        "misto": _e(job.location or "—"),
        "mzda": _e(salary_label(job)),
        "skore": _e(job.score if score is None else score),
        "zduvodneni": _e(", ".join(job.score_reasons) or "žádné klíčové slovo"),
        "poznamka": _e(job.detail_note),
        "odkaz": _e(job.url),
        "portal": _e(" + ".join([job.portal, *job.also_on])),
        "pridano": _e(job.posted.strftime("%d. %m.") if job.posted else ""),
        "duvod": _e(job.reject_reason or ""),
    }


def salary_label(job: Job) -> str:
    """Text do sloupce Mzda: viditelná mzda, jinak co řekl filtr mzdy portálu."""
    if job.salary_text:
        return job.salary_text
    if job.hidden_salary:
        return job.hidden_salary.label
    return "neuvedeno"


def _status_line(report: RunReport, main: int, others: int) -> str:
    """Stavový řádek, který se sečte beze zbytku (viz účetnictví v pipeline.py).

    nalezeno = unikátních + opakovaných
    unikátních = nových + čeká na detail + vyřazeno [+ dříve vyřazené + duplicity + už odesláno]
    nových = v hlavní tabulce + ostatních + podle názvu
    Volitelné položky se ukazují jen nenulové, aby řádek nebyl zbytečně dlouhý.
    """
    portals = sorted({s.portal for s in report.stats if s.portal})
    found = f"nalezeno {report.found_total}"
    if report.repeated:
        found += f" = {report.unique} unikátních + {report.repeated} opakovaných v dalším hledání"
    # Rozpad nových = tři sekce e-mailu; sečtou se přesně na „nových N“.
    title = len(report.title_jobs)
    breakdown = [f"{main} v hlavní tabulce"]
    if others:
        breakdown.append(f"{others} ostatních")
    if title:
        breakdown.append(f"{title} podle názvu")
    new = f"nových {report.shown_total}"
    if len(breakdown) > 1:
        new += f" ({', '.join(breakdown)})"
    if report.listing_only:
        # „z nich“: podmnožina nových napříč sekcemi, ne další kategorie.
        new += f", z nich {report.listing_only} jen podle výpisu"
    top = collectors.COLLECTORS[0].name if collectors.COLLECTORS else ""
    for portal, (count, with_top_id) in report.only_on_secondary().items():
        # Také podmnožina nových: kolik přinesl jen tento portál (na hlavním je hledání nenašla).
        new += f"; jen na {portal} {count}"
        if with_top_id:
            new += f" (z toho {with_top_id} existuje i na {top}, jen mimo tvá hledání)"
    parts = [f"Portály: {', '.join(portals) or '—'} ({len(report.stats)} hledání)", found, new]
    if report.pending:
        parts.append(f"čeká na detail {len(report.pending)}")
    parts.append(f"vyřazeno {report.rejected_total}")
    for count, label in (
        (report.rejected_known, "dříve vyřazených"),
        (report.duplicates, "duplicit z jiného portálu"),
        (report.already_sent, "už odeslaných"),
    ):
        if count:
            parts.append(f"{label} {count}")
    parts.append(f"chyby {report.errors}")
    if report.skipped:
        parts.append(f"přeskočeno {report.skipped}")
    return _e(" · ".join(parts))


def _details_line(report: RunReport) -> str:
    parts = [f"Detaily: staženo {report.details_fetched}"]
    if report.details_no_text:
        parts.append(f"z toho bez textu {report.details_no_text}")
    if report.details_over_limit:
        parts.append(f"nad limit běhu {report.details_over_limit}")
    below, above = report.hidden_salary_counts()
    if below or above:
        # Jen nabídky v e-mailu; inzeráty s viditelnou mzdou se sem nepočítají.
        parts.append(f"skrytá mzda: {below} pravděpodobně pod minimem, {above} nad minimem")
    return _e(" · ".join(parts))


def _warnings_html(report: RunReport) -> str:
    if not report.warnings:
        return ""
    items = "".join(f'<li style="margin:4px 0">{_e(w)}</li>' for w in report.warnings)
    return (
        '<div style="background:#fdecea;border:2px solid #c62828;color:#8e0000;padding:10px 14px;'
        'margin:12px 0;border-radius:4px;font-size:14px"><strong>Upozornění</strong>'
        f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>'
    )


def save_preview(html_text: str, out_dir: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"email_{now:%Y-%m-%d_%H%M%S}.html"
    path.write_text(html_text, encoding="utf-8")
    return path


def open_in_browser(path: Path) -> None:
    # as_uri() vyrobí správné file:///F:/... i s mezerami a diakritikou v cestě.
    if not webbrowser.open(path.resolve().as_uri()):
        log.info("Prohlížeč se nepodařilo otevřít, náhled je v %s", path)
