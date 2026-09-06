"""SQLite wrapper for job/application/message state. Stdlib sqlite3 only —
no ORM, keeps the audit trail easy to inspect directly with the sqlite3 CLI.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    url TEXT,
    description TEXT,
    fit_score INTEGER,
    reason TEXT,
    recommend_apply INTEGER,
    applied INTEGER DEFAULT 0,
    applied_at TEXT,
    dry_run INTEGER,
    scraped_at TEXT,
    external_apply_url TEXT,
    apply_outcome TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    job_id TEXT,
    sender TEXT,
    received_at TEXT,
    body TEXT,
    draft_reply TEXT,
    sent INTEGER DEFAULT 0,
    sent_at TEXT
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS doesn't add columns to an existing
        # table (e.g. an already-populated jobs.db from before this column
        # existed) — migrate it in by hand, guarded by a presence check
        # since SQLite has no "ADD COLUMN IF NOT EXISTS".
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "external_apply_url" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN external_apply_url TEXT")
        if "apply_outcome" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN apply_outcome TEXT")


def upsert_job(job: dict):
    """job must contain at least job_id, title, company, url. Any of
    description/fit_score/reason/recommend_apply may be added later via
    separate calls (scoring happens after the initial scrape)."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT job_id FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        if existing:
            fields = [k for k in job if k != "job_id"]
            if fields:
                set_clause = ", ".join(f"{f} = ?" for f in fields)
                conn.execute(
                    f"UPDATE jobs SET {set_clause} WHERE job_id = ?",
                    [job[f] for f in fields] + [job["job_id"]],
                )
        else:
            job = {**job, "scraped_at": job.get("scraped_at", datetime.now().isoformat())}
            columns = ", ".join(job.keys())
            placeholders = ", ".join("?" for _ in job)
            conn.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                list(job.values()),
            )


def get_job(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_unscored_jobs() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE fit_score IS NULL").fetchall()
        return [dict(r) for r in rows]


def get_job_ids_with_description() -> set[str]:
    """job_ids that already have a stored, non-empty description. Added
    2026-09-06 so run_search_cycle() can pass this to
    naukri_client.search_jobs_with_details(skip_job_ids=...) and skip
    re-fetching details for a job already known from a prior search."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE description IS NOT NULL AND description != ''"
        ).fetchall()
        return {r["job_id"] for r in rows}


def mark_applied(job_id: str, dry_run: bool):
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET applied = 1, applied_at = ?, dry_run = ? WHERE job_id = ?",
            (datetime.now().isoformat(), int(dry_run), job_id),
        )


def count_applications_today() -> int:
    """Counts only real (non-dry-run) applications. Dry-run attempts are
    still logged via mark_applied() for audit visibility, but must never
    count against config.DAILY_APPLICATION_CAP — otherwise testing in
    dry-run mode would silently exhaust the cap for real applications later
    the same day."""
    today = date.today().isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE applied = 1 AND dry_run = 0 AND applied_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row["n"]


def get_status_summary() -> dict:
    """Read-only aggregate counts for the `status` CLI subcommand — added
    2026-09-06 so "how did the last run go" doesn't require hand-written
    SQL (the gap README's own documented `sqlite3 "select ..."` one-liner
    pointed at). Nothing here writes anything."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        unscored = conn.execute("SELECT COUNT(*) n FROM jobs WHERE fit_score IS NULL").fetchone()["n"]
        recommend_apply_true = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE recommend_apply = 1"
        ).fetchone()["n"]
        applied_real = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE applied = 1 AND dry_run = 0"
        ).fetchone()["n"]
        applied_dry_run = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE applied = 1 AND dry_run = 1"
        ).fetchone()["n"]
        outcome_rows = conn.execute(
            "SELECT apply_outcome, COUNT(*) AS n FROM jobs WHERE apply_outcome IS NOT NULL "
            "GROUP BY apply_outcome ORDER BY n DESC"
        ).fetchall()

    return {
        "total_jobs": total,
        "unscored": unscored,
        "scored": total - unscored,
        "recommend_apply_true": recommend_apply_true,
        "applied_real": applied_real,
        "applied_dry_run": applied_dry_run,
        "applied_today": count_applications_today(),
        "daily_cap": config.DAILY_APPLICATION_CAP,
        "apply_outcomes": {r["apply_outcome"]: r["n"] for r in outcome_rows},
    }


def get_applicable_jobs(min_fit_score: int) -> list[dict]:
    """Jobs scored at or above the threshold, not yet *really* applied to.
    Ordered by fit_score descending so the best matches are attempted first
    if the daily cap is hit partway through.

    `applied = 0 OR dry_run = 1`, not just `applied = 0`: a dry-run "apply"
    still sets applied=1 for audit visibility (see mark_applied), but must
    not remove the job from future consideration — otherwise running a dry
    run once would mark every candidate applied and a later real run would
    see nothing left to apply to."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE fit_score >= ? AND (applied = 0 OR dry_run = 1) "
            "ORDER BY fit_score DESC",
            (min_fit_score,),
        ).fetchall()
        return [dict(r) for r in rows]


def insert_message(message: dict):
    """message must contain at least message_id, job_id, sender, body."""
    with _connect() as conn:
        message = {
            **message,
            "received_at": message.get("received_at", datetime.now().isoformat()),
        }
        columns = ", ".join(message.keys())
        placeholders = ", ".join("?" for _ in message)
        conn.execute(
            f"INSERT OR IGNORE INTO messages ({columns}) VALUES ({placeholders})",
            list(message.values()),
        )


def save_draft_reply(message_id: str, draft: str):
    """Stores a drafted reply for human review. Never marks it sent — sending
    is a separate, explicit, manual action (see naukri_client.send_reply)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE messages SET draft_reply = ? WHERE message_id = ?",
            (draft, message_id),
        )
