"""All Playwright/DOM logic for naukri.com lives here and nowhere else.
Naukri's markup changes periodically — when selectors break, this is the
only file that should need to change.
"""

import logging
import random
import time
from pathlib import Path

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


def search_jobs(keywords: str, location: str = "", max_pages: int = 1) -> list[dict]:
    """Returns [{job_id, title, company, url}, ...] for the given search."""
    playwright, context = get_browser_context()
    results: list[dict] = []
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)

        slug = keywords.strip().lower().replace(" ", "-")
        location_suffix = f"-in-{location.strip().lower().replace(' ', '-')}" if location else ""
        search_url = SEARCH_URL_TEMPLATE.format(keywords=slug, location_suffix=location_suffix)

        page.goto(search_url, wait_until="domcontentloaded")
        jittered_wait()

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
    finally:
        context.close()
        playwright.stop()

    return results


def get_job_details(job_url: str) -> dict:
    """Opens a job page and extracts description + metadata, ready for
    storage.upsert_job()."""
    playwright, context = get_browser_context()
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)

        page.goto(job_url, wait_until="domcontentloaded")
        jittered_wait()

        description = ""
        if page.locator(JOB_DESCRIPTION_SELECTOR).first.count():
            description = page.locator(JOB_DESCRIPTION_SELECTOR).first.inner_text().strip()

        meta = ""
        if page.locator(JOB_META_SELECTOR).first.count():
            meta = page.locator(JOB_META_SELECTOR).first.inner_text().strip()

        return {
            "job_id": _job_id_from_url(job_url),
            "url": job_url,
            "description": description,
            "meta": meta,
        }
    finally:
        context.close()
        playwright.stop()


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


SKIP_QUESTION_CHIP_TEXT = "skip this question"


def _find_skip_chip(page: Page):
    """Naukri's own escape hatch for a single question it can't get an
    answer to — a chip literally worded "Skip this question", rendered
    with the exact same class as every other chip
    (chatbot_Chip.chipInRow.chipItem). Live-verified 2026-09-01: it can
    coexist with a free-text input (chatbot_InputContainer) on the SAME
    question — i.e. its presence doesn't mean "this is a chip-choice
    question," it means "you may skip this one specific question and the
    conversation continues," regardless of what kind of question it is.
    Returns the chip's locator, or None if not present this turn."""
    chips = page.locator(CHATBOT_CHIP_SELECTOR)
    for i in range(chips.count()):
        if chips.nth(i).inner_text().strip().lower() == SKIP_QUESTION_CHIP_TEXT:
            return chips.nth(i)
    return None


def _try_skip_question(page: Page, job_id: str, question: str, qa_log: list, options=None) -> bool:
    """Called only when answer_fn couldn't produce a usable answer. Clicks
    Naukri's own "Skip this question" chip if one is present THIS turn,
    letting the conversation continue to the next question instead of
    abandoning the whole application. This is not a guess or a fabricated
    answer — it's Naukri's own sanctioned "move on without answering"
    action, which is exactly why it's safe to use automatically even though
    a fabricated answer would not be. Returns True if it skipped (caller
    should `continue` the loop), False if no skip option existed (caller
    falls through to manual review as before)."""
    skip_chip = _find_skip_chip(page)
    if skip_chip is None:
        return False
    log.info("Job %s: couldn't confidently answer %r — using Naukri's own Skip option.", job_id, question)
    skip_chip.click()
    qa_log.append(
        {
            "question": question,
            "answer": "(skipped via Naukri's 'Skip this question')",
            "options": options,
        }
    )
    return True


def _handle_screening_chatbot(page: Page, job_id: str, answer_fn) -> dict:
    """Walks Naukri's screening-question chatbot turn by turn. Only called
    when config.AUTO_ANSWER_SCREENING_QUESTIONS is True (checked by the
    caller). `answer_fn(question: str, options: list[str] | None) -> str |
    None` drafts each answer — None means "can't confidently answer." That
    no longer always stops the walk: if Naukri offers its own "Skip this
    question" chip this turn (see _try_skip_question), that's used instead,
    and the conversation continues to the next question. Only when no skip
    option exists does an unanswerable question stop the walk for manual
    review — the walk never fabricates an answer either way.

    Per turn, the "real" input mechanism is chosen from chip options with
    the skip chip excluded — a chip list that, once skip is excluded, is
    empty means this isn't a genuine chip-choice question (the skip chip
    can coexist with a text input on the same question; see
    _find_skip_chip). Checked in that order: substantive chips, radio
    buttons, free-text input, and only last — when none of those matched —
    the file uploader. That last-resort ordering matters:
    CHATBOT_FILE_INPUT_SELECTOR matches an <input type=file> that's
    persistently present in the drawer regardless of the current question
    (chat-widget furniture, not a per-question signal). Checking it first
    was the original bug — see the comment above the selector constants for
    what that looked like live.

    Chip and radio answers submit differently: a chip click appeared to
    auto-submit in the one flow that exercised it; a radio selection needs
    an explicit send-button click afterward (checked separately, via
    _click_send_button, since the button starts disabled until something is
    selected).

    Every return dict includes "qa_log": a list of {"question": str,
    "answer": str | None, "options": list[str] | None} covering every
    question actually seen this walk — answer is None only for the question
    that finally stopped the walk (no skip option available either), and
    options is the exact choice list offered for that question (None for
    free-text questions with no fixed choices), so a surprising answer is
    diagnosable after the fact without needing to re-visit a live page —
    added after a real answer ("Serving Notice Period" instead of an
    expected "3 Months") couldn't be explained afterward because only the
    chosen answer had been logged, not what it was chosen from. Consumed by
    excel_log.log_application() for the human-readable audit trail."""
    qa_log: list[dict] = []

    for turn in range(MAX_CHATBOT_TURNS):
        jittered_wait()

        if page.locator(CHATBOT_APPLIED_BANNER_SELECTOR).count() > 0:
            log.info("Job %s: chatbot flow completed, application submitted.", job_id)
            return {"applied": True, "reason": "applied", "qa_log": qa_log}

        messages = page.locator(CHATBOT_MESSAGE_SELECTOR)
        if not messages.count():
            log.warning("Job %s: chatbot has no messages, can't determine the question.", job_id)
            return {"applied": False, "reason": "chatbot_state_unrecognized_manual_review", "qa_log": qa_log}
        question = messages.last.inner_text().strip()

        chips = page.locator(CHATBOT_CHIP_SELECTOR)
        chip_count = chips.count()
        chip_texts = [chips.nth(i).inner_text().strip() for i in range(chip_count)]
        substantive_chips = [t for t in chip_texts if t.lower() != SKIP_QUESTION_CHIP_TEXT]

        if substantive_chips:
            answer = answer_fn(question, substantive_chips)
            if answer is None:
                if _try_skip_question(page, job_id, question, qa_log, options=substantive_chips):
                    continue
                log.warning(
                    "Job %s: couldn't confidently answer %r from resume — manual review.", job_id, question
                )
                qa_log.append({"question": question, "answer": None, "options": substantive_chips})
                return {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": qa_log}
            matched = False
            for i in range(chip_count):
                if chip_texts[i] == answer:
                    chips.nth(i).click()
                    matched = True
                    break
            if not matched:
                if _try_skip_question(page, job_id, question, qa_log, options=substantive_chips):
                    continue
                log.warning("Job %s: drafted answer %r didn't match any chip — manual review.", job_id, answer)
                qa_log.append({"question": question, "answer": None, "options": substantive_chips})
                return {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": qa_log}
            qa_log.append({"question": question, "answer": answer, "options": substantive_chips})
            continue

        radios = page.locator(CHATBOT_RADIO_SELECTOR)
        radio_count = radios.count()
        if radio_count > 0:
            options = [_radio_label(page, radios.nth(i)) for i in range(radio_count)]
            answer = answer_fn(question, options)
            if answer is None:
                if _try_skip_question(page, job_id, question, qa_log, options=options):
                    continue
                log.warning(
                    "Job %s: couldn't confidently answer %r from resume — manual review.", job_id, question
                )
                qa_log.append({"question": question, "answer": None, "options": options})
                return {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": qa_log}
            matched = False
            for i in range(radio_count):
                if options[i] == answer:
                    _select_radio_option(page, radios.nth(i))
                    matched = True
                    break
            if not matched:
                if _try_skip_question(page, job_id, question, qa_log, options=options):
                    continue
                log.warning("Job %s: drafted answer %r didn't match any radio option — manual review.", job_id, answer)
                qa_log.append({"question": question, "answer": None, "options": options})
                return {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": qa_log}
            if not _click_send_button(page, job_id):
                qa_log.append({"question": question, "answer": answer, "options": options})
                return {"applied": False, "reason": "chatbot_state_unrecognized_manual_review", "qa_log": qa_log}
            qa_log.append({"question": question, "answer": answer, "options": options})
            continue

        text_input = page.locator(CHATBOT_TEXT_INPUT_SELECTOR)
        if text_input.count() > 0:
            answer = answer_fn(question, None)
            if answer is None:
                if _try_skip_question(page, job_id, question, qa_log):
                    continue
                log.warning(
                    "Job %s: couldn't confidently answer %r from resume — manual review.", job_id, question
                )
                qa_log.append({"question": question, "answer": None, "options": None})
                return {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": qa_log}
            text_input.first.fill(answer)
            jittered_wait()
            if not _click_send_button(page, job_id):
                qa_log.append({"question": question, "answer": answer, "options": None})
                return {"applied": False, "reason": "chatbot_state_unrecognized_manual_review", "qa_log": qa_log}
            qa_log.append({"question": question, "answer": answer, "options": None})
            continue

        file_input = page.locator(CHATBOT_FILE_INPUT_SELECTOR)
        if file_input.count() > 0:
            # config.RESUME_PDF_PATH is a hardcoded absolute path outside
            # this project's tree — nothing enforced it exists before
            # handing it to set_input_files(), which raises if it doesn't.
            # Uncaught, that exception used to propagate out of this whole
            # function (and, before the try/except added around
            # apply_to_job() in orchestrator.py, out of the entire apply
            # cycle). Checked here instead so a stale/moved resume file
            # degrades to manual review for this one job, not a crash.
            if not Path(config.RESUME_PDF_PATH).is_file():
                log.warning(
                    "Job %s: configured resume PDF %s not found - can't upload, manual review.",
                    job_id,
                    config.RESUME_PDF_PATH,
                )
                qa_log.append({"question": question, "answer": None, "options": None})
                return {"applied": False, "reason": "resume_pdf_not_found_manual_review", "qa_log": qa_log}

            log.info("Job %s: no chip/radio/text mechanism found, uploading resume as last resort.", job_id)
            file_input.first.set_input_files(config.RESUME_PDF_PATH)
            jittered_wait()
            # Naukri's own error text on a rejected upload (live-verified
            # once already — see DECISIONS.md's file-input-detection-order
            # entry, where this exact message appeared after a bad upload
            # attempt). Checking for it here means a rejected upload is
            # flagged for review instead of silently treated as answered,
            # which would otherwise let the loop re-show the same question
            # up to MAX_CHATBOT_TURNS times before giving up.
            if page.locator("text=/file upload was unsuccessful/i").count() > 0:
                log.warning("Job %s: resume upload rejected by Naukri - manual review.", job_id)
                qa_log.append({"question": question, "answer": "(upload failed)", "options": None})
                return {"applied": False, "reason": "resume_upload_failed_manual_review", "qa_log": qa_log}
            qa_log.append({"question": question, "answer": "(uploaded resume)", "options": None})
            continue

        if _try_skip_question(page, job_id, question, qa_log):
            continue

        log.warning(
            "Job %s: chatbot showed a message with no recognized input mechanism — manual review.", job_id
        )
        qa_log.append({"question": question, "answer": None, "options": None})
        return {"applied": False, "reason": "chatbot_state_unrecognized_manual_review", "qa_log": qa_log}

    log.warning("Job %s: chatbot exceeded %d turns — manual review.", job_id, MAX_CHATBOT_TURNS)
    return {"applied": False, "reason": "questionnaire_too_long_manual_review", "qa_log": qa_log}


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


def apply_to_job(job_id: str, job_url: str, answer_fn=None) -> dict:
    """Returns {"applied": bool, "reason": str, "qa_log": list,
    "external_url": str | None}. "external_url" is only ever set for
    reason="external_apply_not_supported" — every other path either never
    reaches an external-apply button or returns None for it via .get()
    (not added as an explicit key everywhere, unlike qa_log, since it's
    only ever meaningful in that one branch). "applied" is
    only ever True for an actual, real submission — DRY_RUN always returns
    False with reason="dry_run" even though that's the expected/successful
    dry-run outcome; callers (orchestrator.run_apply_cycle) key off `reason`,
    not just `applied`, to decide what counts as an attempt for audit
    logging. "qa_log" is always present (empty list when the chatbot was
    never reached) — consumed by excel_log.log_application().

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
        return {"applied": False, "reason": "paused", "qa_log": []}

    if config.DRY_RUN:
        log.info("[DRY RUN] would apply to job %s (%s)", job_id, job_url)
        return {"applied": False, "reason": "dry_run", "qa_log": []}

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
                return {
                    "applied": False,
                    "reason": "external_apply_not_supported",
                    "qa_log": [],
                    "external_url": external_url,
                }
            log.warning("No Apply button found for job %s.", job_id)
            return {"applied": False, "reason": "apply_button_not_found", "qa_log": []}

        btn_text = apply_btn.inner_text().strip().lower()
        if "applied" in btn_text:
            log.info("Job %s already shows Applied.", job_id)
            return {"applied": False, "reason": "already_applied", "qa_log": []}

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
            return {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": []}

        post_click_text = page.locator(APPLY_BUTTON_SELECTOR).first
        if post_click_text.count() and "applied" in post_click_text.inner_text().strip().lower():
            log.info("Applied to job %s.", job_id)
            return {"applied": True, "reason": "applied", "qa_log": []}

        log.warning(
            "Job %s: clicked Apply but couldn't confirm success — needs manual review.",
            job_id,
        )
        return {"applied": False, "reason": "apply_outcome_unclear_manual_review", "qa_log": []}
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
