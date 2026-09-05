# Flow

Documents how execution actually travels between files, functions, and
modules — what calls what, in what order — separate from `DECISIONS.md`
(which documents *why* choices were made, not the call graph itself).

**Convention:** whoever (human or AI agent) is mid-change should note it under
"Currently in flight" below, naming the exact function(s) being touched, so
anyone picking up the work mid-stream knows which part of the path is
unstable right now. Clear that section back out once the change lands.

## Currently in flight

_Nothing in progress._

## Module map

```
orchestrator.py   -- entry point (CLI) + cycle orchestration
  |-- naukri_client.py   -- all Playwright/DOM logic
  |-- scoring.py          -- embedding + LLM fit scoring (calls Ollama directly)
  |-- storage.py          -- SQLite persistence (machine-queried: candidates, cap count)
  |-- excel_log.py        -- human-readable .xlsx audit trail (Q&A transcripts, one row/attempt)
config.py          -- constants, imported by all of the above
```

`naukri_client.py`, `scoring.py`, and `storage.py` never import each other —
`orchestrator.py` is the only module that ties them together. Keep it that
way; if `scoring.py` ever needs to store something, it returns a value for
`orchestrator.py` to persist rather than importing `storage` directly.

## CLI entry points

`orchestrator.main()` parses `sys.argv` into one of two subcommands, each
calling `storage.init_db()` first, then dispatching:

```
python orchestrator.py search "<keywords>" ["<location>"]
  main() -> run_search_cycle(keywords, location)

python orchestrator.py score
  main() -> run_scoring_cycle()
```

## `run_search_cycle(keywords, location)` — orchestrator.py

```
run_search_cycle
 └─ naukri_client.search_jobs(keywords, location)
     ├─ get_browser_context()            # launch_persistent_context, one Chrome window
     ├─ ensure_logged_in(page)           # goto naukri.com, check URL for "mnjuser",
     │                                   # else block waiting for manual login (<=5 min)
     ├─ goto search results page
     └─ for each job card on the page:
         └─ extract {job_id, title, company, url} via JOB_CARD_SELECTOR /
            JOB_TITLE_SELECTOR / JOB_COMPANY_SELECTOR
     (context closed here — one browser session for the whole search)

 └─ for each job returned above:
     ├─ naukri_client.get_job_details(job["url"])
     │   ├─ get_browser_context()        # NOTE: opens a fresh Chrome window per job
     │   ├─ ensure_logged_in(page)       # cheap re-check, already-logged-in case is fast
     │   ├─ goto job page
     │   └─ extract description (JOB_DESCRIPTION_SELECTOR) + meta (JOB_META_SELECTOR)
     │   (context closed here)
     └─ storage.upsert_job({**job, "description": ...})
```

Known inefficiency, not yet fixed: `get_job_details()` opens/closes its own
browser context per job rather than reusing the one from `search_jobs()` —
correct, but ~10s slower per job than it needs to be. Fine at current volumes
(~20 jobs/run); would matter if search volume grows a lot.

## `run_scoring_cycle()` — orchestrator.py

```
run_scoring_cycle
 ├─ scoring.load_resume_profile()        # reads resume.md once for the whole cycle
 ├─ storage.get_unscored_jobs()          # SELECT ... WHERE fit_score IS NULL
 └─ for each unscored job:
     ├─ scoring.score_job(job["description"], resume_profile)
     │   ├─ _embed(job_description), _embed(resume_profile)   # POST /api/embeddings
     │   │   └─ requests.RequestException -> {fit_score: None, ...}  # added 2026-09-05,
     │   │      see below -- NOT the same as a real 0
     │   ├─ _cosine_similarity(...)
     │   │   └─ if below EMBED_SIMILARITY_FLOOR: return {fit_score: 0, ...} — LLM never called
     │   ├─ _call_scoring_llm(...)        # POST /api/generate, format=json, think=false
     │   │   └─ requests.RequestException -> {fit_score: None, ...}  # added 2026-09-05
     │   ├─ _parse_score_response(raw)    # json.loads + shape/type validation
     │   │   └─ on failure: retry _call_scoring_llm once, then _FALLBACK_RESULT (fit_score: 0)
     │   └─ returns {fit_score, reason, recommend_apply}   # fit_score: int 0-100, or None
     └─ storage.upsert_job({job_id, fit_score, reason, recommend_apply})
        # fit_score=None writes SQL NULL -> job stays picked up by
        # get_unscored_jobs() next cycle, i.e. genuinely retried, not lost
```

`fit_score=None` (added 2026-09-05) is NOT the same as `fit_score=0` — None
means the embedding or LLM call to Ollama itself failed (network/timeout),
and the job is deliberately left unscored so the next `score` run retries it;
0 means scoring actually happened and the job genuinely doesn't match (or,
for two rounds of unparseable model output, degraded to a fallback that
isn't retried since a network retry wouldn't fix bad JSON). Before this fix,
all of these collapsed into the same `fit_score=0` and a transient Ollama
outage silently and permanently dropped whatever job it hit — see
DECISIONS.md, which also documents the real job this happened to live.
`run_scoring_cycle()`'s log line branches on `fit_score is None` rather than
formatting it with `%d`, which would otherwise crash the cycle outright on
the very next transient failure.

Because `resume_profile` is read once at the top of the cycle (not per job),
an edit to `resume.md` mid-run has no effect until the next `score` invocation
— relevant if you're iterating on resume content while a long scoring run is
in flight.

## `run_apply_cycle(live=False)` — orchestrator.py

`live` comes straight from the CLI's `apply --live` flag (main()) — a
second, per-invocation signal on top of config.DRY_RUN, added 2026-09-05
(see DECISIONS.md). Neither one alone is enough to go real:

```
run_apply_cycle(live)
 ├─ if config.PAUSED: return                         # global kill switch, checked first
 ├─ effective_dry_run = config.DRY_RUN or not live
 │   # config.DRY_RUN=True  -> always a dry run, --live is irrelevant
 │   # config.DRY_RUN=False, live=False -> FORCED back to a dry run (loud warning)
 │   # config.DRY_RUN=False, live=True  -> real mode, both signals agree
 ├─ original = config.DRY_RUN; config.DRY_RUN = effective_dry_run   # temp override,
 │   #   restored in a finally block below no matter how this function exits —
 │   #   every other module (naukri_client, excel_log, storage) still just
 │   #   reads config.DRY_RUN directly, so nothing else had to change
 ├─ if not config.DRY_RUN: log.warning("LIVE MODE...")   # loud, unmissable, added 2026-09-05
 ├─ storage.count_applications_today()                # counts dry_run=0 rows only
 │   └─ if >= config.DAILY_APPLICATION_CAP: return
 ├─ storage.get_applicable_jobs(config.FIT_SCORE_THRESHOLD)
 │   # fit_score >= threshold AND (applied=0 OR dry_run=1), ordered by fit_score desc
 ├─ if not config.DRY_RUN and candidates:                # never during a dry run, added 2026-09-05
 │     _preflight_summary_and_confirm(candidates, applied_today)
 │       -> prints every job about to be attempted for real + cap numbers,
 │          requires typed "yes" via _prompt_yes()
 │     -> declined? log + return                          # nothing attempted at all, loop never starts
 ├─ consecutive_applies = 0; recent_applies = []           # circuit breaker state, added 2026-09-05
 └─ for idx, candidate job (enumerate):
     ├─ re-check applied_today >= cap -> break if so   # re-checked EVERY iteration
     ├─ try: naukri_client.apply_to_job(job["job_id"], job["url"])
     │   except Exception: log full traceback, result = {applied: False,
     │     reason: "unexpected_error: ...", qa_log: [], external_url: None}
     │   # Added 2026-09-05 -- without this, any exception here (a Playwright
     │   # timeout, a missing resume-upload file, etc.) propagated straight out
     │   # of this loop, skipping excel_log/storage bookkeeping for this job AND
     │   # every candidate after it. Now always falls through to the normal
     │   # excel_log.log_application() call below instead.
     │   # every return path includes "qa_log" (list, [] when chatbot never reached)
     │   ├─ if config.PAUSED: return {applied: False, reason: "paused", qa_log: []}
     │   ├─ if config.DRY_RUN: log + return {applied: False, reason: "dry_run", qa_log: []}
     │   │   (nothing below this line runs in dry-run mode — no browser opened)
     │   ├─ get_browser_context() + ensure_logged_in(page) + goto job_url
     │   ├─ look for APPLY_BUTTON_SELECTOR ("#apply-button")
     │   │   ├─ not found, but EXTERNAL_APPLY_SELECTOR present
     │   │   │   -> return {applied: False, reason: "external_apply_not_supported", qa_log: []}
     │   │   └─ not found at all -> {applied: False, reason: "apply_button_not_found", qa_log: []}
     │   ├─ button text already says "applied" -> {applied: False, reason: "already_applied", qa_log: []}
     │   ├─ click Apply, jittered_wait()
     │   ├─ QUESTIONNAIRE_MODAL_SELECTOR visible?
     │   │   ├─ config.AUTO_ANSWER_SCREENING_QUESTIONS AND answer_fn given:
     │   │   │   -> _handle_screening_chatbot(page, job_id, answer_fn)  # see below, own qa_log
     │   │   └─ else -> {applied: False, reason: "questionnaire_required_manual_review", qa_log: []}
     │   │      (never fills or answers anything in the modal)
     │   ├─ button text now says "applied" -> {applied: True, reason: "applied", qa_log: []}
     │   └─ else -> {applied: False, reason: "apply_outcome_unclear_manual_review", qa_log: []}
     ├─ excel_log.log_application(company=job["company"], title=job["title"], ...,
     │      fit_score=job["fit_score"], fit_reason=job["reason"],
     │      applied=result["applied"], outcome_reason=result["reason"],
     │      qa_log=result["qa_log"], dry_run=config.DRY_RUN)
     │   # appends one row to applications_log.xlsx for EVERY attempt, dry-run or real,
     │   # regardless of outcome — this is the one thing in the loop that runs unconditionally
     ├─ if result["reason"] in ("dry_run", "applied"):
     │      storage.mark_applied(job_id, dry_run=config.DRY_RUN)
     │      if not config.DRY_RUN: applied_today += 1   # only real applies count toward cap
     │   else: log.info("Skipped ...")                  # not marked applied at all
     └─ result["reason"] == "applied"?                   # circuit breaker, added 2026-09-05 --
        │  # "applied" only ever means a REAL successful submission (dry-run
        │  # is always reason="dry_run" instead) -- anything else resets the streak
        ├─ yes -> consecutive_applies += 1; recent_applies.append(job)
        └─ no  -> consecutive_applies = 0; recent_applies = []
           consecutive_applies >= config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES?
             ├─ _confirm_after_apply_streak(recent_applies, remaining_count)
             │     -> prints the streak just completed, requires typed "yes"
             └─ declined? log + break                     # cycle ends here, already-applied jobs stay applied
 # finally: config.DRY_RUN = original   -- always restored, even on an exception
```

`answer_fn` is built **per job**, inside the `run_apply_cycle()` loop, only
when `config.AUTO_ANSWER_SCREENING_QUESTIONS` is `True`:
`lambda question, options, jd=job_description: scoring.draft_screening_answer(question, options, resume_profile, jd)`
— per-job (not built once outside the loop) so `job_description` can be
bound per closure, letting `draft_screening_answer` see each job's own
description. This is how the LLM call in `scoring.py` reaches
`naukri_client.py` without `naukri_client` importing `scoring` directly
(module boundary, see below).

### `_handle_screening_chatbot(page, job_id, answer_fn)` — naukri_client.py

```
_handle_screening_chatbot   # only reached when AUTO_ANSWER_SCREENING_QUESTIONS=True
 ├─ qa_log = []   # accumulates {question, answer, options} per turn;
 │                # answer=None means "stopped here"; options is the exact
 │                # choice list offered (None for free-text) — kept even on
 │                # a normal answer, so a surprising answer is diagnosable
 │                # after the fact without re-visiting a live page
 └─ loop up to MAX_CHATBOT_TURNS (6):
     ├─ CHATBOT_APPLIED_BANNER_SELECTOR visible -> {applied: True, reason: "applied", qa_log}
     ├─ no messages at all -> {applied: False, reason: "chatbot_state_unrecognized_manual_review", qa_log}
     ├─ latest bot message text = the "question"
     ├─ chip texts gathered, "Skip this question" excluded -> substantive_chips
     ├─ substantive_chips non-empty (genuine chip-choice question)          [checked 1st]
     │   ├─ answer_fn(question, substantive_chips) -> scoring.draft_screening_answer(...)
     │   │   (always a single option now — see DECISIONS.md "Multi-select ...
     │   │   reverted": Naukri's widget is single-select by platform design
     │   │   even when the question reads as "which of these apply")
     │   ├─ None or no match -> _try_skip_question() -> found a "Skip this
     │   │   question" chip? click it, continue loop : else manual-review reason
     │   └─ else -> click the matching chip, continue loop
     ├─ no substantive chips, radio buttons present (CHATBOT_RADIO_SELECTOR) [checked 2nd]
     │   ├─ options = label text per radio, via _radio_label()
     │   ├─ answer_fn(question, options) -> None or no match -> _try_skip_question() as above
     │   └─ else -> _select_radio_option() [clicks the <label>, not .check() the
     │        input — see DECISIONS.md], then _click_send_button(), continue loop
     ├─ text input present (CHATBOT_TEXT_INPUT_SELECTOR)                    [checked 3rd]
     │   ├─ answer_fn(question, None) -> None -> _try_skip_question() as above
     │   └─ else -> fill + _click_send_button(), continue loop
     ├─ file input present (CHATBOT_FILE_INPUT_SELECTOR)                    [checked LAST]
     │   ├─ Path(config.RESUME_PDF_PATH).is_file()? no -> {applied: False,
     │   │    reason: "resume_pdf_not_found_manual_review"}          # added 2026-09-05
     │   └─ yes -> set_input_files(...), jittered_wait(), then check for Naukri's
     │        own "File upload was unsuccessful" text -> if present, {applied: False,
     │        reason: "resume_upload_failed_manual_review"}              # added 2026-09-05
     │      -> else continue loop (still not live-verified end-to-end --
     │      see DECISIONS.md for why this moved to last: it's persistently present
     │      in the DOM regardless of the current question, not a per-question signal)
     ├─ none of the above matched -> _try_skip_question() -> found? continue :
     └─   else -> {applied: False, reason: "chatbot_state_unrecognized_manual_review"}
 └─ loop exhausted -> {applied: False, reason: "questionnaire_too_long_manual_review"}
```

A chip list that's only `["Skip this question"]` (after excluding it,
`substantive_chips` is empty) does NOT mean "this is a single-option chip
question" — it falls through to check radio/text, since Naukri's skip chip
can coexist with a real free-text input on the same question. Treating a
lone skip chip as the whole answer set was the original bug here; see
`DECISIONS.md`.

`scoring.draft_screening_answer(question, options, resume_profile, job_description)`
is the only thing that can produce a real answer — it returns `None`
(never a placeholder) whenever the resume doesn't clearly support one, and
**always** returns `None` for anything mentioning salary/CTC/compensation
regardless of resume content — see `DECISIONS.md`'s "Expected-CTC
calibration abandoned" entry for why. `None` no longer always stops the
walk, though — see `_try_skip_question` above.

For a fixed-option (chip/radio) question, a matched answer isn't returned
immediately — it first goes through `_verify_screening_answer()`, a
second, independent, deliberately skeptical LLM call asking "does the
resume actually, specifically support this exact answer?" A "NO" rejects
it back to `None`. Added after a real wrong answer went out live (see
DECISIONS.md — "Serving Notice Period" reproduced 3/4 times with varied
option lists; more prompt instructions made zero measurable difference,
same lesson as expected-CTC calibration). Free-text answers skip this
second call. Roughly doubles latency for fixed-option answers — accepted
per explicit instruction to prioritize completeness over speed.

A free-text (no-options) answer instead passes through
`_clean_free_text_answer(raw, question)` before being returned — strips a
leading echo of the question and any "Answer:"/"**Answer:**" label,
in code, not via a prompt instruction (a prompt-side attempt at this same
fix caused a real regression in an unrelated options-based question —
see `DECISIONS.md`).

**Live-verified** (2026-09-01, several specific user-authorized jobs —
Capco `310826012994`, Coffeebeans `260826022431`, and Yellowblock
`310826005866`, `DRY_RUN` overridden in-process only, never on disk): the
real click-through path, the external-apply skip, the direct-apply
detection, the questionnaire-modal detection, chip-based answers, radio-
button answers, free-text/contenteditable answers, the "Skip this
question" fallback, and the single-select-only correction. Several of
those required a real bug fix after failing live on the first attempt —
file-input detection order, radio clicking, contenteditable text
detection, chip-vs-skip-chip conflation, and the multi-select-then-
single-select correction; see `DECISIONS.md` for each. **Two real
applications went out unintentionally or as an accepted side effect of
testing** — Capco (an unintentional chip click made while exploring the
chatbot's structure, before `_handle_screening_chatbot` existed) and
Cctech/I Knowledge Factory (external-apply URL capture marking a job
"Applied" as a documented side effect, see `DECISIONS.md`).

**The full walk has now been live-verified end to end**: the Yellowblock
job (`310826005866`) completed with `{applied: True, reason: "applied"}`
after answering a single-select chip question, using "Skip this question"
on an ungrounded CTC question, and answering a radio notice-period
question — the first real, fully-automated completion via
`_handle_screening_chatbot` itself (as opposed to Capco, which completed
via a manual click before this function existed). One caveat surfaced by
that same run: the notice-period answer ("Serving Notice Period") may not
accurately reflect the user's real situation — see the DECISIONS.md entry
on that finding.

**Not yet exercised live:** the resume-upload path (still design-only, see
`_handle_screening_chatbot`'s docstring) and a plain apply with no
questionnaire at all.

`DRY_RUN=True` remains the default; flipping it is a deliberate, explicit
decision each time (see `README.md` Safety notes), not something either the
code or an agent should do on its own. `AUTO_ANSWER_SCREENING_QUESTIONS`
defaults to `False` on top of that — both must be deliberately set for
`_handle_screening_chatbot` to ever run for real.

## Phase 3 (not started)

`naukri_client.get_inbox_messages()` and `send_reply()` both currently just
`raise NotImplementedError`. No orchestrator cycle exists yet for polling the
inbox or drafting replies via `scoring`'s LLM plumbing.
