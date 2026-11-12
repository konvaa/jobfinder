"""Vykreslení e-mailu ze šablon a náhled pro `--dry-run`.

Šablony (viz templating.py: výchozí v programu, upravené v templates/ vedle
něj) používají string.Template ze standardní knihovny ($pozice, $firma …). Proč ne Jinja2: další závislost,
a hlavně v šabloně nejde napsat kód, takže úprava od neprogramátora nic
nerozbije. Neznámá proměnná zůstane v e-mailu vidět jako „$preklep“
(safe_substitute), takže se chyba dá najít okem a program kvůli ní nespadne.

Proč víc souborů (email, nabidka, ostatni, vyrazeno): string.Template neumí
cykly, proto se řádek vykreslí zvlášť pro každý inzerát a výsledek se vloží
do kostry.

Sekce „Zajímavé podle názvu“ má nabídky bez textu detailu (kariérní weby
s JavaScriptem apod.) s kladnými body v názvu, řazené podle bodů v názvu.
Jejich celkové skóre se s ostatními nedá srovnat, proto vlastní sekce.

Sekce „Ostatní“ (pod hranicí skóre + bez popisu bez bodů v názvu) má úsporné
jednořádkové záznamy, ne celou tabulku. V těle e-mailu je to obyčejný nadpis
s řádky: webmail Seznamu při čištění HTML odstraní <details> i S OBSAHEM
(ověřeno 9. 11. 2026), jiní klienti značku ignorují nebo ji sbalí. Sbalitelné
<details> dostane jen příloha (collapsible=True), která se otevírá v prohlížeči.

Textová verze (email.txt) vzniká ze stejných dat jako HTML. Proč
obě: klienti bez HTML ji zobrazí a spamové filtry hodnotí zprávu jen s HTML
hůř. Odesílání samo je v mailer.py.

Velikost: Gmail zkracuje HTML nad zhruba 100 kB („Zpráva byla zkrácena“).
Když je souhrn větší (typicky první ostrý běh se stovkami nabídek), jde celý
jako příloha a tělo má hlavní tabulku, „podle názvu“ a prvních
BODY_OTHER_ROWS „ostatních“ (viz build_bodies).
"""

from __future__ import annotations

import html
import logging
import re
import webbrowser
from datetime import datetime
from pathlib import Path

from jobagent import collectors
from jobagent.models import Job
from jobagent.pipeline import RunReport
from jobagent.templating import TEMPLATE_FILES, Templates, TemplateError  # noqa: F401 (re-export)

log = logging.getLogger(__name__)

# Nad tuto velikost HTML jde celý souhrn jako příloha (Gmail zkracuje kolem 102 kB).
BODY_LIMIT_BYTES = 100_000
BODY_OTHER_ROWS = 20
ATTACHMENT_NAME = "job-agent-souhrn.html"


def _e(value: object) -> str:
    """HTML escape. Text inzerátu přichází z cizího webu a nesmí rozbít (ani zneužít) HTML e-mailu."""
    return html.escape(str(value), quote=True)


def split_by_threshold(jobs: list[Job], threshold: int | None) -> tuple[list[Job], list[Job]]:
    if threshold is None:
        return jobs, []
    return [j for j in jobs if j.score >= threshold], [j for j in jobs if j.score < threshold]


def render(report: RunReport, templates: Templates | None = None, now: datetime | None = None, threshold: int | None = None,
           others_limit: int | None = None, collapsible: bool = False) -> str:
    """HTML souhrnu. others_limit: kolik „ostatních“ ukázat (zbytek je v příloze).

    collapsible=True zabalí „Ostatní“ do <details> – jen pro přílohu a náhled
    v prohlížeči. Výchozí False je bezpečný pro tělo e-mailu (viz hlavička modulu).
    """
    now = now or datetime.now()
    templates = templates or Templates()
    page = templates.get("email.html")
    row = templates.get("nabidka.html")
    other_row = templates.get("ostatni.html")
    rejected_row = templates.get("vyrazeno.html")

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
        shown_others = others if others_limit is None else others[:others_limit]
        other_rows = "\n".join(other_row.safe_substitute(_job_values(job)) for job in shown_others)
        rest = len(others) - len(shown_others)
        more = (f'<p style="margin:8px 0 0;color:#555;font-size:13px">… a dalších {rest} v příloze '
                f"{_e(ATTACHMENT_NAME)}.</p>") if rest else ""
        heading = f"Ostatní nabídky ({len(others)})" + (
            f" – skóre pod {threshold} nebo bez popisu" if threshold is not None else " – bez popisu")
        body = f'<div style="margin-top:8px">{other_rows}</div>{more}'
        if collapsible:
            others_html = (
                '<details style="margin:24px 0 0"><summary style="cursor:pointer;font-size:15px;'
                f'font-weight:bold;color:#333">{_e(heading)}</summary>{body}</details>'
            )
        else:
            others_html = f'<h3 style="margin:28px 0 4px;font-size:15px;color:#333">{_e(heading)}</h3>{body}'


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

    attachment_note = ""
    if others_limit is not None and len(others) > others_limit:
        attachment_note = (
            '<p style="margin:8px 0;padding:8px 12px;background:#e3f2fd;border-radius:4px;font-size:14px">'
            f"Souhrn je dnes dlouhý, celý je v příloze <strong>{_e(ATTACHMENT_NAME)}</strong> "
            "(otevře se v prohlížeči). Tady je hlavní tabulka, nabídky podle názvu a "
            f"prvních {others_limit} ostatních.</p>"
        )

    return page.safe_substitute(
        datum=_e(now.strftime("%d. %m. %Y %H:%M")),
        stav=_e(_status_line(report, len(main), len(others))),
        detaily=_e(_details_line(report)),
        priloha=attachment_note,
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
    """Stavový řádek (čistý text; HTML ho escapuje), sečte se beze zbytku (viz pipeline.py).

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
    return " · ".join(parts)


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
    return " · ".join(parts)


def _warnings_html(report: RunReport) -> str:
    if not report.warnings:
        return ""
    items = "".join(f'<li style="margin:4px 0">{_e(w)}</li>' for w in report.warnings)
    return (
        '<div style="background:#fdecea;border:2px solid #c62828;color:#8e0000;padding:10px 14px;'
        'margin:12px 0;border-radius:4px;font-size:14px"><strong>Upozornění</strong>'
        f'<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></div>'
    )


def _count_cz(n: int, one: str, few: str, many: str) -> str:
    """Česká čísla: 1 nová, 2–4 nové, 0 a 5+ nových."""
    return f"{n} {one if n == 1 else few if 2 <= n <= 4 else many}"


def subject(report: RunReport, prefix: str, threshold: int | None = None) -> str:
    """Předmět se souhrnem. „⚠“ na začátku, když některý portál selhal nebo se přeskočil.

    Proč počty v předmětu: v seznamu zpráv je vidět, jestli stojí za to e-mail
    otevřít, a „dnes nic nového“ potvrzuje, že program běžel.
    """
    prefix = (prefix or "Job Agent").strip()
    if report.shown_total:
        main, _ = split_by_threshold(report.new_jobs, threshold)
        text = f"{prefix}: {_count_cz(report.shown_total, 'nová', 'nové', 'nových')} ({len(main)} v hlavní tabulce)"
    else:
        text = f"{prefix}: dnes nic nového"
    failed = sorted({s.portal or s.name for s in report.stats if s.error or s.skipped})
    if failed:
        text = f"⚠ {text} – chyba: {', '.join(failed)}"
    return text


def render_text(report: RunReport, templates: Templates | None = None, now: datetime | None = None, threshold: int | None = None,
                others_limit: int | None = None) -> str:
    """Čistě textová verze stejného souhrnu (šablona email.txt)."""
    now = now or datetime.now()
    templates = templates or Templates()
    page = templates.get("email.txt")
    main, others = split_by_threshold(report.new_jobs, threshold)
    others = others + report.no_text_other

    def full(job: Job, score: int | None = None) -> str:
        v = _job_plain(job, score)
        lines = [f"* {v['pozice']}", f"  {v['firma']} · {v['misto']} · {v['mzda']} · skóre {v['skore']} · {v['portal']}",
                 f"  Proč: {v['zduvodneni']}"]
        if v["poznamka"]:
            lines.append(f"  Pozn.: {v['poznamka']}")
        lines.append(f"  {v['odkaz']}")
        return "\n".join(lines)

    def compact(job: Job) -> str:
        v = _job_plain(job)
        return f"* [{v['skore']}] {v['pozice']} – {v['firma']}, {v['misto']}\n  {v['odkaz']}"

    if main:
        rows = "\n\n".join(full(j) for j in main)
    elif others or report.title_jobs:
        rows = "V hlavní tabulce dnes nic. Podívej se do sekcí níže."
    else:
        rows = "Dnes nic nového. Program proběhl v pořádku."
    title = ""
    if report.title_jobs:
        title = (f"ZAJÍMAVÉ PODLE NÁZVU – BEZ POPISU, OTEVŘI RUČNĚ ({len(report.title_jobs)})\n"
                 "Skóre = jen body za slova v názvu.\n\n"
                 + "\n\n".join(full(j, score=j.title_score) for j in report.title_jobs))
    other_text = ""
    if others:
        shown = others if others_limit is None else others[:others_limit]
        other_text = f"OSTATNÍ NABÍDKY ({len(others)})\n\n" + "\n".join(compact(j) for j in shown)
        if len(shown) < len(others):
            other_text += f"\n… a dalších {len(others) - len(shown)} v příloze {ATTACHMENT_NAME}."
    rejected = ""
    if report.rejected:
        more = f" (zobrazeno {len(report.rejected)} z {report.rejected_total})" if report.rejected_total > len(report.rejected) else ""
        rejected = f"VYŘAZENÉ INZERÁTY{more}\n\n" + "\n".join(
            f"* {j.title} – {j.company or '—'}: {j.reject_reason or ''}" for j in report.rejected)
    warnings = ""
    if report.warnings:
        warnings = "UPOZORNĚNÍ\n" + "\n".join(f"! {w}" for w in report.warnings)
    note = ""
    if others_limit is not None and len(others) > others_limit:
        note = f"Souhrn je dnes dlouhý, celý je v příloze {ATTACHMENT_NAME} (otevře se v prohlížeči)."
    text = page.safe_substitute(
        datum=now.strftime("%d. %m. %Y %H:%M"),
        rezim="NÁHLED (dry-run) – nic nebylo odesláno" if report.dry_run else "",
        stav=_status_line(report, len(main), len(others)),
        detaily=_details_line(report),
        varovani=warnings,
        priloha=note,
        pocet_novych=str(len(main)),
        nabidky=rows,
        podle_nazvu=title,
        ostatni=other_text,
        vyrazene=rejected,
        hodnoceni=report.scoring_method,
    )
    # Prázdné sekce nechají v šabloně prázdné řádky; víc než jeden za sebou je zbytečný.
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def build_bodies(report: RunReport, templates: Templates | None = None, now: datetime | None = None,
                 threshold: int | None = None) -> tuple[str, str, str | None]:
    """(HTML těla, text těla, HTML přílohy nebo None) pro odeslání.

    Celý souhrn se vykreslí vždy. Když přesáhne BODY_LIMIT_BYTES, tělo se
    zkrátí na prvních BODY_OTHER_ROWS „ostatních“ a celý souhrn jde jako příloha.
    """
    full_body = render(report, templates, now, threshold)
    if len(full_body.encode("utf-8")) <= BODY_LIMIT_BYTES:
        return full_body, render_text(report, templates, now, threshold), None
    return (render(report, templates, now, threshold, others_limit=BODY_OTHER_ROWS),
            render_text(report, templates, now, threshold, others_limit=BODY_OTHER_ROWS),
            # Příloha se otevírá v prohlížeči, tam <details> funguje a dlouhý seznam sbalí.
            render(report, templates, now, threshold, collapsible=True))


def _job_plain(job: Job, score: int | None = None) -> dict[str, str]:
    return {
        "pozice": job.title,
        "firma": job.company or "—",
        "misto": job.location or "—",
        "mzda": salary_label(job),
        "skore": str(job.score if score is None else score),
        "zduvodneni": ", ".join(job.score_reasons) or "žádné klíčové slovo",
        "poznamka": job.detail_note,
        "odkaz": job.url,
        "portal": " + ".join([job.portal, *job.also_on]),
    }


def open_in_browser(path: Path) -> None:
    # as_uri() vyrobí správné file:///F:/... i s mezerami a diakritikou v cestě.
    if not webbrowser.open(path.resolve().as_uri()):
        log.info("Prohlížeč se nepodařilo otevřít, náhled je v %s", path)
