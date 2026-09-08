# Naukri Automation

Local automation for naukri.com: scores incoming job listings for fit and
(eventually) auto-applies to strong matches, and drafts replies to recruiter
messages for human approval (never auto-sends). Runs entirely locally — no
cloud LLM APIs, no paid services.

**Current status:** Phase 1 (login + search/scrape) and Phase 2 scoring are
live and verified against the real site + a local Ollama instance. The apply
click-through flow is implemented and live-tested (one real application has
gone out — see `DECISIONS.md` incident entry). Naukri's screening-question
chatbot can now be auto-answered from `resume.md` and submitted, but that
path is off by default (see Safety notes) and not yet live-tested end to
end. Phase 3 (inbox/reply drafting) hasn't been started.

See `DECISIONS.md` for the reasoning behind non-obvious architectural choices,
and `FLOW.md` for how execution actually travels between modules.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chrome
```

Fill in `resume.md` with your actual background (used by scoring).

## First run

```
python orchestrator.py search "python developer" "Bangalore"
```

A visible Chrome window opens on first run. Log in manually via naukri's
native email/OTP form — **not** "Continue with Google", which gets blocked by
Google's bot-detection wall inside an automated browser (see `DECISIONS.md`).
This tool never automates OTP entry. The session persists in
`.browser_profile/`, so subsequent runs won't prompt for login again as long
as that directory isn't deleted.

The run logs how many jobs were found and saves them to `jobs.db` (SQLite).
A job that's a repost of one already known under a different `job_id` is
detected automatically (by comparing description embeddings — see
`DECISIONS.md`) and flagged rather than treated as new; it's never scored
or applied to independently. Inspect results with:

```
sqlite3 jobs.db "select title, company, url from jobs;"
```

Instead of typing `keywords`/`location` every time, `search --auto` picks
the least-recently-searched role/city combo from `config.SEARCH_QUERIES`
(edit that list to match your actual target roles/cities):

```
python orchestrator.py search --auto
```

A combo that's never been searched is always picked before one that has,
no matter how long ago; repeated invocations (e.g. a scheduled/cron run)
rotate through every combo in the list rather than only ever hitting
whichever one gets typed most often.

Then score the scraped jobs against `resume.md`:

```
python orchestrator.py score
```

And apply to jobs at/above the fit threshold, capped daily (dry-run by
default — see Safety notes):

```
python orchestrator.py apply
```

This always runs as a dry run unless you also pass `--live` (and
`config.DRY_RUN` is `False` in `config.py`):

```
python orchestrator.py apply --live
```

Every apply attempt — dry-run or real, successful or not — gets a row in
`applications_log.xlsx` (created automatically): company, job title, fit
score/reason, outcome, the full question-and-answer transcript from the
screening chatbot (if any), and whether it was a dry run. Open it directly
in Excel; it's a human-readable companion to `jobs.db`, not something other
code reads. The same outcome is also persisted to `jobs.db`'s
`apply_outcome` column for every attempt, queryable without opening Excel.

For a quick read-only summary — how many jobs are scored, how many were
detected as reposts of a job already known, how many have been applied to
(real vs. dry-run), a breakdown of apply outcomes, and how long it's been
since you last ran each cycle (`search`/`score`/`apply`/`check-status`) —
without hand-written SQL:

```
python orchestrator.py status
```

To check what actually happened to real applications after submission —
Naukri's own status (Applied / Application Sent / Shortlisted / Not
Shortlisted, as recruiters act on them) and its own relevance score,
correlated against this tool's `fit_score` — run:

```
python orchestrator.py check-status
```

This is read-only (a page view, not a write action) and only covers
whatever Naukri's Application History page shows on its default view; if
your real application count grows past one page, older history stops
being tracked until pagination support is added (a warning is logged when
this happens, not silently dropped).

## Safety notes

- `config.DRY_RUN = True` by default — `apply_to_job()` only logs what it
  would do, never actually applies. Flipping this requires an explicit,
  deliberate change.
- Going live requires a SECOND, explicit signal on top of that: pass
  `apply --live` on the command line. `config.DRY_RUN=False` alone is not
  enough — without `--live`, the apply cycle always behaves as a dry run
  regardless of what's on disk, so a config change that got flipped and
  forgotten can't silently go live the next time `apply` is run out of
  habit. Both `config.DRY_RUN=False` AND `--live` are required together.
- `config.AUTO_ANSWER_SCREENING_QUESTIONS = False` by default — even with
  `DRY_RUN` off, Naukri's screening-question chatbot is left for manual
  review unless this second flag is also explicitly `True`. When it is, the
  chatbot is walked automatically: answers are drafted from `resume.md` by
  a local LLM and **submitted with no human review step**. Answers are only
  ever given when the resume clearly supports one — anything it can't
  confidently ground is skipped, never guessed. Salary/CTC questions are
  *always* skipped regardless of resume content (see `DECISIONS.md` —
  numeric range calibration proved unreliable for this model); notice
  period and relocation are answered from `resume.md`'s Availability &
  logistics section when it's filled in. A fixed-option (chip/radio)
  answer is independently re-verified before being used, and Naukri
  visibly repeats the same standard questions across postings, so a
  question already verified once — even in a previous run, on a previous
  day — is remembered (keyed to the current content of `resume.md`; an
  edited resume never reuses an old verdict) rather than re-verified from
  scratch every time. Still, this is the highest-risk switch in the
  codebase; flip it deliberately.
- Scoring itself doesn't trust a single sample near the threshold: a job
  whose `fit_score` lands within `config.FIT_SCORE_REVERIFY_MARGIN` (10
  by default) of `config.FIT_SCORE_THRESHOLD` is automatically re-scored
  `config.FIT_SCORE_REVERIFY_PASSES` times total and the average decides
  `fit_score`/`recommend_apply` (see `DECISIONS.md`). Jobs clearly above
  or below the threshold are scored once, as before.
- `config.PAUSED` is a global kill switch for all write actions.
- `config.DAILY_APPLICATION_CAP` is enforced in code, not just as an LLM
  instruction.
- Before a real (`--live`) apply cycle's loop even starts, it prints every
  candidate about to be attempted (fit score, title, company) and the
  daily-cap numbers, and requires you to type "yes" — declining aborts the
  whole cycle with nothing attempted. There's no flag to skip this prompt;
  it fails closed if there's no interactive terminal to answer it (e.g. a
  cron job), rather than hanging or proceeding silently.
- `config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES` (3 by default) — after that
  many real applications go through back-to-back with nothing
  skipped/failed in between, the apply cycle pauses and shows that streak,
  requiring another typed "yes" before continuing to the rest of the
  candidates. This is separate from the daily cap: it catches an unbroken
  run of applies within one cycle (the shape a misbehaving code path would
  take), not just total volume for the day.
- Replies to recruiters are never sent automatically — drafts are saved to
  the `messages` table for manual review and a separate, deliberate send.

Any change to caps, thresholds, or the dry-run default is safety-relevant —
treat it as a real decision, not routine config.
