# Job Agent

A small personal tool that checks Czech job portals once a day, drops
unsuitable offers, scores the rest by your own keywords and prepares a summary
e-mail.

*Česky: [README.cs.md](README.cs.md).*

> **Status:** version 0.2. Collecting, filtering, scoring, the e-mail summary,
> the daily run via Task Scheduler and a standalone Windows program work.
> Optional AI scoring (phase 5) is not done yet.
>
> **Download:** the ready-made Windows program is in
> [Releases](https://github.com/konvaa/jobfinder/releases); the step-by-step guide
> (Czech) is [docs/NAVOD.md](docs/NAVOD.md), also included in the zip as `NAVOD.txt`.
> The configuration and all messages are in Czech, because the tool is meant
> for Czech job portals and Czech-speaking users.

## What it does

1. Walks through the searches you defined. A search is simply a URL copied
   from the portal after you set your filters there (place, radius, field).
2. Downloads the listing pages and, within a per-run limit, the job details.
3. Filters: maximum age, minimum salary (an offer **without** a stated salary is
   never rejected), words that reject an offer, words that reject it only in
   the title.
4. Scores offers by your keywords (± points, synonym groups, wildcards,
   title-only words) and explains every point ("found X in the title").
5. Merges duplicates across portals (by portal ID, or by title + company +
   town when an ID is missing; when unsure, it does not merge).
6. Remembers offers in SQLite (seen, rejected, downloaded details). Offers are
   marked as sent only after a successful send, so you get only new ones and
   nothing is lost when sending fails.
7. Renders an HTML summary: main table, "interesting by title only", others,
   and rejected offers with reasons.

Supported portals: **jobs.cz**, **prace.cz**, **jobscontact.cz**. Each portal is
one module in `jobagent/collectors/`.

## Rules it follows

- **Personal use.** It is a tool for one person looking for a job, not a crawler
  or a data collection service. Do not use it to republish job offers.
- **robots.txt** is checked for every site (RFC 9309). If robots.txt cannot be
  loaded because of a server or network error, the site is skipped for that run.
- **Polite pacing:** 1–3 s between requests, a limit on pages and detail
  requests per run, a timeout, and an honest User-Agent with a link to this
  repository.
- **Run it at most once a day.**
- No logins and no private APIs. Pages that load the job text only via
  JavaScript from a private API are kept with the listing data only.
- This project is not affiliated with any of the portals.

## Requirements

- Windows (other systems should work for development, but the scheduling and
  the planned `.exe` target Windows)
- Python 3.12
- `beautifulsoup4` (pinned in `requirements.txt` with its dependencies)

## Setup

```bat
git clone https://github.com/konvaa/jobfinder.git
cd jobfinder

py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt

copy config.example.toml config.toml
python -m jobagent nastav-heslo
```

Then edit `config.toml` in any text editor. It is heavily commented (in Czech):
your searches, filters, keywords and points, the e-mail summary and the
optional AI. Passwords do not belong there. The config is not tracked by git.

E-mail: fill in the server, `uzivatel` (the full address) and `prijemci` in
`[email]`. Store the password with `nastav-heslo`: the program first tries to log
in (nothing is sent) and then saves the password encrypted in the **Windows
Credential Manager** (entry `JobAgent/…`; remove it with `smaz-heslo`). Outside
Windows the password goes to `secrets.toml` (see `secrets.example.toml`, not
tracked by git). Seznam with two-factor authentication and Gmail need an **app
password**, not the account password. A dedicated sender mailbox is a good idea.
Check the settings with `test-email`.

The Credential Manager keeps the password out of files that could be shared or
backed up by mistake. It does not protect against malware running under your
own account.

## Building the exe

```bat
py -3.12 tools\build.py
```

One command: checks for uncommitted changes (test build: `--necisty`), creates a
clean `.venv-build` from `requirements-dev.txt` (PyInstaller pinned), compiles the
sources with warnings as errors, runs the tests, builds with `jobagent.spec`,
checks that no personal files ended up in the package, smoke-tests the result and
writes `dist\JobAgent-<version>-windows.zip` (+ `.sha256` and `test-sandbox.wsb`).
The build is **onedir** with two executables sharing `_internal\`: `JobAgent.exe`
(console, manual commands) and `JobAgent-planovac.exe` (no window, for Task
Scheduler).

## Usage

```bat
python -m jobagent run                    :: live run: collect, score, send the summary
python -m jobagent run --dry-run          :: full run without sending; opens the e-mail preview in a browser
python -m jobagent test-email             :: send a short test e-mail (server, password, recipients)
python -m jobagent test-email --nahled    :: resend the latest saved message from vystup/ (subject "TEST – …")
python -m jobagent sablony                :: create templates/ with editable e-mail templates
python -m jobagent nastav-heslo           :: store the e-mail password in the Windows Credential Manager (verifies login)
python -m jobagent smaz-heslo             :: remove it from there
python -m jobagent install-schedule       :: daily run in Windows Task Scheduler (default 7:30; other time: install-schedule 18:05)
python -m jobagent uninstall-schedule     :: remove it
python -m jobagent run --dry-run -v       :: the same with a detailed console log
python -m jobagent vysvetli <url|file>    :: why an offer got its score: text source, removed lines,
                                          :: filters, keywords with place, points and snippet
python -m jobagent sample <url>           :: save a page as HTML to samples/private/ (not tracked by git)
python -m unittest -v                     :: tests (offline)
```

Logs go to `logs/`, the database is `jobagent.db`, previews go to `vystup/`.

Exit codes (shown by Windows Task Scheduler as "Last Run Result"): 0 = OK,
1 = error (e.g. all searches failed), 2 = configuration error, 3 = the e-mail
could not be sent. On 3 nothing is marked as sent, the next run sends the same
offers again, and the unsent summary is saved in `vystup/` (`…_NEODESLANO.html`
and `.eml`).

The `vystup/` folder: every run (including `--dry-run`) saves `email_….html`
(the full summary for a browser) and `email_….eml` (exactly the message that was
or would be sent: body, plain-text version and attachment). Files older than
`[obecne] uchovat_vystup_dni` (default 30, 0 = never) are deleted on the next run.
Only `email_*` files are deleted, nothing else in the folder.

## How it is built

- Python standard library only (`urllib`, `sqlite3`, `smtplib`, `tomllib`)
  plus `beautifulsoup4`.
- Job details are read in three layers: JSON-LD (`schema.org/JobPosting`) →
  portal CSS selectors → the main text of the page, with a check that the text
  belongs to the job.
- Raw text is stored. It is cleaned when used (agency boilerplate, consent
  paragraphs), so better cleaning rules apply to older offers too.

The reasons behind the design decisions are in
[docs/ARCHITEKTURA.md](docs/ARCHITEKTURA.md) (Czech).

## Tests and samples

The tests run offline against saved pages in `samples/`. Real pages are
anonymized: the HTML structure is kept, the job texts, names and contacts are
replaced. See [samples/README.md](samples/README.md).

## License

[MIT](LICENSE) © 2026 Libor Konvičný
