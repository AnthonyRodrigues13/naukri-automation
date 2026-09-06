"""SQLite wrapper for job/application/message state. Stdlib sqlite3 only —
no ORM, keeps the audit trail easy to inspect directly with the sqlite3 CLI.
"""

import json
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
    apply_outcome TEXT,
    latest_apply_status TEXT,
    naukri_ars_score INTEGER,
    description_embedding TEXT,
    duplicate_of TEXT
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

-- Append-only log of every distinct application-status entry observed on
-- Naukri's own Application History page. Added 2026-09-06 for outcome
-- tracking (see DECISIONS.md and JOB_SEARCH_STRATEGY.md) -- the primary
-- key on (job_id, status_id, status_datetime) makes re-recording an
-- already-seen status a no-op (INSERT OR IGNORE), so repeated checks
-- accumulate only genuinely NEW status transitions, never duplicates.
CREATE TABLE IF NOT EXISTS application_status_history (
    job_id TEXT,
    status_id INTEGER,
    status_value TEXT,
    status_datetime TEXT,
    recorded_at TEXT,
    PRIMARY KEY (job_id, status_id, status_datetime)
);

-- One row per cycle invocation (search/score/apply/check-status), added
-- 2026-09-06 for cadence/staleness tracking in `status` (see DECISIONS.md
-- and JOB_SEARCH_STRATEGY.md roadmap item 5) -- there was no existing
-- timestamp for "when was this cycle last run" (jobs.scraped_at/applied_at
-- are per-job, not per-cycle, and scoring has no timestamp column at all).
CREATE TABLE IF NOT EXISTS run_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle TEXT NOT NULL,
    ran_at TEXT NOT NULL
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
        if "latest_apply_status" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN latest_apply_status TEXT")
        if "naukri_ars_score" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN naukri_ars_score INTEGER")
        if "description_embedding" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN description_embedding TEXT")
        if "duplicate_of" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN duplicate_of TEXT")


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
    """Excludes jobs flagged as a repost (duplicate_of IS NOT NULL) --
    added 2026-09-06, roadmap item 7: a repost never gets its own
    independent fit_score, so it must never reach run_scoring_cycle()'s
    LLM-scoring loop in the first place. See scoring.find_duplicate_job()
    and get_original_job_embeddings()."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE fit_score IS NULL AND duplicate_of IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]


def get_original_job_embeddings() -> list[dict]:
    """Candidate pool for repost/duplicate detection (see
    scoring.find_duplicate_job()) -- added 2026-09-06, roadmap item 7. Only
    jobs with a stored description_embedding AND duplicate_of IS NULL
    (i.e. not themselves already flagged as a repost of something else)
    are eligible originals, so a chain of reposts always resolves back to
    one true original rather than drifting to whichever repost happened
    to be checked most recently."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id, description_embedding FROM jobs "
            "WHERE description_embedding IS NOT NULL AND duplicate_of IS NULL"
        ).fetchall()
        return [{"job_id": r["job_id"], "embedding": json.loads(r["description_embedding"])} for r in rows]


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


def record_application_status(job_id: str, ars_score, statuses: list[dict]):
    """Outcome tracking, added 2026-09-06 (see DECISIONS.md and
    JOB_SEARCH_STRATEGY.md) -- records Naukri's own view of what happened
    to a real application after it was submitted. `statuses` is
    [{"status_id", "status_value", "status_datetime"}, ...] as returned by
    naukri_client.get_application_status_history(). Every entry is
    appended to application_status_history (INSERT OR IGNORE on the
    (job_id, status_id, status_datetime) primary key, so re-recording an
    already-seen status on a later check is a no-op, not a duplicate row).
    jobs.latest_apply_status and jobs.naukri_ars_score are also updated to
    the most recent values, so the fit_score-vs-outcome correlation this
    capability exists to enable (see get_outcome_correlation) is a plain
    query, not a join across every historical status row. Does nothing if
    `statuses` is empty (a job with no recorded status yet)."""
    if not statuses:
        return
    with _connect() as conn:
        now = datetime.now().isoformat()
        for s in statuses:
            conn.execute(
                "INSERT OR IGNORE INTO application_status_history "
                "(job_id, status_id, status_value, status_datetime, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, s["status_id"], s["status_value"], s["status_datetime"], now),
            )
        latest = max(statuses, key=lambda s: s["status_datetime"])
        conn.execute(
            "UPDATE jobs SET latest_apply_status = ?, naukri_ars_score = ? WHERE job_id = ?",
            (latest["status_value"], ars_score, job_id),
        )


def get_outcome_correlation() -> list[dict]:
    """fit_score vs. Naukri's own latest_apply_status/naukri_ars_score for
    every job with a recorded status -- the actual data outcome tracking
    exists to produce (see JOB_SEARCH_STRATEGY.md's automation roadmap,
    item 1: this is what would let the 70/100 fit_score threshold be
    measured against real recruiter response instead of assumed).
    Added 2026-09-06."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id, title, company, fit_score, naukri_ars_score, latest_apply_status "
            "FROM jobs WHERE latest_apply_status IS NOT NULL ORDER BY fit_score DESC"
        ).fetchall()
        return [dict(r) for r in rows]


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
        # Excludes reposts (duplicate_of IS NOT NULL) -- they never get
        # scored (see get_unscored_jobs()), so counting them here would
        # make "unscored" grow forever even though nothing is actually
        # waiting to be scored. Reported separately below instead.
        unscored = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE fit_score IS NULL AND duplicate_of IS NULL"
        ).fetchone()["n"]
        # Computed directly (not total - unscored) so it stays correct now
        # that duplicates are excluded from unscored but not from total --
        # added 2026-09-06, roadmap item 7.
        scored = conn.execute("SELECT COUNT(*) n FROM jobs WHERE fit_score IS NOT NULL").fetchone()["n"]
        duplicates_detected = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE duplicate_of IS NOT NULL"
        ).fetchone()["n"]
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
        "scored": scored,
        "duplicates_detected": duplicates_detected,
        "recommend_apply_true": recommend_apply_true,
        "applied_real": applied_real,
        "applied_dry_run": applied_dry_run,
        "applied_today": count_applications_today(),
        "daily_cap": config.DAILY_APPLICATION_CAP,
        "apply_outcomes": {r["apply_outcome"]: r["n"] for r in outcome_rows},
        "last_run_at": get_last_run_times(),
    }


def record_run(cycle: str):
    """Appends one row to run_history recording that `cycle` (e.g.
    "search", "score", "apply", "check-status") ran now. Added 2026-09-06
    for cadence/staleness tracking — see get_last_run_times() and
    get_status_summary()."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO run_history (cycle, ran_at) VALUES (?, ?)",
            (cycle, datetime.now().isoformat()),
        )


def get_last_run_times() -> dict:
    """Most recent ran_at timestamp per cycle name, e.g.
    {"search": "2026-09-06T...", "score": "..."}. A cycle never run yet is
    simply absent from the returned dict. Added 2026-09-06."""
    with _connect() as conn:
        rows = conn.execute("SELECT cycle, MAX(ran_at) AS last_ran FROM run_history GROUP BY cycle").fetchall()
        return {r["cycle"]: r["last_ran"] for r in rows}


def get_applicable_jobs(min_fit_score: int) -> list[dict]:
    """Jobs scored at or above the threshold, not yet *really* applied to.
    Ordered by fit_score descending so the best matches are attempted first
    if the daily cap is hit partway through.

    `applied = 0 OR dry_run = 1`, not just `applied = 0`: a dry-run "apply"
    still sets applied=1 for audit visibility (see mark_applied), but must
    not remove the job from future consideration — otherwise running a dry
    run once would mark every candidate applied and a later real run would
    see nothing left to apply to.

    `duplicate_of IS NULL` added 2026-09-06 (roadmap item 7) as defense in
    depth: a repost should never have a fit_score in the first place (see
    get_unscored_jobs()), but this guards against ever applying to one even
    if that invariant is somehow violated."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE fit_score >= ? AND (applied = 0 OR dry_run = 1) "
            "AND duplicate_of IS NULL ORDER BY fit_score DESC",
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
