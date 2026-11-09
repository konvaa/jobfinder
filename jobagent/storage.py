"""SQLite: paměť mezi běhy.

Co si pamatujeme a proč:
  * jobs – každý viděný inzerát včetně staženého detailu. Detail se tak
    stahuje jen jednou, ne každý den znovu (šetří portál i čas běhu),
  * sent_at – kdy šel inzerát v e-mailu jako nabídka. Podle toho poznáme
    „nové“: nové = ještě neodeslané, ne „poprvé viděné“. Díky tomu
    `--dry-run` nic „nespotřebuje“ a ostrý běh pošle všechno, co ještě nepřišlo,
  * rejected_reported_at – vyřazené inzeráty ukážeme na konci e-mailu jen
    jednou. Při dalších bězích se ale vyhodnocují znovu, takže po úpravě
    filtrů se mezi nabídky můžou vrátit,
  * search_stats – kolik inzerátů vrátilo každé hledání. Z toho poznáme,
    že portál, který dřív vracel výsledky, najednou vrací 0 (rozbitý parser).

Verze schématu se drží v PRAGMA user_version. Proč: kamarádi budou mít DB
ze starší verze programu a nová verze ji musí umět sama doplnit (migrace),
ne spadnout.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from jobagent.models import HiddenSalary, Job

NO_TEXT_NOTE = "detail bez textu (stránka se načítá JavaScriptem) – hodnoceno jen podle výpisu"
FAILED_NOTE = "detail se opakovaně nepodařilo stáhnout – hodnoceno jen podle výpisu"

SCHEMA_VERSION = 7

_SCHEMA_V1 = """
CREATE TABLE jobs (
    key                  TEXT PRIMARY KEY,
    portal               TEXT NOT NULL,
    native_id            TEXT NOT NULL,
    url                  TEXT NOT NULL,
    title                TEXT NOT NULL,
    company              TEXT NOT NULL DEFAULT '',
    location             TEXT NOT NULL DEFAULT '',
    salary_text          TEXT NOT NULL DEFAULT '',
    posted               TEXT,
    tags                 TEXT NOT NULL DEFAULT '[]',
    description          TEXT NOT NULL DEFAULT '',
    detail_fetched       INTEGER NOT NULL DEFAULT 0,
    dedup_key            TEXT NOT NULL DEFAULT '',
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    score                INTEGER,
    reject_reason        TEXT,
    sent_at              TEXT,
    rejected_reported_at TEXT
);
CREATE INDEX idx_jobs_dedup ON jobs(dedup_key);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    dry_run     INTEGER NOT NULL,
    summary     TEXT
);

CREATE TABLE search_stats (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    search_name TEXT NOT NULL,
    url         TEXT NOT NULL,
    portal      TEXT,
    found       INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX idx_stats_url ON search_stats(url);
"""

# v2: detail_status rozliší „detail stažen, ale stránka text nemá“ (JS stránky)
# od běžného detailu. Bez toho by se takové stránky stahovaly každý den znovu.
_MIGRATE_V2 = ("detail_status", "ALTER TABLE jobs ADD COLUMN detail_status TEXT")

# v3: neúspěšné pokusy o detail napříč běhy. Po MAX pokusech (každý v jiném
# běhu) se inzerát přestane odkládat a ukáže se „podle názvu“.
_MIGRATE_V3 = ("detail_attempts", "ALTER TABLE jobs ADD COLUMN detail_attempts INTEGER NOT NULL DEFAULT 0")

# v4: detaily uložené před verzí 2 (detail_status IS NULL) vznikly bez kontroly
# patiček, kostry JS stránek a šablonových bloků. Smažou se a stáhnou znovu.
# Nejde o změnu schématu, jen o jednorázové přečištění dat.
_MIGRATE_V4 = """UPDATE jobs SET detail_fetched=0, description=''
                 WHERE detail_fetched=1 AND detail_status IS NULL"""

# v5: identita napříč portály (ID jobs.cz u inzerátů z prace.cz). Podle ní se
# pozná, že inzerát už odešel z jiného portálu, i v dalších bězích.
_MIGRATE_V5 = ("alt_key", "ALTER TABLE jobs ADD COLUMN alt_key TEXT NOT NULL DEFAULT ''")
_MIGRATE_V5_INDEX = "CREATE INDEX IF NOT EXISTS idx_jobs_alt ON jobs(alt_key)"

# v6: skrytá mzda podle filtru mzdy portálu (JSON HiddenSalary, '' = nic).
# Jen kvůli `jobagent vysvetli`: denní běh ji počítá vždy znovu z obou hledání.
_MIGRATE_V6 = ("hidden_salary", "ALTER TABLE jobs ADD COLUMN hidden_salary TEXT NOT NULL DEFAULT ''")

# v7: ostrý běh se počítá (okno prvního běhu hledání, varování o rostoucím
# „čeká na detail“) až po úspěšném odeslání e-mailu. Když se e-mail nepošle,
# další běh se chová, jako by tento neproběhl, a o nic se nepřijde.
_MIGRATE_V7 = ("delivered", "ALTER TABLE runs ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0", "runs")


class Storage:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.conn.executescript(_SCHEMA_V1)
        if version < 2:
            self._add_column(*_MIGRATE_V2)
        if version < 3:
            self._add_column(*_MIGRATE_V3)
        if version < 4:
            self.conn.execute(_MIGRATE_V4)
        if version < 5:
            self._add_column(*_MIGRATE_V5)
            self.conn.execute(_MIGRATE_V5_INDEX)
        if version < 6:
            self._add_column(*_MIGRATE_V6)
        if version < 7:
            self._add_column(*_MIGRATE_V7)
            # Ostrý běh před v7 nešel spustit (odesílání neexistovalo). Kdyby přesto
            # nějaký v DB byl, počítal se jako ostrý: význam zůstane stejný.
            self.conn.execute("UPDATE runs SET delivered=1 WHERE dry_run=0")
        # Další verze: if version < 3: ALTER TABLE ... – vždy jen přidávat, nikdy mazat.
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _add_column(self, name: str, statement: str, table: str = "jobs") -> None:
        """ALTER TABLE jen když sloupec chybí. Proč: přerušená migrace (pád,
        vypnutý počítač) se při dalším spuštění dokončí, místo aby spadla
        na „duplicate column“."""
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.conn.execute(statement)

    # ------------------------------------------------------------ inzeráty

    def merge_seen(self, job: Job, dedup_key: str, now: datetime) -> sqlite3.Row | None:
        """Uloží inzerát z výpisu. Když už ho známe, doplní do `job` uložený detail.

        Data z výpisu (název, mzda) přepisujeme čerstvými, protože portál je
        mohl změnit. Detail necháváme z DB, protože ten se znovu nestahuje.
        Vrací původní řádek (nebo None u úplně nového inzerátu).
        """
        row = self.conn.execute("SELECT * FROM jobs WHERE key = ?", (job.key,)).fetchone()
        stamp = now.isoformat(timespec="seconds")
        if row is None:
            self.conn.execute(
                """INSERT INTO jobs (key, portal, native_id, url, title, company, location, salary_text,
                                     posted, tags, dedup_key, alt_key, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.key, job.portal, job.native_id, job.url, job.title, job.company, job.location,
                    job.salary_text, job.posted.isoformat() if job.posted else None,
                    json.dumps(job.tags, ensure_ascii=False), dedup_key, job.alt_key, stamp, stamp,
                ),
            )
            return None

        if row["detail_fetched"]:
            job.description = row["description"]
            job.detail_fetched = True
            if row["detail_status"] == "bez-textu":
                job.detail_note = NO_TEXT_NOTE
            elif row["detail_status"] == "selhalo":
                job.detail_note = FAILED_NOTE
            # Mzdu a firmu mohl doplnit až detail; prázdná hodnota z výpisu je nesmí smazat.
            job.salary_text = job.salary_text or row["salary_text"]
            job.company = job.company or row["company"]
        if job.posted is None and row["posted"]:
            job.posted = date.fromisoformat(row["posted"])
        self.conn.execute(
            """UPDATE jobs SET url=?, title=?, company=?, location=?, salary_text=?, posted=?, tags=?,
                               dedup_key=?, alt_key=?, last_seen=? WHERE key=?""",
            (
                job.url, job.title, job.company, job.location or row["location"], job.salary_text,
                job.posted.isoformat() if job.posted else None,
                json.dumps(job.tags, ensure_ascii=False), dedup_key, job.alt_key or row["alt_key"], stamp, job.key,
            ),
        )
        return row

    def save_detail(self, job: Job, status: str) -> None:
        self.conn.execute(
            """UPDATE jobs SET description=?, detail_fetched=1, detail_status=?, salary_text=?, company=?,
                               location=?, posted=? WHERE key=?""",
            (
                job.description, status, job.salary_text, job.company, job.location,
                job.posted.isoformat() if job.posted else None, job.key,
            ),
        )

    def record_detail_failure(self, job: Job, max_attempts: int) -> int:
        """Zapíše neúspěšný pokus o detail. Vrací počet pokusů včetně tohoto.

        Pipeline zkouší detail každého inzerátu nejvýš jednou za běh, takže
        počet pokusů = počet běhů, ve kterých stažení selhalo. Po dosažení
        max_attempts se detail bere jako trvale nedostupný (už se nezkouší).
        """
        self.conn.execute("UPDATE jobs SET detail_attempts = detail_attempts + 1 WHERE key=?", (job.key,))
        attempts = self.conn.execute("SELECT detail_attempts FROM jobs WHERE key=?", (job.key,)).fetchone()[0]
        if attempts >= max_attempts:
            self.conn.execute(
                "UPDATE jobs SET detail_fetched=1, detail_status='selhalo' WHERE key=?", (job.key,)
            )
        return attempts

    def find_listing(self, portal: str | None, native_id: str | None, url: str) -> Job | None:
        """Inzerát z výpisu (pro `jobagent vysvetli`): název, štítky, mzda…

        Hledá podle ID portálu (stejný inzerát má víc podob URL), když ID
        neznáme, tak podle URL.
        """
        row = None
        if portal and native_id:
            row = self.conn.execute("SELECT * FROM jobs WHERE key=?", (f"{portal}:{native_id}",)).fetchone()
        if row is None:
            row = self.conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()
        if row is None:
            return None
        return Job(
            portal=row["portal"], native_id=row["native_id"], url=row["url"], title=row["title"],
            company=row["company"], location=row["location"], salary_text=row["salary_text"],
            posted=date.fromisoformat(row["posted"]) if row["posted"] else None,
            tags=json.loads(row["tags"]),
            hidden_salary=HiddenSalary.from_json(row["hidden_salary"]),
        )

    def save_evaluation(self, job: Job) -> None:
        self.conn.execute(
            "UPDATE jobs SET score=?, reject_reason=?, hidden_salary=? WHERE key=?",
            (job.score, job.reject_reason, job.hidden_salary.to_json() if job.hidden_salary else "", job.key),
        )

    def is_sent(self, key: str) -> bool:
        row = self.conn.execute("SELECT sent_at FROM jobs WHERE key=?", (key,)).fetchone()
        return bool(row and row["sent_at"])

    def rejection_reported(self, key: str) -> bool:
        row = self.conn.execute("SELECT rejected_reported_at FROM jobs WHERE key=?", (key,)).fetchone()
        return bool(row and row["rejected_reported_at"])

    def identity_sent(self, identity: str, own_key: str) -> bool:
        """Odešel už stejný inzerát (stejné ID napříč portály) pod jiným klíčem?

        Např. inzerát z prace.cz s ID jobs.cz, který dřív odešel z jobs.cz, nebo naopak.
        """
        row = self.conn.execute(
            """SELECT 1 FROM jobs WHERE key<>? AND sent_at IS NOT NULL
               AND (key=? OR (alt_key<>'' AND alt_key=?)) LIMIT 1""",
            (own_key, identity, identity),
        ).fetchone()
        return row is not None

    def dedup_sent_elsewhere(self, dedup_key: str, own_key: str) -> str | None:
        """Byl už stejný inzerát (jiné ID, třeba z jiného portálu) odeslán? Vrátí jeho klíč."""
        if not dedup_key:
            return None
        row = self.conn.execute(
            "SELECT key FROM jobs WHERE dedup_key=? AND key<>? AND sent_at IS NOT NULL LIMIT 1",
            (dedup_key, own_key),
        ).fetchone()
        return row["key"] if row else None

    def mark_sent(self, jobs: list[Job], rejected: list[Job], now: datetime, run_id: int | None = None) -> None:
        """Volá se až PO úspěšném odeslání e-mailu – když odeslání selže, nic se neztratí.

        Inzeráty a příznak doručení běhu se zapíší v jedné transakci: buď obojí,
        nebo nic. (Pád mezi odesláním a tímto zápisem znamená duplicitu v dalším
        e-mailu, nikdy ztracený inzerát – vědomě zvolené pořadí.)
        """
        stamp = now.isoformat(timespec="seconds")
        self.conn.executemany("UPDATE jobs SET sent_at=? WHERE key=?", [(stamp, j.key) for j in jobs])
        self.conn.executemany(
            "UPDATE jobs SET rejected_reported_at=? WHERE key=?", [(stamp, j.key) for j in rejected]
        )
        if run_id is not None:
            self.conn.execute("UPDATE runs SET delivered=1 WHERE id=?", (run_id,))
        self.conn.commit()

    # ------------------------------------------------------------ běhy a statistiky

    def start_run(self, now: datetime, dry_run: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, dry_run) VALUES (?, ?)",
            (now.isoformat(timespec="seconds"), int(dry_run)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: datetime, summary: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, summary=? WHERE id=?",
            (now.isoformat(timespec="seconds"), json.dumps(summary, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def record_search(self, run_id: int, name: str, url: str, portal: str | None, found: int, error: str | None) -> None:
        self.conn.execute(
            "INSERT INTO search_stats (run_id, search_name, url, portal, found, error) VALUES (?,?,?,?,?,?)",
            (run_id, name, url, portal, found, error),
        )

    def had_live_run(self, url: str, exclude_run: int) -> bool:
        """Mělo tohle hledání už úspěšný ostrý běh? Dry-run ani neodeslaný e-mail se nepočítá.

        Klíčem je URL: kamarád může hledání přejmenovat, ale nová URL je
        z pohledu portálu jiné hledání se svými staršími inzeráty.
        """
        row = self.conn.execute(
            """SELECT 1 FROM search_stats s JOIN runs r ON r.id = s.run_id
               WHERE s.url=? AND s.run_id<>? AND r.dry_run=0 AND r.delivered=1 AND s.error IS NULL LIMIT 1""",
            (url, exclude_run),
        ).fetchone()
        return row is not None

    def recent_live_summaries(self, exclude_run: int, limit: int) -> list[dict]:
        """Souhrny posledních ostrých běhů (nejnovější první). Dry-runy se nepočítají."""
        rows = self.conn.execute(
            """SELECT summary FROM runs WHERE dry_run=0 AND delivered=1 AND id<>? AND summary IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (exclude_run, limit),
        ).fetchall()
        return [json.loads(r["summary"]) for r in rows]

    def recent_max_found(self, url: str, exclude_run: int, last_runs: int = 5) -> int | None:
        """Nejvyšší počet nalezených inzerátů za posledních N běhů tohoto hledání (bez běhů s chybou)."""
        row = self.conn.execute(
            """SELECT MAX(found) AS m FROM (
                   SELECT found FROM search_stats
                   WHERE url=? AND run_id<>? AND error IS NULL
                   ORDER BY run_id DESC LIMIT ?)""",
            (url, exclude_run, last_runs),
        ).fetchone()
        return row["m"] if row else None

    def commit(self) -> None:
        self.conn.commit()
