"""All Playwright/DOM logic for naukri.com lives here and nowhere else.
Naukri's markup changes periodically — when selectors break, this is the
only file that should need to change.
"""

import logging
import random
import time
from pathlib import Path
from typing import TypedDict

from playwright.sync_api import Page, sync_playwright

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("naukri_client")

BASE_URL = "https://www.naukri.com"
LOGIN_URL_MARKER = "mnjuser"
SEARCH_URL_TEMPLATE = BASE_URL + "/{keywords}-jobs{location_suffix}"

JOB_CARD_SELECTOR = "div.cust-job-tuple"
JOB_TITLE_SELECTOR = "a.title"
JOB_COMPANY_SELECTOR = "a.comp-name, span.comp-name"
NEXT_PAGE_SELECTOR = "a.styles_btn-secondary__2AsIP:has-text('Next')"

# These CSS-module class hashes (e.g. __h0K4t) are generated per naukri
# frontend build and can change on their own release cadence, independent of
# any visible markup change — re-verify against a live job page if this comes
# back empty again.
JOB_DESCRIPTION_SELECTOR = "div.styles_JDC__dang-inner-html__h0K4t, section.styles_job-desc-container__txpYf"
JOB_META_SELECTOR = "div.styles_jhc__jd-stats__KrId0"

# Naukri shows one of two buttons on a job page, never both meaningfully:
# a direct in-site Apply (automatable) or a redirect to the company's own
# site (a different, unpredictable form per company — not automated here).
APPLY_BUTTON_SELECTOR = "#apply-button"
EXTERNAL_APPLY_SELECTOR = "#company-site-button"

# Live-verified: clicking Apply on a job with screening questions opens a
# right-side drawer built from nested divs named "chatbot_*" / "_chatBot*"
# (Naukri's real markup, confirmed 2026-09-01, Capco job 310826012994) —
# "Hi! Anthony, the recruiter needs some profile information also..." with
# a Save button. Whether anything in it gets answered/clicked is gated by
# config.AUTO_ANSWER_SCREENING_QUESTIONS — see _handle_screening_chatbot().
#
# The :visible pseudo-class matters here: the outermost wrapper
# (_chatBotContainer) is the first DOM match for a bare "chatbot"/"modal"
# selector but itself reports not-visible (zero-size positioning wrapper) —
# only its children are actually visible. Checking `.first.is_visible()`
# on the un-filtered selector picks that wrapper and silently misses the
# real, visible drawer. Filtering with :visible at the selector level and
# checking .count() avoids that trap entirely.
QUESTIONNAIRE_MODAL_SELECTOR = (
    "div[class*='modal' i]:visible, div[role='dialog']:visible, "
    "div[class*='chatbot' i]:visible, div[class*='chatBot' i]:visible"
)

# Chatbot drawer internals. IDs (e.g. "sendMsg__yybxs5xxhInputBox") carry a
# random per-session prefix — never hardcode them, match by class/prefix
# instead. Live-verified 2026-09-01 against three real questions across two
# jobs: a chip-based resume-upload prompt (Capco), and a radio-button notice-
# period question (Coffeebeans). The text-input (free-typed answer) path is
# still unverified — every real question seen so far has been chips or
# radios, never a raw text box — treat CHATBOT_TEXT_INPUT_SELECTOR as a
# best-effort heuristic like JOB_DESCRIPTION_SELECTOR was before it broke.
#
# CHATBOT_FILE_INPUT_SELECTOR matches a real <input type="file"> that is
# PERSISTENTLY present in the drawer's DOM regardless of the current
# question — it is chat-widget furniture, not a signal that "this question
# wants a file." Treating its mere presence as that signal was the original
# bug: every question (including a plain radio-button one) was misread as a
# resume-upload prompt, the file was uploaded into the wrong step, Naukri
# rejected it ("File upload was unsuccessful"), and the same question kept
# re-appearing until the turn cap stopped the loop. Fixed by only reaching
# the file-input branch last, when no chip/radio/text mechanism matched.
CHATBOT_MESSAGE_SELECTOR = ".botItem.chatbot_ListItem"
CHATBOT_CHIP_SELECTOR = ".chatbot_Chip.chipInRow.chipItem"
CHATBOT_RADIO_SELECTOR = "input.ssrc__radio"
CHATBOT_FILE_INPUT_SELECTOR = ".chatbot_Uploader"
# Live-verified 2026-09-01: Naukri's free-text chatbot answer field is a
# contenteditable div (class "textArea", contenteditable="true"), not a
# real <input>/<textarea> — the original selector matched neither and this
# question type fell through undetected entirely. Playwright's .fill()
# works on contenteditable elements the same as real inputs.
CHATBOT_TEXT_INPUT_SELECTOR = (
    ".chatbot_InputContainer textarea, .chatbot_InputContainer input[type='text'], "
    "div.textArea[contenteditable='true']"
)
CHATBOT_SEND_BUTTON_SELECTOR = "[id^='sendMsg_']:not(.disabled) .sendMsg"
CHATBOT_APPLIED_BANNER_SELECTOR = "text=/Applied to/i"

MAX_CHATBOT_TURNS = 6


class ApplyResult(TypedDict):
    """Return shape shared by apply_to_job() and _handle_screening_chatbot()
    (the latter's return value IS one of the former's, in the branch that
    hands off to it directly) — added 2026-09-06, see DECISIONS.md. Every
    return path in both functions is built via _apply_result() below, so
    all four keys are always present; previously only "qa_log" was
    guaranteed everywhere and "external_url" appeared in just 1 of
    apply_to_job()'s 9 return statements.

    "applied" is True only for an actual, real submission — DRY_RUN always
    returns False with reason="dry_run" even though that's the expected
    outcome; callers key off `reason`, not just `applied`, to decide what
    counts as an attempt for audit logging.
    "qa_log" is a list of {"question", "answer", "options"} dicts, [] when
    the screening chatbot was never reached.
    "external_url" is None except for reason="external_apply_not_supported"
    with config.CAPTURE_EXTERNAL_APPLY_URLS True — every other path leaves
    it None, but the key itself is always present so callers can index it
    uniformly instead of needing .get().
    """

    applied: bool
    reason: str
    qa_log: list
    external_url: str | None


def _apply_result(applied: bool, reason: str, qa_log: list | None = None, external_url: str | None = None) -> ApplyResult:
    """The ONE place every apply_to_job()/_handle_screening_chatbot() return
    value is built — see ApplyResult. qa_log defaults to a FRESH empty
    list per call (never a shared mutable default)."""
    return {
        "applied": applied,
        "reason": reason,
        "qa_log": qa_log if qa_log is not None else [],
        "external_url": external_url,
    }


def jittered_wait():
    time.sleep(random.uniform(config.MIN_DELAY_SECONDS, config.MAX_DELAY_SECONDS))


def get_browser_context():
    """Launches (or reattaches to) a persistent Chrome profile so login
    survives across process runs. Headless=False on purpose — first run
    needs a visible window for manual login/OTP entry."""
    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=config.USER_DATA_DIR,
        channel="chrome",
        headless=False,
    )
    return playwright, context


def ensure_logged_in(page: Page):
    """Navigates to naukri.com and confirms a logged-in session. If not
    logged in, pauses and waits for the user to log in manually (including
    any OTP step) — this code never fills or clicks anything on an OTP
    field or login form.

    Detection is URL-based rather than a CSS selector: naukri.com redirects
    an authenticated visit to /mnjuser/homepage, while an unauthenticated
    visit stays on the public marketing page. This is far more stable across
    naukri's frontend markup changes than any single nav-icon selector."""
    page.goto(BASE_URL, wait_until="domcontentloaded")
    jittered_wait()

    if LOGIN_URL_MARKER in page.url:
        log.info("Already logged in.")
        return

    log.warning(
        "Not logged in. Please log in manually in the open browser window "
        "(including OTP if prompted). Waiting up to 5 minutes..."
    )
    try:
        page.wait_for_url(f"**{LOGIN_URL_MARKER}**", timeout=300_000)
        log.info("Login detected, continuing.")
    except Exception as e:
        raise RuntimeError(
            "Timed out waiting for manual login. Re-run and log in within 5 minutes."
        ) from e


def _job_id_from_url(url: str) -> str:
    # Naukri job URLs end in a numeric/job-code segment, e.g. .../job-listings-<id>
    return url.rstrip("/").split("-")[-1].split("?")[0]


def _scrape_search_results(page: Page, keywords: str, location: str, max_pages: int) -> list[dict]:
    """Shared by search_jobs() and search_jobs_with_details() — assumes the
    caller already has an open, logged-in `page`. Returns
    [{job_id, title, company, url}, ...]."""
    slug = keywords.strip().lower().replace(" ", "-")
    location_suffix = f"-in-{location.strip().lower().replace(' ', '-')}" if location else ""
    search_url = SEARCH_URL_TEMPLATE.format(keywords=slug, location_suffix=location_suffix)

    page.goto(search_url, wait_until="domcontentloaded")
    jittered_wait()

    results: list[dict] = []
    for page_num in range(1, max_pages + 1):
        page.wait_for_selector(JOB_CARD_SELECTOR, timeout=30_000)
        cards = page.locator(JOB_CARD_SELECTOR)
        count = cards.count()
        log.info("Page %d: found %d job cards.", page_num, count)

        for i in range(count):
            card = cards.nth(i)
            title_el = card.locator(JOB_TITLE_SELECTOR).first
            url = title_el.get_attribute("href")
            if not url:
                continue
            title = title_el.inner_text().strip()
            company = ""
            if card.locator(JOB_COMPANY_SELECTOR).first.count():
                company = card.locator(JOB_COMPANY_SELECTOR).first.inner_text().strip()

            results.append(
                {
                    "job_id": _job_id_from_url(url),
                    "title": title,
                    "company": company,
                    "url": url,
                }
            )

        if page_num < max_pages:
            next_btn = page.locator(NEXT_PAGE_SELECTOR).first
            if not next_btn.count() or not next_btn.is_enabled():
                log.info("No further pages.")
                break
            next_btn.click()
            jittered_wait()

    return results


def _scrape_job_details(page: Page, job_url: str) -> dict:
    """Shared by get_job_details() and search_jobs_with_details() — assumes
    the caller already has an open, logged-in `page`. Returns
    {job_id, url, description, meta}."""
    page.goto(job_url, wait_until="domcontentloaded")
    jittered_wait()

    description = ""
    if page.locator(JOB_DESCRIPTION_SELECTOR).first.count():
        description = page.locator(JOB_DESCRIPTION_SELECTOR).first.inner_text().strip()
    else:
        # JOB_DESCRIPTION_SELECTOR is a CSS-module hash that's already
        # broken once before (see the comment on the selector itself) --
        # log every occurrence so a fresh break is visible immediately
        # instead of only showing up later as a cluster of empty
        # descriptions / fit_score=0 rows in jobs.db with no obvious cause.
        log.warning(
            "Job %s: description selector matched nothing - job will have an empty description.",
            _job_id_from_url(job_url),
        )

    meta = ""
    if page.locator(JOB_META_SELECTOR).first.count():
        meta = page.locator(JOB_META_SELECTOR).first.inner_text().strip()

    return {
        "job_id": _job_id_from_url(job_url),
        "url": job_url,
        "description": description,
        "meta": meta,
    }


def search_jobs(keywords: str, location: str = "", max_pages: int = 1) -> list[dict]:
    """Returns [{job_id, title, company, url}, ...] for the given search.
    Opens and closes its own browser context — for the combined,
    single-session search-then-details path used by run_search_cycle(),
    see search_jobs_with_details() instead."""
    playwright, context = get_browser_context()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)
        return _scrape_search_results(page, keywords, location, max_pages)
    finally:
        context.close()
        playwright.stop()


def get_job_details(job_url: str) -> dict:
    """Opens a job page and extracts description + metadata, ready for
    storage.upsert_job(). Opens and closes its own browser context — for
    the combined, single-session search-then-details path used by
    run_search_cycle(), see search_jobs_with_details() instead."""
    playwright, context = get_browser_context()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)
        return _scrape_job_details(page, job_url)
    finally:
        context.close()
        playwright.stop()


def search_jobs_with_details(
    keywords: str, location: str = "", max_pages: int = 1, skip_job_ids: set | None = None
) -> list[dict]:
    """Combines search_jobs() + a get_job_details() call per result into a
    SINGLE browser session: one context/page opened once, one
    ensure_logged_in() call, reused for the search-results scrape AND every
    job's detail fetch. Returns [{job_id, title, company, url, description}, ...]
    -- the exact shape run_search_cycle() previously assembled by hand from
    search_jobs() + get_job_details(), so storage.upsert_job(job) can be
    called directly on each result with no merging at the call site (`meta`
    is deliberately dropped, matching the old call site's behavior, which
    only ever pulled `description` out of get_job_details()'s return value).

    `skip_job_ids`, if given, is a set of job_ids to skip the detail fetch
    for entirely -- added 2026-09-06 so run_search_cycle() can pass in
    every job_id that already has a stored, non-empty description
    (storage.get_job_ids_with_description()), avoiding a pointless re-fetch
    of a job already known from a prior search. This module never imports
    storage directly (see FLOW.md's module-boundary rule) -- the caller
    supplies the set instead, the same pattern already used for
    `answer_fn`. A skipped job's result dict has NO "description" key at
    all (not an empty string) -- storage.upsert_job() only SETs the keys
    present in the dict it's given, so omitting the key entirely means the
    existing stored description is left untouched, not overwritten with
    an empty one.

    Added 2026-09-05 to fix a known inefficiency documented in FLOW.md:
    the old per-job get_job_details() call opened a fresh
    launch_persistent_context() (reloading the whole Chrome profile) AND
    re-ran ensure_logged_in() (a full extra navigation to the naukri.com
    homepage) before every single job's detail fetch. Confirmed live
    2026-09-05: a 20-job search took ~4.5 minutes this way, each job
    visibly gated behind a repeated "Already logged in." homepage
    round-trip -- see DECISIONS.md for the before/after timing.

    A single job's detail fetch failing (a dead link, a timeout, a broken
    selector) is caught here and degrades that one job to an empty
    description rather than losing the whole batch -- this matters more
    now than it did for the old per-job-context design: everything used to
    be returned in one list built up over the whole function, so an
    uncaught exception here would previously have lost every job's search
    result too (job_id/title/company/url included), not just the failed
    job's description. The old per-job orchestrator loop didn't have this
    problem only as a side effect of persisting each job immediately after
    its own successful fetch; folding the loop in here needed this guard to
    not be a net regression.
    """
    playwright, context = get_browser_context()
    results: list[dict] = []
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)

        jobs = _scrape_search_results(page, keywords, location, max_pages)
        empty_description_count = 0
        skip_job_ids = skip_job_ids or set()

        for idx, job in enumerate(jobs, start=1):
            if job["job_id"] in skip_job_ids:
                log.info(
                    "Job %d/%d: %s already has a stored description, skipping detail fetch.",
                    idx,
                    len(jobs),
                    job["job_id"],
                )
                results.append(dict(job))  # no "description" key -- see docstring
                continue

            try:
                details = _scrape_job_details(page, job["url"])
                description = details.get("description", "")
            except Exception:
                log.error(
                    "Job %s (%s): failed to fetch details - keeping the job with an "
                    "empty description rather than losing the whole batch.",
                    job["job_id"],
                    job.get("title", ""),
                    exc_info=True,
                )
                description = ""
            if not description:
                empty_description_count += 1
            results.append({**job, "description": description})
            # With the whole loop now inside this function (see the docstring
            # above), the caller gets nothing back until every job is done --
            # unlike the old per-job get_job_details() call, which let
            # run_search_cycle() log progress after each one. Logged here
            # instead so a run in progress is still visible, not silent until
            # the very end.
            log.info("Fetched details for job %d/%d: %s (%s)", idx, len(jobs), job["job_id"], job.get("title", ""))

        if empty_description_count:
            # A cluster of these in one run is the visible symptom of a
            # broken JOB_DESCRIPTION_SELECTOR (or a genuine wave of dead
            # links) -- surfaced here as one clear summary line instead of
            # only being discoverable later by noticing several fit_score=0
            # rows in jobs.db with no obvious shared cause.
            log.warning(
                "%d/%d job(s) had an empty description this run - if that's "
                "unexpected, JOB_DESCRIPTION_SELECTOR may need re-verifying "
                "against a live job page.",
                empty_description_count,
                len(jobs),
            )
    finally:
        context.close()
        playwright.stop()

    return results


# --- Write actions below are stubbed for Phase 1. Dry-run/pause/cap gates
# are enforced here AND in orchestrator.py — belt and suspenders on the
# actions that submit something on the user's behalf. ---


def _radio_label(page: Page, radio) -> str:
    rid = radio.get_attribute("id") or ""
    if rid:
        label = page.locator(f"label[for='{rid}']")
        if label.count():
            return label.first.inner_text().strip()
    return (radio.get_attribute("value") or "").strip()


def _select_radio_option(page: Page, radio) -> bool:
    """The raw <input type=radio> is a custom-styled control that Playwright
    reports as outside the viewport / not actionable even though it's on
    screen — live-verified failure: `.check()` timed out waiting for
    actionability on a real, visible radio. The associated <label for=id>
    is the actual clickable surface a real user interacts with; click that
    instead. Falls back to a forced check() (bypassing actionability
    checks) only if no label exists."""
    rid = radio.get_attribute("id") or ""
    if rid:
        label = page.locator(f"label[for='{rid}']")
        if label.count():
            label.first.click()
            return True
    radio.check(force=True)
    return True


def _click_send_button(page: Page, job_id: str) -> bool:
    """Waits briefly for the send button to become enabled (it's disabled
    until an answer is actually entered/selected), then clicks it. Returns
    False if it never enables, so callers can bail to manual review instead
    of clicking a stale/disabled control."""
    for _ in range(5):
        send_btn = page.locator(CHATBOT_SEND_BUTTON_SELECTOR)
        if send_btn.count():
            send_btn.first.click()
            return True
        time.sleep(1)
    log.warning("Job %s: send button never became enabled — manual review.", job_id)
    return False


# Naukri's own escape hatch for a single question it can't get an answer
# to — a chip literally worded "Skip this question", rendered with the
# exact same class as every other chip (chatbot_Chip.chipInRow.chipItem).
# Live-verified 2026-09-01: it can coexist with a free-text input
# (chatbot_InputContainer) on the SAME question — i.e. its presence
# doesn't mean "this is a chip-choice question," it means "you may skip
# this one specific question and the conversation continues," regardless
# of what kind of question it is. This is not a guess or a fabricated
# answer — it's Naukri's own sanctioned "move on without answering"
# action, which is exactly why it's safe to use automatically even though
# a fabricated answer would not be. See DECISIONS.md and
# _decide_chatbot_turn()'s skip_action() below.
SKIP_QUESTION_CHIP_TEXT = "skip this question"


def _read_chatbot_turn_state(page: Page) -> dict:
    """All the Playwright/DOM reads needed to decide one turn of the
    screening chatbot, bundled into a plain-data snapshot. Kept separate
    from _decide_chatbot_turn() (added 2026-09-05, see DECISIONS.md) so the
    actual decision logic can run against a hand-built dict in tests, with
    no Page/browser involved at all.

    "question" is "" when has_messages is False (no question to read yet).
    chip_texts is the FULL, unfiltered chip list (skip chip included, if
    present) — filtering it down to "real" choices is _decide_chatbot_turn's
    job, not this function's, since which chip is the skip chip is itself
    part of the decision, not just the reading.
    """
    applied_banner_visible = page.locator(CHATBOT_APPLIED_BANNER_SELECTOR).count() > 0

    messages = page.locator(CHATBOT_MESSAGE_SELECTOR)
    has_messages = messages.count() > 0
    question = messages.last.inner_text().strip() if has_messages else ""

    chips = page.locator(CHATBOT_CHIP_SELECTOR)
    chip_texts = [chips.nth(i).inner_text().strip() for i in range(chips.count())]

    radios = page.locator(CHATBOT_RADIO_SELECTOR)
    radio_options = [_radio_label(page, radios.nth(i)) for i in range(radios.count())]

    return {
        "applied_banner_visible": applied_banner_visible,
        "has_messages": has_messages,
        "question": question,
        "chip_texts": chip_texts,
        "radio_options": radio_options,
        "text_input_present": page.locator(CHATBOT_TEXT_INPUT_SELECTOR).count() > 0,
        "file_input_present": page.locator(CHATBOT_FILE_INPUT_SELECTOR).count() > 0,
        # Checked here, not inside _decide_chatbot_turn(), so that function
        # stays free of filesystem access too, not just Playwright.
        "resume_pdf_exists": Path(config.RESUME_PDF_PATH).is_file(),
    }


def _decide_chatbot_turn(state: dict, answer_fn) -> dict:
    """Pure decision logic for one turn of Naukri's screening chatbot — no
    Playwright, no filesystem access, no logging. `state` comes from
    _read_chatbot_turn_state(); `answer_fn` is the exact same callable
    _handle_screening_chatbot() already receives (a plain, deterministic
    stub in tests; a real LLM call via scoring.draft_screening_answer in
    production — this function doesn't know or care which, matching the
    module-boundary rule in FLOW.md).

    This is the actual bug-prone logic that's caused every real screening-
    chatbot incident on record (see DECISIONS.md): the substantive-chip-vs-
    skip-chip filtering, the chip/radio/text/file check ORDER (file last,
    on purpose — it's chat-widget furniture persistently present in the
    DOM, not a per-question signal; checking it first was the original
    bug), and the can't-answer/no-match -> try-skip -> else-manual-review
    fallback repeated across chip/radio/text. Extracted 2026-09-05
    specifically so tests/test_naukri_client.py can exercise all of it
    without a browser or Ollama.

    Returns an action dict; see _handle_screening_chatbot() for how each
    "type" is carried out and logged:
      {"type": "applied"}
      {"type": "no_messages"}
      {"type": "click_chip", "index": int, "qa_entry": {...}}
      {"type": "click_skip_chip", "index": int, "qa_entry": {...}}
      {"type": "manual_review", "detail": str, "reason": str, "qa_entry": {...}, "answer"?: str}
      {"type": "select_radio", "index": int, "qa_entry": {...}}
      {"type": "fill_text", "text": str, "qa_entry": {...}}
      {"type": "attempt_resume_upload"}
    "qa_entry" is always {"question", "answer", "options"} ready to append
    to qa_log as-is.
    """
    if state["applied_banner_visible"]:
        return {"type": "applied"}

    if not state["has_messages"]:
        return {"type": "no_messages"}

    question = state["question"]
    chip_texts = state["chip_texts"]
    substantive_chips = [t for t in chip_texts if t.lower() != SKIP_QUESTION_CHIP_TEXT]

    def skip_action(options):
        for i, t in enumerate(chip_texts):
            if t.lower() == SKIP_QUESTION_CHIP_TEXT:
                return {
                    "type": "click_skip_chip",
                    "index": i,
                    "qa_entry": {
                        "question": question,
                        "answer": "(skipped via Naukri's 'Skip this question')",
                        "options": options,
                    },
                }
        return None

    def cant_answer(options):
        return skip_action(options) or {
            "type": "manual_review",
            "detail": "no_answer",
            "reason": "questionnaire_required_manual_review",
            "qa_entry": {"question": question, "answer": None, "options": options},
        }

    if substantive_chips:
        answer = answer_fn(question, substantive_chips)
        if answer is None:
            return cant_answer(substantive_chips)
        for i, t in enumerate(chip_texts):
            if t == answer:
                return {
                    "type": "click_chip",
                    "index": i,
                    "qa_entry": {"question": question, "answer": answer, "options": substantive_chips},
                }
        return skip_action(substantive_chips) or {
            "type": "manual_review",
            "detail": "no_chip_match",
            "reason": "questionnaire_required_manual_review",
            "answer": answer,
            "qa_entry": {"question": question, "answer": None, "options": substantive_chips},
        }

    radio_options = state["radio_options"]
    if radio_options:
        answer = answer_fn(question, radio_options)
        if answer is None:
            return cant_answer(radio_options)
        for i, opt in enumerate(radio_options):
            if opt == answer:
                return {
                    "type": "select_radio",
                    "index": i,
                    "qa_entry": {"question": question, "answer": answer, "options": radio_options},
                }
        return skip_action(radio_options) or {
            "type": "manual_review",
            "detail": "no_radio_match",
            "reason": "questionnaire_required_manual_review",
            "answer": answer,
            "qa_entry": {"question": question, "answer": None, "options": radio_options},
        }

    if state["text_input_present"]:
        answer = answer_fn(question, None)
        if answer is None:
            return cant_answer(None)
        return {"type": "fill_text", "text": answer, "qa_entry": {"question": question, "answer": answer, "options": None}}

    if state["file_input_present"]:
        # No skip check here, deliberately — matches the original behavior:
        # a skip chip coexisting with the file uploader was never
        # considered, since every real question seen so far has offered a
        # skip chip alongside chips/radio/text, never alongside the
        # uploader specifically.
        if not state["resume_pdf_exists"]:
            return {
                "type": "manual_review",
                "detail": "resume_pdf_not_found",
                "reason": "resume_pdf_not_found_manual_review",
                "qa_entry": {"question": question, "answer": None, "options": None},
            }
        return {"type": "attempt_resume_upload"}

    return skip_action(None) or {
        "type": "manual_review",
        "detail": "no_mechanism",
        "reason": "chatbot_state_unrecognized_manual_review",
        "qa_entry": {"question": question, "answer": None, "options": None},
    }


_MANUAL_REVIEW_LOG_MESSAGES = {
    "no_answer": "Job %s: couldn't confidently answer %r from resume - manual review.",
    "no_chip_match": "Job %s: drafted answer %r didn't match any chip - manual review.",
    "no_radio_match": "Job %s: drafted answer %r didn't match any radio option - manual review.",
    "no_mechanism": "Job %s: chatbot showed a message with no recognized input mechanism - manual review.",
    "resume_pdf_not_found": "Job %s: configured resume PDF %s not found - can't upload, manual review.",
}


def _handle_screening_chatbot(page: Page, job_id: str, answer_fn) -> ApplyResult:
    """Walks Naukri's screening-question chatbot turn by turn. Only called
    when config.AUTO_ANSWER_SCREENING_QUESTIONS is True (checked by the
    caller). `answer_fn(question: str, options: list[str] | None) -> str |
    None` drafts each answer — None means "can't confidently answer," never
    fabricated. All actual decision-making (which input mechanism to use,
    whether a drafted answer matches, when to fall back to Naukri's own
    "Skip this question" chip vs. stopping for manual review) lives in
    _decide_chatbot_turn() — this function just reads DOM state each turn
    (_read_chatbot_turn_state), asks for a decision, and carries out
    whatever action comes back: the Playwright clicks/fills, the
    resume-upload's own post-upload failure check (the one place that
    genuinely needs to read fresh DOM state again mid-turn, after an
    action — not folded into the pure decision function, unlike everything
    else), and all logging.

    Every return dict includes "qa_log": a list of {"question": str,
    "answer": str | None, "options": list[str] | None} covering every
    question actually seen this walk — answer is None only for the question
    that finally stopped the walk (no skip option available either), and
    options is the exact choice list offered for that question (None for
    free-text questions with no fixed choices), so a surprising answer is
    diagnosable after the fact without needing to re-visit a live page.
    Consumed by excel_log.log_application() for the human-readable audit
    trail."""
    qa_log: list[dict] = []

    for turn in range(MAX_CHATBOT_TURNS):
        jittered_wait()

        state = _read_chatbot_turn_state(page)
        action = _decide_chatbot_turn(state, answer_fn)
        action_type = action["type"]

        if action_type == "applied":
            log.info("Job %s: chatbot flow completed, application submitted.", job_id)
            return _apply_result(True, "applied", qa_log=qa_log)

        if action_type == "no_messages":
            log.warning("Job %s: chatbot has no messages, can't determine the question.", job_id)
            return _apply_result(False, "chatbot_state_unrecognized_manual_review", qa_log=qa_log)

        if action_type == "click_chip":
            page.locator(CHATBOT_CHIP_SELECTOR).nth(action["index"]).click()
            qa_log.append(action["qa_entry"])
            continue

        if action_type == "click_skip_chip":
            log.info(
                "Job %s: couldn't confidently answer %r - using Naukri's own Skip option.",
                job_id,
                state["question"],
            )
            page.locator(CHATBOT_CHIP_SELECTOR).nth(action["index"]).click()
            qa_log.append(action["qa_entry"])
            continue

        if action_type == "manual_review":
            log_args = (job_id, action["answer"]) if "answer" in action else (job_id,)
            if action["detail"] == "resume_pdf_not_found":
                log_args = (job_id, config.RESUME_PDF_PATH)
            elif action["detail"] == "no_answer":
                log_args = (job_id, state["question"])
            log.warning(_MANUAL_REVIEW_LOG_MESSAGES[action["detail"]], *log_args)
            qa_log.append(action["qa_entry"])
            return _apply_result(False, action["reason"], qa_log=qa_log)

        if action_type == "select_radio":
            _select_radio_option(page, page.locator(CHATBOT_RADIO_SELECTOR).nth(action["index"]))
            qa_log.append(action["qa_entry"])
            if not _click_send_button(page, job_id):
                return _apply_result(False, "chatbot_state_unrecognized_manual_review", qa_log=qa_log)
            continue

        if action_type == "fill_text":
            page.locator(CHATBOT_TEXT_INPUT_SELECTOR).first.fill(action["text"])
            jittered_wait()
            qa_log.append(action["qa_entry"])
            if not _click_send_button(page, job_id):
                return _apply_result(False, "chatbot_state_unrecognized_manual_review", qa_log=qa_log)
            continue

        if action_type == "attempt_resume_upload":
            # The one action whose outcome genuinely can't be decided ahead
            # of time — whether the upload succeeded is only knowable by
            # reading the DOM again AFTER performing it, so this stays
            # imperative rather than folded into _decide_chatbot_turn().
            log.info("Job %s: no chip/radio/text mechanism found, uploading resume as last resort.", job_id)
            page.locator(CHATBOT_FILE_INPUT_SELECTOR).first.set_input_files(config.RESUME_PDF_PATH)
            jittered_wait()
            # Naukri's own error text on a rejected upload (live-verified
            # once already — see DECISIONS.md's file-input-detection-order
            # entry). Checking for it means a rejected upload is flagged
            # for review instead of silently treated as answered, which
            # would otherwise let the loop re-show the same question up to
            # MAX_CHATBOT_TURNS times before giving up.
            if page.locator("text=/file upload was unsuccessful/i").count() > 0:
                log.warning("Job %s: resume upload rejected by Naukri - manual review.", job_id)
                qa_log.append({"question": state["question"], "answer": "(upload failed)", "options": None})
                return _apply_result(False, "resume_upload_failed_manual_review", qa_log=qa_log)
            qa_log.append({"question": state["question"], "answer": "(uploaded resume)", "options": None})
            continue

        raise AssertionError(f"unreachable: unknown chatbot action type {action_type!r}")

    log.warning("Job %s: chatbot exceeded %d turns - manual review.", job_id, MAX_CHATBOT_TURNS)
    return _apply_result(False, "questionnaire_too_long_manual_review", qa_log=qa_log)


def _capture_external_apply_url(page: Page, context, job_id: str) -> str | None:
    """Clicks "Apply on company site" to capture where it goes — never
    fills in or submits anything on the destination page, and closes it
    immediately after reading the URL. Only called when
    config.CAPTURE_EXTERNAL_APPLY_URLS is True; the caller is responsible
    for that gate.

    IMPORTANT, live-verified 2026-09-01: this click marks the job "Applied"
    in the user's own Naukri account/history (same green "Applied" pill as
    a real Naukri-native apply), even though nothing is actually submitted
    to the external site. Not a harmless read — see config.py's comment on
    CAPTURE_EXTERNAL_APPLY_URLS and the DECISIONS.md entry for how this was
    discovered.

    The button has no static href/onclick (destination is set by JS on
    click) and always opened a NEW TAB in the one case tested, not a
    same-tab navigation — this still checks for a same-tab URL change as a
    fallback in case some jobs behave differently, since that wasn't
    exhaustively tested across every job."""
    btn = page.locator(EXTERNAL_APPLY_SELECTOR).first
    original_url = page.url
    try:
        with context.expect_page(timeout=8_000) as new_page_info:
            btn.click()
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
        url = new_page.url
        new_page.close()
        return url
    except Exception as e:
        jittered_wait()
        if page.url != original_url:
            return page.url
        log.warning("Job %s: couldn't capture external apply URL (%s).", job_id, e)
        return None


def apply_to_job(job_id: str, job_url: str, answer_fn=None) -> ApplyResult:
    """Returns an ApplyResult (see that class) — every return path below is
    built via _apply_result() so all four keys are always present, added
    2026-09-06 (see DECISIONS.md; previously "external_url" appeared in
    just 1 of this function's 9 return statements).

    `answer_fn`, if given, is passed straight to _handle_screening_chatbot
    and is only ever invoked when config.AUTO_ANSWER_SCREENING_QUESTIONS is
    True — orchestrator.py is responsible for deciding whether to pass one
    at all, keeping this module Playwright-only and the LLM call in
    scoring.py, matching the module boundary described in FLOW.md.

    Conservative by design: any job without a direct in-site Apply button,
    or any post-click state this code doesn't clearly recognize as success,
    is left alone rather than guessed at. A missed application is
    recoverable by hand; a wrong click on someone's behalf is not."""
    if config.PAUSED:
        log.warning("PAUSED is set — skipping apply_to_job(%s).", job_id)
        return _apply_result(False, "paused")

    if config.DRY_RUN:
        log.info("[DRY RUN] would apply to job %s (%s)", job_id, job_url)
        return _apply_result(False, "dry_run")

    log.warning("[LIVE] Applying for real to job %s (%s) - DRY_RUN is False.", job_id, job_url)

    playwright, context = get_browser_context()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)

        page.goto(job_url, wait_until="domcontentloaded")
        jittered_wait()

        apply_btn = page.locator(APPLY_BUTTON_SELECTOR).first
        if not apply_btn.count():
            if page.locator(EXTERNAL_APPLY_SELECTOR).first.count():
                external_url = None
                if config.CAPTURE_EXTERNAL_APPLY_URLS:
                    external_url = _capture_external_apply_url(page, context, job_id)
                    log.info("Job %s is external-apply-only. Captured URL: %s", job_id, external_url)
                else:
                    log.info("Job %s is external-apply-only, skipping (not clicked).", job_id)
                return _apply_result(False, "external_apply_not_supported", external_url=external_url)
            log.warning("No Apply button found for job %s.", job_id)
            return _apply_result(False, "apply_button_not_found")

        btn_text = apply_btn.inner_text().strip().lower()
        if "applied" in btn_text:
            log.info("Job %s already shows Applied.", job_id)
            return _apply_result(False, "already_applied")

        apply_btn.click()
        jittered_wait()

        if page.locator(QUESTIONNAIRE_MODAL_SELECTOR).count() > 0:
            if config.AUTO_ANSWER_SCREENING_QUESTIONS and answer_fn is not None:
                return _handle_screening_chatbot(page, job_id, answer_fn)
            log.warning(
                "Job %s opened a screening-question modal after Apply — "
                "leaving it for manual review, not answering on your behalf.",
                job_id,
            )
            return _apply_result(False, "questionnaire_required_manual_review")

        post_click_text = page.locator(APPLY_BUTTON_SELECTOR).first
        if post_click_text.count() and "applied" in post_click_text.inner_text().strip().lower():
            log.info("Applied to job %s.", job_id)
            return _apply_result(True, "applied")

        log.warning(
            "Job %s: clicked Apply but couldn't confirm success — needs manual review.",
            job_id,
        )
        return _apply_result(False, "apply_outcome_unclear_manual_review")
    finally:
        context.close()
        playwright.stop()


def get_inbox_messages():
    raise NotImplementedError("Inbox polling not implemented yet (Phase 3).")


def send_reply(message_id: str, text: str):
    """Never called automatically by any run loop. Sending a reply is
    always a deliberate, manual call made after human review of the draft
    stored via storage.save_draft_reply()."""
    raise NotImplementedError("Reply sending not implemented yet (Phase 3).")
