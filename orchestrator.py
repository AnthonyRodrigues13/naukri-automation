"""Ties naukri_client + scoring + storage together. All three cycles —
search, score, apply — are real and wired into main()."""

import argparse
import hashlib
import json
import logging
from datetime import datetime

import config
import excel_log
import naukri_client
import scoring
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("orchestrator")


def run_search_cycle(keywords: str, location: str = ""):
    """Search -> fetch details -> persist, all within one reused browser
    session (see naukri_client.search_jobs_with_details). Replaces the
    former search_jobs() + per-job get_job_details() pattern, which opened
    a fresh browser context and re-checked login before every single job —
    ~10s/job overhead, confirmed live 2026-09-05. See DECISIONS.md.

    Jobs already known from a prior search (a stored, non-empty
    description in jobs.db) have their detail fetch skipped entirely —
    see storage.get_job_ids_with_description() and
    naukri_client.search_jobs_with_details's skip_job_ids param.

    Repost/duplicate detection (added 2026-09-06, roadmap item 7): a
    repost gets a brand-new job_id, so the skip_job_ids check above never
    catches it. Each newly-found job's description embedding is compared
    against every ORIGINAL job already known (see
    storage.get_original_job_embeddings() — never against another
    repost, so chains resolve to one true original) and against
    originals found earlier in this SAME batch, since one batch of
    search results can itself contain more than one repost of the same
    posting. A match sets duplicate_of, excluding it from
    run_scoring_cycle() (see storage.get_unscored_jobs()).

    Also records this (keywords, location) combo's run time (added
    2026-09-06, roadmap item 4) so `search --auto` (see
    run_search_cycle_auto()) can rotate through config.SEARCH_QUERIES by
    least-recently-run — recorded here unconditionally, for every search
    regardless of whether it was typed manually or auto-picked, so the
    rotation state stays accurate either way."""
    storage.record_run("search")
    storage.record_search_run(keywords, location)
    log.info("Searching for %r in %r...", keywords, location or "(any location)")
    already_known = storage.get_job_ids_with_description()
    jobs = naukri_client.search_jobs_with_details(keywords, location, skip_job_ids=already_known)
    log.info("Found %d jobs.", len(jobs))

    candidates = storage.get_original_job_embeddings()

    for job in jobs:
        description = job.get("description") or ""
        if description.strip():
            embedding = scoring.embed_job_description(description)
            if embedding is not None:
                job["description_embedding"] = json.dumps(embedding)
                duplicate_of = scoring.find_duplicate_job(embedding, candidates)
                if duplicate_of:
                    job["duplicate_of"] = duplicate_of
                    log.info(
                        "Job %s (%s) flagged as a repost of %s - excluded from scoring.",
                        job["job_id"],
                        job.get("title", ""),
                        duplicate_of,
                    )
                else:
                    candidates.append({"job_id": job["job_id"], "embedding": embedding})

        storage.upsert_job(job)
        log.info("Saved job %s - %s at %s", job["job_id"], job["title"], job["company"])

    return jobs


def _pick_least_recently_run_query(queries: list, run_times: dict) -> dict:
    """Pure, no I/O. Picks the entry from `queries` whose (keywords,
    location) pair has the oldest recorded run in `run_times` -- an entry
    with NO recorded run at all sorts as older than anything with a real
    timestamp, so a combo that's never been searched is always picked over
    one that has, no matter how long ago. Ties (including multiple
    never-run entries) keep `queries`' original order: Python's min() only
    replaces its current pick on a STRICTLY smaller key, so the first
    tied entry encountered wins -- no explicit tie-break needed. Added
    2026-09-06, JOB_SEARCH_STRATEGY.md roadmap item 4."""
    def sort_key(query):
        ran_at = run_times.get((query["keywords"], query["location"]))
        return (ran_at is not None, ran_at or "")

    return min(queries, key=sort_key)


def run_search_cycle_auto():
    """`search --auto` entry point -- added 2026-09-06, JOB_SEARCH_STRATEGY.md
    roadmap item 4. Picks the least-recently-run role/city combo from
    config.SEARCH_QUERIES (see _pick_least_recently_run_query()) instead of
    keywords/location being typed manually every time, so repeated
    invocations (e.g. a scheduled/cron run) rotate through every real
    target combo automatically rather than only ever hitting whichever one
    a human happened to type most often."""
    if not config.SEARCH_QUERIES:
        log.error("config.SEARCH_QUERIES is empty - nothing to rotate through. Pass keywords/location manually instead.")
        return

    run_times = storage.get_search_run_times()
    query = _pick_least_recently_run_query(config.SEARCH_QUERIES, run_times)
    log.info(
        "search --auto picked %r in %r (least recently run).",
        query["keywords"],
        query["location"] or "(any location)",
    )
    return run_search_cycle(query["keywords"], query["location"])


def run_scoring_cycle():
    """Scores every job in storage that hasn't been scored yet, against the
    resume in config.RESUME_PATH. Does not apply to anything — that's
    run_apply_cycle's job, gated separately by threshold + daily cap."""
    storage.record_run("score")
    resume_profile = scoring.load_resume_profile()
    unscored = storage.get_unscored_jobs()
    log.info("Scoring %d unscored job(s)...", len(unscored))

    if not unscored:
        return

    # Computed ONCE for the whole cycle, not once per job — resume_profile
    # doesn't change across jobs within one cycle, so re-embedding it N
    # times for N jobs was pure waste. See scoring.embed_resume().
    resume_embedding = scoring.embed_resume(resume_profile)
    if resume_embedding is None:
        log.error("Could not embed resume_profile - skipping this scoring cycle entirely.")
        return

    for job in unscored:
        result = scoring.score_job_with_reverification(
            job.get("description") or "", resume_profile, resume_embedding=resume_embedding
        )
        storage.upsert_job(
            {
                "job_id": job["job_id"],
                "fit_score": result["fit_score"],
                "reason": result["reason"],
                "recommend_apply": result["recommend_apply"],
            }
        )
        # result["fit_score"] is None only for a transport failure (see
        # scoring.score_job) — storage.upsert_job just wrote fit_score back
        # to NULL, so this job is still "unscored" and will be retried next
        # cycle, not silently dropped. %d would raise on None, so branch on
        # it explicitly rather than relying on %s to paper over the type.
        if result["fit_score"] is None:
            log.warning(
                "Job %s (%s): scoring failed transiently (%s) - left unscored, will retry next cycle.",
                job["job_id"],
                job.get("title", ""),
                result["reason"],
            )
        else:
            log.info(
                "Scored %s (%s): fit_score=%d recommend_apply=%s",
                job["job_id"],
                job.get("title", ""),
                result["fit_score"],
                result["recommend_apply"],
            )


def _prompt_yes(message: str) -> bool:
    """Blocks on real terminal input. Never called during a dry run (see
    run_apply_cycle) — only when a cycle is genuinely about to submit real
    applications. Deliberately has no bypass flag: a flag to skip this
    would recreate exactly the "ran out of habit, no real signal" gap
    `--live` was already built to close (see DECISIONS.md). Tests patch
    this function directly rather than stdin. Fails closed (aborts) on
    EOFError/KeyboardInterrupt — a non-interactive invocation (e.g. cron)
    with no one to answer must never fall through to "proceed" by default."""
    try:
        response = input(f'{message}\nType "yes" to proceed, anything else to abort: ')
    except (EOFError, KeyboardInterrupt):
        print()
        log.warning("No confirmation received (no interactive input available) - aborting.")
        return False
    return response.strip().lower() == "yes"


def _preflight_summary_and_confirm(candidates: list, applied_today: int) -> bool:
    """Shown once, before a real apply cycle's loop starts (never during a
    dry run) — a human-readable preview of exactly what's about to happen,
    so a real cycle can't proceed on nothing but the LIVE MODE log line. See
    DECISIONS.md."""
    remaining = config.DAILY_APPLICATION_CAP - applied_today
    to_attempt = candidates[:remaining]
    lines = [
        f"=== LIVE APPLY: about to attempt {len(to_attempt)} of {len(candidates)} "
        f"candidate(s) for real ==="
    ]
    for job in to_attempt:
        lines.append(f"  [{job.get('fit_score')}] {job.get('title', '')} @ {job.get('company', '')} ({job['job_id']})")
    if len(candidates) > len(to_attempt):
        lines.append(
            f"  ...and {len(candidates) - len(to_attempt)} more beyond today's "
            f"remaining cap, not attempted this run."
        )
    lines.append(
        f"Daily cap {config.DAILY_APPLICATION_CAP}, already applied today "
        f"{applied_today}, remaining {remaining}."
    )
    print("\n".join(lines))
    return _prompt_yes("Proceed with these real applications?")


def _confirm_after_apply_streak(recent: list, remaining_count: int) -> bool:
    """Circuit breaker checkpoint: config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES
    real applications in a row, no skip/failure in between. `recent` is a
    list of (job_id, title, company) tuples for the streak just completed.
    See DECISIONS.md."""
    lines = [f"=== CIRCUIT BREAKER: {len(recent)} real applications submitted back-to-back ==="]
    for job_id, title, company in recent:
        lines.append(f"  {title} @ {company} ({job_id})")
    lines.append(f"{remaining_count} candidate(s) remain this cycle.")
    print("\n".join(lines))
    return _prompt_yes("Continue applying?")


def run_apply_cycle(live: bool = False):
    """Applies to jobs at or above config.FIT_SCORE_THRESHOLD, capped by
    config.DAILY_APPLICATION_CAP. Cap + pause enforcement happens here in
    code, not just via the LLM's recommend_apply signal — matches the
    project's hard safety rules. The cap is re-checked after every single
    application, not just once at the top, since one run can apply to
    several jobs.

    `live` is True only when the CLI's `apply --live` flag was passed (see
    main()). config.DRY_RUN alone gates real submissions everywhere else in
    the codebase, but it's a file on disk — flip it to False, forget about
    it, and the next `apply` invocation (weeks later, muscle memory typing
    the same command as always) goes live with no signal that anything
    changed. `live` is a second, explicit, per-invocation flag that can't be
    left stale the way a file edit can: going live now requires BOTH
    config.DRY_RUN=False on disk AND --live typed on this specific command
    line. Either one being "safe" keeps the whole run a dry run.

    Two more checkpoints only ever run for a genuinely real cycle (never a
    dry run): _preflight_summary_and_confirm() shows every candidate about
    to be attempted for real and requires a typed "yes" before the loop
    starts at all, and _confirm_after_apply_streak() (the circuit breaker,
    config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES) pauses for another typed
    confirmation after that many real applies in a row with nothing
    skipped in between. Neither has a bypass flag — see DECISIONS.md."""
    storage.record_run("apply")
    if config.PAUSED:
        log.warning("PAUSED is set — skipping apply cycle entirely.")
        return

    effective_dry_run = config.DRY_RUN or not live
    if config.DRY_RUN and live:
        log.info("--live was passed, but config.DRY_RUN is True on disk - this run stays a dry run.")
    elif not config.DRY_RUN and not live:
        log.warning(
            "config.DRY_RUN is False on disk, but --live wasn't passed on the "
            "command line - forcing this run to behave as a dry run. Re-run "
            "as `apply --live` to actually submit real applications."
        )

    # Temporarily override the module-level config.DRY_RUN for the duration
    # of this cycle so every other module that reads it directly
    # (naukri_client.apply_to_job, excel_log.log_application, storage.mark_applied)
    # sees the correctly-gated effective value without threading a new
    # parameter through each of them. Restored in the finally block below —
    # this must never leak past this function, including on an exception.
    # Same in-process-only technique already used for live-testing, see
    # DECISIONS.md's 2026-09-01 live-verification entries.
    original_dry_run = config.DRY_RUN
    config.DRY_RUN = effective_dry_run
    try:
        if not config.DRY_RUN:
            log.warning(
                "LIVE MODE - real applications will be submitted below, up "
                "to the daily cap (%d).",
                config.DAILY_APPLICATION_CAP,
            )

        applied_today = storage.count_applications_today()
        if applied_today >= config.DAILY_APPLICATION_CAP:
            log.info("Daily application cap (%d) already reached.", config.DAILY_APPLICATION_CAP)
            return

        candidates = storage.get_applicable_jobs(config.FIT_SCORE_THRESHOLD)
        log.info("%d job(s) at or above fit_score threshold %d.", len(candidates), config.FIT_SCORE_THRESHOLD)

        # Pre-flight checkpoint: only for a genuinely real cycle (never a
        # dry run, including one forced back to dry-run above), and only
        # when there's actually something to attempt. A real cycle must
        # never proceed on the LIVE MODE log line alone.
        if not config.DRY_RUN and candidates:
            if not _preflight_summary_and_confirm(candidates, applied_today):
                log.warning("Live apply cycle aborted at pre-flight confirmation - nothing attempted.")
                return

        resume_profile = scoring.load_resume_profile() if config.AUTO_ANSWER_SCREENING_QUESTIONS else None
        # Fresh per cycle, never persisted — Naukri visibly reuses standard
        # screening questions verbatim across postings (see DECISIONS.md),
        # so this skips a redundant _verify_screening_answer() call for a
        # literal repeat within THIS cycle only. resume_profile is fixed
        # for the whole cycle by the time this is created, so it's safe to
        # key the cache on (question, answer) alone. See scoring.py.
        verification_cache: dict = {}

        # Cross-cycle counterpart to verification_cache above -- added
        # 2026-09-06, JOB_SEARCH_STRATEGY.md roadmap item 3: Naukri repeats
        # the same standard questions across DIFFERENT cycles/days too, not
        # just within one, so a durable store (storage.screening_answers)
        # skips a redundant verification call for those repeats as well.
        # Closed over resume_hash (computed once here, not per job — mirrors
        # resume_embedding above) so an edited resume.md can never have its
        # old verdicts silently reused against the new content — see
        # storage.get_screening_answer_verification's docstring. scoring.py
        # only ever sees these two plain callables, never storage.py itself
        # (module boundary rule — see README.md).
        durable_lookup = durable_save = None
        if config.AUTO_ANSWER_SCREENING_QUESTIONS:
            resume_hash = hashlib.sha256(resume_profile.encode()).hexdigest()
            durable_lookup = lambda q, a: storage.get_screening_answer_verification(q, a, resume_hash)
            durable_save = lambda q, a, v: storage.save_screening_answer_verification(q, a, resume_hash, v)

        consecutive_applies = 0
        recent_applies: list = []

        for idx, job in enumerate(candidates):
            if applied_today >= config.DAILY_APPLICATION_CAP:
                log.info("Daily application cap (%d) reached mid-cycle, stopping.", config.DAILY_APPLICATION_CAP)
                break

            answer_fn = None
            if config.AUTO_ANSWER_SCREENING_QUESTIONS:
                # Per-job closure (not built once outside the loop) so range-based
                # answers (e.g. expected CTC) can be calibrated to *this* job's
                # description — see scoring.draft_screening_answer.
                job_description = job.get("description") or ""
                answer_fn = lambda question, options, jd=job_description: scoring.draft_screening_answer(
                    question,
                    options,
                    resume_profile,
                    jd,
                    cache=verification_cache,
                    durable_lookup=durable_lookup,
                    durable_save=durable_save,
                )

            try:
                result = naukri_client.apply_to_job(job["job_id"], job["url"], answer_fn=answer_fn)
            except Exception as e:
                # apply_to_job() already closes its own browser context in a
                # finally block, so this is just about what happens to the
                # AUDIT TRAIL on an unexpected crash (a Playwright timeout, a
                # malformed Ollama response bubbling up, etc.) instead of on a
                # cleanly-returned outcome. Without this, an exception here
                # propagates straight out of run_apply_cycle(), skipping both
                # excel_log.log_application() and storage.mark_applied() for
                # this job AND every candidate after it in the loop — for a
                # real (non-dry-run) attempt, that means a real click could have
                # happened with zero record of it. Treated the same as any other
                # unclear outcome: not marked applied (conservative — a human
                # can check the job page directly), but always logged. See
                # DECISIONS.md.
                log.error(
                    "Job %s (%s): apply_to_job raised an unexpected error - "
                    "logging as a failed attempt and continuing to the next job.",
                    job["job_id"],
                    job.get("title", ""),
                    exc_info=True,
                )
                result = {"applied": False, "reason": f"unexpected_error: {e}", "qa_log": [], "external_url": None}

            # apply_outcome persisted for EVERY attempt (added 2026-09-06) --
            # previously only lived in applications_log.xlsx's free-text
            # Outcome Reason column; jobs.db had no queryable record of why
            # an apply attempt did or didn't succeed. See storage.get_status_summary.
            job_update = {"job_id": job["job_id"], "apply_outcome": result["reason"]}
            external_url = result.get("external_url")
            if external_url:
                job_update["external_apply_url"] = external_url
            storage.upsert_job(job_update)

            excel_log.log_application(
                company=job.get("company", ""),
                title=job.get("title", ""),
                job_id=job["job_id"],
                url=job["url"],
                fit_score=job.get("fit_score"),
                fit_reason=job.get("reason", ""),
                applied=result["applied"],
                outcome_reason=result["reason"],
                qa_log=result.get("qa_log", []),
                dry_run=config.DRY_RUN,
                external_url=external_url,
            )

            if result["reason"] in ("dry_run", "applied"):
                storage.mark_applied(job["job_id"], dry_run=config.DRY_RUN)
                if not config.DRY_RUN:
                    applied_today += 1
                log.info("%s (%s): %s", job["job_id"], job.get("title", ""), result["reason"])
            else:
                log.info("Skipped %s (%s): %s", job["job_id"], job.get("title", ""), result["reason"])

            # Circuit breaker: result["reason"] == "applied" only ever
            # happens for a genuinely real, successful submission (dry-run
            # always returns reason="dry_run" instead — see
            # naukri_client.apply_to_job) — a streak of anything else
            # (skipped, failed, dry-run) resets the counter. Checked once
            # per job, not just at the daily cap, since the failure shape
            # this guards against (real incidents on record — see
            # DECISIONS.md) is an unbroken run of real applies within one
            # cycle, not merely hitting a volume ceiling.
            if result["reason"] == "applied":
                consecutive_applies += 1
                recent_applies.append((job["job_id"], job.get("title", ""), job.get("company", "")))
            else:
                consecutive_applies = 0
                recent_applies = []

            if consecutive_applies >= config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES:
                remaining_count = len(candidates) - idx - 1
                if not _confirm_after_apply_streak(recent_applies, remaining_count):
                    log.warning("Circuit breaker: user did not confirm continuing - stopping the apply cycle.")
                    break
                consecutive_applies = 0
                recent_applies = []
    finally:
        config.DRY_RUN = original_dry_run


def _format_time_ago(iso_timestamp: str) -> str:
    """"3 days ago" / "4 hours ago" / "just now" style formatting for
    run_status_report()'s cadence section. Added 2026-09-06. Presentation
    only -- storage.py returns raw ISO timestamps, this is the one place
    that turns them into something a person reads at a glance."""
    delta = datetime.now() - datetime.fromisoformat(iso_timestamp)
    if delta.days >= 1:
        return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    minutes = delta.seconds // 60
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return "just now"


def run_status_report():
    """Read-only summary of jobs.db's current state -- "how did the last
    run go" without hand-written SQL (previously the README's own
    documented method). Added 2026-09-06, see DECISIONS.md and
    storage.get_status_summary()."""
    summary = storage.get_status_summary()
    print(f"Total jobs: {summary['total_jobs']}")
    print(f"  Unscored: {summary['unscored']}")
    print(f"  Scored: {summary['scored']} (recommend_apply=True: {summary['recommend_apply_true']})")
    if summary["duplicates_detected"]:
        print(f"  Duplicates detected: {summary['duplicates_detected']} (reposts, excluded from scoring)")
    print(f"Applied: {summary['applied_real']} real, {summary['applied_dry_run']} dry-run")
    print(f"Applied today: {summary['applied_today']} / {summary['daily_cap']} daily cap")
    if summary["apply_outcomes"]:
        print("Apply outcomes:")
        for outcome, count in summary["apply_outcomes"].items():
            print(f"  {count:4d}  {outcome}")
    else:
        print("No apply attempts recorded yet.")

    print("\nLast run:")
    last_run_at = summary["last_run_at"]
    for cycle in ("search", "score", "apply", "check-status"):
        if cycle in last_run_at:
            print(f"  {cycle}: {_format_time_ago(last_run_at[cycle])}")
        else:
            print(f"  {cycle}: never")


def run_check_status_cycle():
    """Outcome tracking (added 2026-09-06, see DECISIONS.md and
    JOB_SEARCH_STRATEGY.md's automation-roadmap item 1): checks Naukri's
    own Application History for status updates on jobs already applied
    to, persists any new status entries, then prints the current
    fit_score-vs-outcome picture. Entirely read-only against Naukri (a
    page view, not a write action) — not gated by config.PAUSED, matching
    run_search_cycle()/run_scoring_cycle()."""
    storage.record_run("check-status")
    log.info("Checking Naukri's application status history...")
    history = naukri_client.get_application_status_history()
    log.info("Found %d application(s) in Naukri's history.", len(history))

    for item in history:
        storage.record_application_status(item["job_id"], item["ars_score"], item["statuses"])
        if item["statuses"]:
            latest = max(item["statuses"], key=lambda s: s["status_datetime"])["status_value"]
        else:
            latest = "(no status recorded)"
        log.info("Job %s: latest status = %s (Naukri ars_score=%s)", item["job_id"], latest, item["ars_score"])

    correlation = storage.get_outcome_correlation()
    if not correlation:
        print("No application status history recorded yet.")
        return
    print(f"\n{'fit_score':>9}  {'ars_score':>9}  status")
    for row in correlation:
        fit = row["fit_score"] if row["fit_score"] is not None else "-"
        ars = row["naukri_ars_score"] if row["naukri_ars_score"] is not None else "-"
        print(f"{fit!s:>9}  {ars!s:>9}  {row['latest_apply_status']} - {row['title']} @ {row['company']}")


def main():
    parser = argparse.ArgumentParser(description="Naukri job search + scoring")
    subparsers = parser.add_subparsers(dest="cycle", required=True)

    search_parser = subparsers.add_parser("search", help="search + scrape new jobs")
    search_parser.add_argument(
        "keywords", nargs="?", default=None, help='e.g. "python developer" (omit when using --auto)'
    )
    search_parser.add_argument("location", nargs="?", default="", help='e.g. "Bangalore"')
    search_parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "pick the least-recently-searched role/city combo from "
            "config.SEARCH_QUERIES instead of typing keywords/location manually"
        ),
    )

    subparsers.add_parser("score", help="score unscored jobs against resume.md")

    apply_parser = subparsers.add_parser("apply", help="apply to jobs at/above the fit threshold, capped daily")
    apply_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Actually submit real applications. Requires config.DRY_RUN=False "
            "on disk too - without --live, this command always behaves as a "
            "dry run, even if config.DRY_RUN=False."
        ),
    )

    subparsers.add_parser("status", help="read-only summary of jobs.db's current state")

    subparsers.add_parser(
        "check-status",
        help="check Naukri's own Application History for status updates on applied jobs (read-only)",
    )

    args = parser.parse_args()
    storage.init_db()

    if args.cycle == "search":
        if args.auto:
            if args.keywords is not None:
                search_parser.error("keywords/location can't be combined with --auto")
            run_search_cycle_auto()
        elif args.keywords is None:
            search_parser.error("keywords is required unless --auto is passed")
        else:
            run_search_cycle(args.keywords, args.location)
    elif args.cycle == "score":
        run_scoring_cycle()
    elif args.cycle == "apply":
        run_apply_cycle(live=args.live)
    elif args.cycle == "status":
        run_status_report()
    elif args.cycle == "check-status":
        run_check_status_cycle()


if __name__ == "__main__":
    main()
