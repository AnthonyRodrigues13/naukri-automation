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

`orchestrator.main()` parses `sys.argv` into a subcommand, each
calling `storage.init_db()` first, then dispatching:

```
python orchestrator.py search "<keywords>" ["<location>"]
  main() -> run_search_cycle(keywords, location)

python orchestrator.py score
  main() -> run_scoring_cycle()

python orchestrator.py apply [--live]
  main() -> run_apply_cycle(live=args.live)

python orchestrator.py status
  main() -> run_status_report()          # added 2026-09-06, read-only, no storage.init_db()
                                          # side effects beyond the schema migration it already does

python orchestrator.py check-status
  main() -> run_check_status_cycle()     # added 2026-09-06, read-only against Naukri (a page
                                          # view, not a write action) -- not gated by config.PAUSED
```

## `run_search_cycle(keywords, location)` — orchestrator.py

```
run_search_cycle
 ├─ storage.record_run("search")   # cadence tracking, added 2026-09-06 -- see run_status_report
 ├─ already_known = storage.get_job_ids_with_description()   # added 2026-09-06
 └─ naukri_client.search_jobs_with_details(keywords, location, skip_job_ids=already_known)
     ├─ get_browser_context()            # launch_persistent_context, ONE Chrome window
     │                                   # for the entire run, added 2026-09-05
     ├─ ensure_logged_in(page)           # ONCE for the whole run, not per job — goto
     │                                   # naukri.com, check URL for "mnjuser", else
     │                                   # block waiting for manual login (<=5 min)
     ├─ _scrape_search_results(page, keywords, location, max_pages)
     │   ├─ goto search results page
     │   └─ for each job card on the page:
     │       └─ extract {job_id, title, company, url} via JOB_CARD_SELECTOR /
     │          JOB_TITLE_SELECTOR / JOB_COMPANY_SELECTOR
     └─ for each job returned above, on the SAME page:
         ├─ job["job_id"] in skip_job_ids?                     # added 2026-09-06
         │     -> results.append(dict(job))   # NO "description" key at all --
         │        storage.upsert_job() below only SETs keys present in the dict,
         │        so omitting it leaves the existing stored description untouched
         │     -> continue (detail fetch skipped entirely for this job)
         ├─ try: _scrape_job_details(page, job["url"])
         │   ├─ goto job page                                    # no fresh context,
         │   └─ extract description (JOB_DESCRIPTION_SELECTOR)   # no re-login —
         │       + meta (JOB_META_SELECTOR, discarded by the caller) # same page/session
         │       -> empty match? log.warning(...)                 # added 2026-09-06
         │   except Exception: log full traceback, description = ""
         │     # added 2026-09-05 alongside the reuse fix -- with everything now built
         │     # up in one list over the whole function (not persisted per-job by the
         │     # caller as before), an uncaught exception here would otherwise lose
         │     # every job's search-card info too, not just the failed job's description
         ├─ results.append({**job, "description": description})
         └─ log.info("Fetched details for job %d/%d...")   # added 2026-09-05 --
            # the caller gets nothing back until this whole loop finishes now
            # (unlike the old per-job get_job_details(), which let
            # run_search_cycle() log progress after each one), so this is
            # logged here instead to keep a run-in-progress visible
     ├─ empty_description_count > 0? log.warning("N/M job(s) had an empty
     │     description...")                                       # added 2026-09-06
     (context closed here — ONE browser session for the whole search AND every job's
      detail fetch, not one per job)

 └─ for each job in the returned list: storage.upsert_job(job)
     # job already has job_id/title/company/url/description (unless skipped, see
     # above) -- no merging needed at the call site any more
```

Fixed 2026-09-05 (see DECISIONS.md): the old `search_jobs()` + per-job
`get_job_details()` pattern opened a FRESH browser context (a full
`launch_persistent_context()` profile reload) AND re-ran `ensure_logged_in()`
(an extra navigation to the naukri.com homepage) before every single job's
detail fetch — confirmed live to cost ~4.5 minutes for a 20-job search.
`search_jobs()` and `get_job_details()` still exist unchanged as standalone,
each-opens-its-own-context functions (now thin wrappers around the shared
`_scrape_search_results()`/`_scrape_job_details()` helpers) — only
`run_search_cycle()` was switched to the combined, single-session
`search_jobs_with_details()`. Tradeoff: `ensure_logged_in()` no longer runs
per job, so a session that expires mid-run wouldn't be caught until the next
`search` invocation — accepted, since the whole run is now fast enough
(seconds, not minutes) that a mid-run expiry is far less likely than it was
in the multi-minute version.

## `run_scoring_cycle()` — orchestrator.py

```
run_scoring_cycle
 ├─ storage.record_run("score")   # cadence tracking, added 2026-09-06 -- see run_status_report
 ├─ scoring.load_resume_profile()        # reads resume.md once for the whole cycle
 ├─ storage.get_unscored_jobs()          # SELECT ... WHERE fit_score IS NULL
 ├─ no unscored jobs? return                                    # added 2026-09-06
 ├─ resume_embedding = scoring.embed_resume(resume_profile)      # added 2026-09-06,
 │     ONCE for the whole cycle, not once per job -- resume_profile doesn't
 │     change across jobs within one cycle, so re-embedding it per job was waste
 ├─ resume_embedding is None? log + return                       # skip the WHOLE
 │     cycle rather than try every job against a missing embedding
 └─ for each unscored job:
     ├─ scoring.score_job(job["description"], resume_profile, resume_embedding=resume_embedding)
     │   ├─ _embed(job_description); resume side uses resume_embedding directly,
     │   │     no _embed(resume_profile) call any more                # POST /api/embeddings
     │   │   └─ (requests.RequestException, KeyError, TypeError) -> {fit_score: None, ...}
     │   │      # KeyError/TypeError added 2026-09-06 -- a 200 OK with an unexpected
     │   │      # body (e.g. Ollama {"error": "model not found"}) is not a
     │   │      # RequestException, but must be treated the same way, see below
     │   ├─ _cosine_similarity(...)
     │   │   └─ if below EMBED_SIMILARITY_FLOOR: return {fit_score: 0, ...} — LLM never called
     │   ├─ _call_scoring_llm(...)        # POST /api/generate, format=json, think=false
     │   │   └─ (requests.RequestException, KeyError, TypeError) -> {fit_score: None, ...}
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
(see DECISIONS.md). Neither one alone is enough to go real.

Every `{applied: ..., reason: ..., qa_log: ...}` dict shown below and in
`_handle_screening_chatbot` (further down) is actually a
`naukri_client.ApplyResult` (added 2026-09-06, see DECISIONS.md) — always
carrying `external_url` too (`None` except for
`reason="external_apply_not_supported"`), built via the single
`_apply_result()` function so every return path in both functions is
guaranteed the same shape. Simplified to the three most-relevant keys
below for readability:

```
run_apply_cycle(live)
 ├─ storage.record_run("apply")   # cadence tracking, added 2026-09-06 -- see run_status_report;
 │     recorded even if PAUSED/cap/preflight-decline below end up doing nothing this run
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
     ├─ storage.upsert_job({job_id, apply_outcome: result["reason"], [external_apply_url
     │      if result.get("external_url")]})                       # added 2026-09-06 --
     │      apply_outcome persisted for EVERY attempt, not just when external_url
     │      happens to be set (previously the only trigger for this upsert call at all)
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
`lambda question, options, jd=job_description: scoring.draft_screening_answer(question, options, resume_profile, jd, cache=verification_cache)`
— per-job (not built once outside the loop) so `job_description` can be
bound per closure, letting `draft_screening_answer` see each job's own
description. This is how the LLM call in `scoring.py` reaches
`naukri_client.py` without `naukri_client` importing `scoring` directly
(module boundary, see below).

`verification_cache = {}` (added 2026-09-06, see DECISIONS.md) IS built
once outside the loop, right alongside `resume_profile` — the opposite of
`job_description`'s per-job binding, deliberately: it's meant to persist
*across* jobs for the whole cycle, keyed on `(question, answer)` inside
`scoring._verify_screening_answer()`. Naukri reuses standard screening
questions verbatim across postings, so a repeat within one cycle skips a
redundant verification LLM call. Never persisted across process runs —
recreated fresh every `run_apply_cycle()` call.

### `_handle_screening_chatbot(page, job_id, answer_fn)` — naukri_client.py

Split into three functions 2026-09-06 (see DECISIONS.md): reading DOM state
(`_read_chatbot_turn_state`, Playwright-only), deciding what to do about it
(`_decide_chatbot_turn`, PURE — no Playwright, no filesystem, no logging),
and carrying that decision out (`_handle_screening_chatbot` itself — the
Playwright actions, the resume-upload's own post-upload re-check, and all
logging). `_decide_chatbot_turn` is what's actually covered by
`tests/test_naukri_client.py`'s `DecideChatbotTurnTest` — no browser or
Ollama needed to exercise every branch below.

```
_handle_screening_chatbot   # only reached when AUTO_ANSWER_SCREENING_QUESTIONS=True
 ├─ qa_log = []   # accumulates {question, answer, options} per turn;
 │                # answer=None means "stopped here"; options is the exact
 │                # choice list offered (None for free-text) — kept even on
 │                # a normal answer, so a surprising answer is diagnosable
 │                # after the fact without re-visiting a live page
 └─ loop up to MAX_CHATBOT_TURNS (6):
     ├─ state = _read_chatbot_turn_state(page)   # ALL Playwright reads for this turn,
     │     bundled into one plain dict: applied_banner_visible, has_messages,
     │     question, chip_texts (FULL list, skip chip included if present),
     │     radio_options, text_input_present, file_input_present, resume_pdf_exists
     ├─ action = _decide_chatbot_turn(state, answer_fn)   # PURE, see below
     └─ dispatch on action["type"]:
         ├─ "applied" -> {applied: True, reason: "applied", qa_log}
         ├─ "no_messages" -> {applied: False, reason: "chatbot_state_unrecognized_manual_review", qa_log}
         ├─ "click_chip" -> click chip at action["index"], append qa_entry, continue
         ├─ "click_skip_chip" -> log "using Naukri's own Skip option", click chip
         │     at action["index"], append qa_entry, continue
         ├─ "manual_review" -> log via _MANUAL_REVIEW_LOG_MESSAGES[action["detail"]],
         │     append qa_entry, return {applied: False, reason: action["reason"], qa_log}
         ├─ "select_radio" -> _select_radio_option() [clicks the <label>, not .check()
         │     the input — see DECISIONS.md] at action["index"], append qa_entry,
         │     _click_send_button() -> False? return manual_review same qa_entry : continue
         ├─ "fill_text" -> fill action["text"], append qa_entry, _click_send_button()
         │     -> False? return manual_review same qa_entry : continue
         └─ "attempt_resume_upload" -> the ONE action _decide_chatbot_turn can't fully
               decide ahead of time (success/failure only knowable by reading the DOM
               again AFTER acting) -- stays imperative:
               set_input_files(config.RESUME_PDF_PATH), jittered_wait(), check for
               Naukri's own "File upload was unsuccessful" text -> present? append
               qa_entry(answer="(upload failed)"), return resume_upload_failed_manual_review
               : append qa_entry(answer="(uploaded resume)"), continue
 └─ loop exhausted -> {applied: False, reason: "questionnaire_too_long_manual_review"}
```

`_decide_chatbot_turn(state, answer_fn)` — pure, checked in this order:

```
_decide_chatbot_turn
 ├─ state["applied_banner_visible"] -> {"type": "applied"}
 ├─ not state["has_messages"] -> {"type": "no_messages"}
 ├─ substantive_chips = chip_texts with "skip this question" excluded
 ├─ substantive_chips non-empty (genuine chip-choice question)          [checked 1st]
 │   ├─ answer_fn(question, substantive_chips) -> scoring.draft_screening_answer(...)
 │   │   (always a single option now — see DECISIONS.md "Multi-select ...
 │   │   reverted": Naukri's widget is single-select by platform design
 │   │   even when the question reads as "which of these apply")
 │   ├─ None or no match among the FULL chip_texts -> skip_action() finds a "Skip
 │   │   this question" chip in chip_texts? "click_skip_chip" at its index :
 │   │   "manual_review" (detail "no_answer" or "no_chip_match")
 │   └─ else -> "click_chip" at the matched index (into the FULL chip_texts,
 │        not substantive_chips)
 ├─ no substantive chips, radio_options non-empty                        [checked 2nd]
 │   ├─ answer_fn(question, radio_options) -> None or no match -> skip_action() as above
 │   └─ else -> "select_radio" at the matched index
 ├─ text_input_present                                                   [checked 3rd]
 │   ├─ answer_fn(question, None) -> None -> skip_action(None) as above
 │   └─ else -> "fill_text"
 ├─ file_input_present                                                   [checked LAST]
 │   ├─ NO skip_action() check here, deliberately — a skip chip coexisting with the
 │   │   file uploader specifically was never observed/considered; see DECISIONS.md
 │   ├─ not resume_pdf_exists -> "manual_review" (detail "resume_pdf_not_found")
 │   └─ else -> "attempt_resume_upload"
 └─ none of the above matched -> skip_action(None) -> found? "click_skip_chip" :
      "manual_review" (detail "no_mechanism")
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
walk, though — see `_decide_chatbot_turn`'s `skip_action()` above.

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

## `run_status_report()` — orchestrator.py

Added 2026-09-06 (see DECISIONS.md), backing the `status` CLI subcommand.
Entirely read-only — no writes, no browser, no Ollama.

```
run_status_report
 └─ storage.get_status_summary()   # a handful of read-only aggregate COUNT(*) queries:
        total jobs, unscored, scored, recommend_apply=True count,
        applied (real vs dry-run) counts, today's real-apply count,
        apply_outcome value -> count (GROUP BY), last_run_at (see below)
 └─ print(...)                     # plain stdout, not logged via `log`
 └─ for cycle in (search, score, apply, check-status):
        print(f"{cycle}: {_format_time_ago(...)}" or "never")
```

`get_status_summary()`'s `last_run_at` key is `storage.get_last_run_times()`
— cadence/staleness tracking, added 2026-09-06 (see DECISIONS.md and
JOB_SEARCH_STRATEGY.md roadmap item 5): `storage.record_run(cycle_name)`
is called at the very start of `run_search_cycle`/`run_scoring_cycle`/
`run_apply_cycle`/`run_check_status_cycle` (before any early-return path,
so even a cycle that decides to do nothing — PAUSED, cap already
reached, preflight declined — still counts as "you ran this recently"),
appending one row to the `run_history` table. There was no existing
timestamp for this before — `jobs.scraped_at`/`applied_at` are per-job,
not per-cycle, and scoring had no timestamp column at all.

## `run_check_status_cycle()` — orchestrator.py

Added 2026-09-06 (see DECISIONS.md and JOB_SEARCH_STRATEGY.md's
automation-roadmap item 1: outcome tracking). Entirely read-only against
Naukri (a page view, not a write action) — not gated by `config.PAUSED`,
same as `run_search_cycle()`/`run_scoring_cycle()`.

```
run_check_status_cycle
 ├─ storage.record_run("check-status")   # cadence tracking, added 2026-09-06 -- see run_status_report
 └─ naukri_client.get_application_status_history()
     ├─ get_browser_context() + ensure_logged_in(page)
     ├─ page.on("response", ...) registered BEFORE navigating, to capture
     │     the history page's OWN natural API call
     ├─ page.goto(".../myapply/historypage", wait_until="networkidle")
     │   # that page's own JS calls .../applyapi/v5/history with an
     │   # authorization: Bearer <JWT> + appid/systemid headers it
     │   # attaches itself -- live-verified 2026-09-06: a hand-built
     │   # context.request.get(...) or page.evaluate(fetch(...)) with just
     │   # session cookies both get a 400. Captured via the response
     │   # listener instead of reconstructed by hand -- see DECISIONS.md
     ├─ no response captured -> log.warning(...), return []
     ├─ matchingRowsCount > len(applyDetails)? -> log.warning("pagination
     │     not implemented - older history is not being tracked")
     └─ returns [{job_id, ars_score, is_open, statuses: [{status_id,
           status_value, status_datetime}, ...]}, ...] -- entries missing
           a job_id, or individual statuses missing a value/datetime, are
           filtered out rather than stored malformed
 └─ for each entry:
     ├─ storage.record_application_status(job_id, ars_score, statuses)
     │     -> INSERT OR IGNORE into application_status_history on
     │        (job_id, status_id, status_datetime) -- re-recording an
     │        already-seen status on a later check is a no-op, not a
     │        duplicate row; also updates jobs.latest_apply_status and
     │        jobs.naukri_ars_score to the most recent values
     └─ log.info("Job %s: latest status = %s (Naukri ars_score=%s)", ...)
 └─ storage.get_outcome_correlation()   # fit_score vs. latest_apply_status/
        naukri_ars_score for every job with a recorded status, ordered by
        fit_score desc -- the actual data this capability exists to produce
 └─ print(...)                          # plain stdout, not logged via `log`
```

## Phase 3 (not started)

`naukri_client.get_inbox_messages()` and `send_reply()` both currently just
`raise NotImplementedError`. No orchestrator cycle exists yet for polling the
inbox or drafting replies via `scoring`'s LLM plumbing.

**Investigated 2026-09-06, implementation deliberately deferred** (see
DECISIONS.md): the inbox lives at `https://www.naukri.com/mnjuser/inbox`
(linked from the nav bar), and its message list is populated by:

```
POST https://www.naukri.com/cloudgateway-nc-js/nc-services/v0/template/ni-inboxusermails-svc-tmpl_v0
```

Live-verified response wrapper shape (real, from this account, which
currently has zero messages so `inbox` is empty):

```json
{"successResponse": {"total": 0, "mailsTotal": 0, "mailsUnreadTotal": 0,
  "unreadPowerNvite": 0, "unread": 0, "totalPowerNvite": 0,
  "unreadMostRelevantMail": 0, "unseenOthersMailPresent": 0, "inbox": []},
 "exceptions": {...}, "additionalProp": {...}}
```

The wrapper (top-level counts, `inbox` as a list) is confirmed; the shape
of an individual item INSIDE `inbox` is not — there's no real message in
this account to inspect one against, and guessing field names (sender,
body, timestamp) here would repeat exactly the mistake this project's own
incident history warns against (see DECISIONS.md). Deliberately not
implemented until a real message exists to verify against, rather than
shipping a best-effort guess.
