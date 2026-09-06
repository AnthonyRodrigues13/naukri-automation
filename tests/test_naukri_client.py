"""Unit tests for naukri_client.search_jobs_with_details() -- the
2026-09-05 browser-context-reuse fix (see DECISIONS.md): one browser
context/login check for the whole search+details run, instead of a fresh
context per job. Also covers that search_jobs()/get_job_details() (the
original per-call functions) still open/close their own context each
call, unchanged, and that a single job's detail-fetch failure doesn't
lose the rest of the batch.

Playwright itself is never touched here -- get_browser_context,
ensure_logged_in, _scrape_search_results, and _scrape_job_details are all
mocked. These tests verify ORCHESTRATION (call counts, argument passing,
error containment, context cleanup), not the real DOM-scraping logic
(which needs a live page and isn't unit-testable without one).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import naukri_client

FAKE_JOBS = [
    {"job_id": "1", "title": "Job One", "company": "Co", "url": "https://example.com/1"},
    {"job_id": "2", "title": "Job Two", "company": "Co", "url": "https://example.com/2"},
    {"job_id": "3", "title": "Job Three", "company": "Co", "url": "https://example.com/3"},
]


def _fake_browser_context():
    fake_playwright = MagicMock()
    fake_context = MagicMock()
    fake_context.pages = []
    fake_context.new_page.return_value = MagicMock()
    return fake_playwright, fake_context


class SearchJobsWithDetailsTest(unittest.TestCase):
    def test_opens_exactly_one_context_and_logs_in_once(self):
        with patch.object(
            naukri_client, "get_browser_context", side_effect=_fake_browser_context
        ) as mock_get_context, patch.object(
            naukri_client, "ensure_logged_in"
        ) as mock_ensure_logged_in, patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ), patch.object(
            naukri_client,
            "_scrape_job_details",
            side_effect=lambda page, url: {"job_id": "x", "url": url, "description": f"desc for {url}", "meta": ""},
        ) as mock_scrape_details:
            results = naukri_client.search_jobs_with_details("python developer", "Pune")

        mock_get_context.assert_called_once()
        mock_ensure_logged_in.assert_called_once()
        self.assertEqual(mock_scrape_details.call_count, len(FAKE_JOBS))
        self.assertEqual(len(results), len(FAKE_JOBS))
        for job, result in zip(FAKE_JOBS, results):
            self.assertEqual(result["job_id"], job["job_id"])  # from the search-card, not the details dict
            self.assertEqual(result["url"], job["url"])
            self.assertEqual(result["description"], f"desc for {job['url']}")
        self.assertNotIn("meta", results[0])  # deliberately dropped, matches the old call site's behavior

    def test_context_closed_even_when_search_results_scrape_raises(self):
        fake_playwright, fake_context = _fake_browser_context()
        with patch.object(
            naukri_client, "get_browser_context", return_value=(fake_playwright, fake_context)
        ), patch.object(naukri_client, "ensure_logged_in"), patch.object(
            naukri_client, "_scrape_search_results", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                naukri_client.search_jobs_with_details("python developer")

        fake_context.close.assert_called_once()
        fake_playwright.stop.assert_called_once()

    def test_one_jobs_detail_fetch_failing_does_not_lose_the_batch(self):
        def fake_scrape_details(page, url):
            if url == FAKE_JOBS[1]["url"]:
                raise RuntimeError("dead link")
            return {"job_id": "x", "url": url, "description": f"desc for {url}", "meta": ""}

        with patch.object(
            naukri_client, "get_browser_context", side_effect=_fake_browser_context
        ), patch.object(naukri_client, "ensure_logged_in"), patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ), patch.object(naukri_client, "_scrape_job_details", side_effect=fake_scrape_details):
            results = naukri_client.search_jobs_with_details("python developer")

        self.assertEqual(len(results), 3)  # all three still returned, batch not lost
        self.assertEqual(results[0]["description"], f"desc for {FAKE_JOBS[0]['url']}")
        self.assertEqual(results[1]["description"], "")  # failed job: empty description, not dropped
        self.assertEqual(results[1]["job_id"], FAKE_JOBS[1]["job_id"])  # search-card info preserved
        self.assertEqual(results[2]["description"], f"desc for {FAKE_JOBS[2]['url']}")

    def test_empty_description_summary_logged_when_any_job_has_one(self):
        def fake_scrape_details(page, url):
            if url == FAKE_JOBS[1]["url"]:
                return {"job_id": "x", "url": url, "description": "", "meta": ""}
            return {"job_id": "x", "url": url, "description": f"desc for {url}", "meta": ""}

        with patch.object(
            naukri_client, "get_browser_context", side_effect=_fake_browser_context
        ), patch.object(naukri_client, "ensure_logged_in"), patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ), patch.object(naukri_client, "_scrape_job_details", side_effect=fake_scrape_details):
            with self.assertLogs(naukri_client.log, level="WARNING") as logs:
                naukri_client.search_jobs_with_details("python developer")

        self.assertTrue(any("1/3 job(s) had an empty description" in m for m in logs.output))

    def test_no_empty_description_summary_when_all_have_content(self):
        with patch.object(
            naukri_client, "get_browser_context", side_effect=_fake_browser_context
        ), patch.object(naukri_client, "ensure_logged_in"), patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ), patch.object(
            naukri_client,
            "_scrape_job_details",
            side_effect=lambda page, url: {"job_id": "x", "url": url, "description": "content", "meta": ""},
        ):
            with patch.object(naukri_client.log, "warning") as mock_warning:
                naukri_client.search_jobs_with_details("python developer")

        self.assertFalse(
            any("empty description" in str(call.args) for call in mock_warning.call_args_list)
        )

    def test_skip_job_ids_skips_detail_fetch_and_omits_description_key(self):
        # Added 2026-09-06: a job already known (stored, non-empty
        # description) shouldn't have its detail fetch re-run, and its
        # result dict must have NO "description" key at all -- not an
        # empty string, which storage.upsert_job() would treat as "set
        # description to empty", overwriting the real stored value.
        with patch.object(
            naukri_client, "get_browser_context", side_effect=_fake_browser_context
        ), patch.object(naukri_client, "ensure_logged_in"), patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ), patch.object(
            naukri_client,
            "_scrape_job_details",
            side_effect=lambda page, url: {"job_id": "x", "url": url, "description": f"desc for {url}", "meta": ""},
        ) as mock_scrape_details:
            results = naukri_client.search_jobs_with_details(
                "python developer", skip_job_ids={FAKE_JOBS[1]["job_id"]}
            )

        self.assertEqual(mock_scrape_details.call_count, 2)  # only the two NOT skipped
        self.assertNotIn("description", results[1])  # skipped job: key omitted entirely
        self.assertEqual(results[1]["job_id"], FAKE_JOBS[1]["job_id"])  # search-card info still present
        self.assertEqual(results[0]["description"], f"desc for {FAKE_JOBS[0]['url']}")
        self.assertEqual(results[2]["description"], f"desc for {FAKE_JOBS[2]['url']}")

    def test_context_closed_after_normal_completion(self):
        fake_playwright, fake_context = _fake_browser_context()
        with patch.object(
            naukri_client, "get_browser_context", return_value=(fake_playwright, fake_context)
        ), patch.object(naukri_client, "ensure_logged_in"), patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ), patch.object(
            naukri_client,
            "_scrape_job_details",
            return_value={"job_id": "x", "url": "u", "description": "d", "meta": ""},
        ):
            naukri_client.search_jobs_with_details("python developer")

        fake_context.close.assert_called_once()
        fake_playwright.stop.assert_called_once()


class BackwardCompatWrapperTest(unittest.TestCase):
    """search_jobs() and get_job_details() must keep opening/closing their
    own context each call (unchanged public behavior) -- only
    search_jobs_with_details() is the new combined, reused-context path."""

    def test_search_jobs_opens_and_closes_its_own_context(self):
        fake_playwright, fake_context = _fake_browser_context()
        with patch.object(
            naukri_client, "get_browser_context", return_value=(fake_playwright, fake_context)
        ) as mock_get_context, patch.object(
            naukri_client, "ensure_logged_in"
        ) as mock_ensure_logged_in, patch.object(
            naukri_client, "_scrape_search_results", return_value=list(FAKE_JOBS)
        ) as mock_scrape:
            results = naukri_client.search_jobs("python developer", "Pune")

        mock_get_context.assert_called_once()
        mock_ensure_logged_in.assert_called_once()
        mock_scrape.assert_called_once()
        fake_context.close.assert_called_once()
        fake_playwright.stop.assert_called_once()
        self.assertEqual(results, FAKE_JOBS)

    def test_get_job_details_opens_and_closes_its_own_context(self):
        fake_playwright, fake_context = _fake_browser_context()
        details = {"job_id": "1", "url": "https://example.com/1", "description": "d", "meta": "m"}
        with patch.object(
            naukri_client, "get_browser_context", return_value=(fake_playwright, fake_context)
        ) as mock_get_context, patch.object(
            naukri_client, "ensure_logged_in"
        ) as mock_ensure_logged_in, patch.object(
            naukri_client, "_scrape_job_details", return_value=details
        ) as mock_scrape:
            result = naukri_client.get_job_details("https://example.com/1")

        mock_get_context.assert_called_once()
        mock_ensure_logged_in.assert_called_once()
        mock_scrape.assert_called_once()
        fake_context.close.assert_called_once()
        fake_playwright.stop.assert_called_once()
        self.assertEqual(result, details)


class DecideChatbotTurnTest(unittest.TestCase):
    """Exhaustive coverage of naukri_client._decide_chatbot_turn() -- the
    actual bug-prone decision logic (chip/radio/text/file check ORDER,
    substantive-vs-skip chip filtering, can't-answer/no-match -> skip ->
    manual-review fallbacks) extracted 2026-09-06 (see DECISIONS.md)
    specifically so it's testable without a browser or Ollama. No
    Playwright, no filesystem, no logging involved -- pure input -> output.
    Several tests here are direct regression tests for real bugs already
    documented in DECISIONS.md.
    """

    def _state(self, **overrides):
        base = {
            "applied_banner_visible": False,
            "has_messages": True,
            "question": "Some question?",
            "chip_texts": [],
            "radio_options": [],
            "text_input_present": False,
            "file_input_present": False,
            "resume_pdf_exists": True,
        }
        base.update(overrides)
        return base

    # --- top-level dispatch ---

    def test_applied_banner_takes_priority_over_everything(self):
        state = self._state(applied_banner_visible=True, chip_texts=["Yes"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "Yes")
        self.assertEqual(action, {"type": "applied"})

    def test_no_messages(self):
        state = self._state(has_messages=False, question="")
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action, {"type": "no_messages"})

    # --- chips ---

    def test_chip_matched_answer_clicks_correct_index(self):
        state = self._state(chip_texts=["Yes", "No", "Skip this question"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "No")
        self.assertEqual(action["type"], "click_chip")
        self.assertEqual(action["index"], 1)  # index into the FULL chip_texts, not substantive_chips
        self.assertEqual(
            action["qa_entry"], {"question": "Some question?", "answer": "No", "options": ["Yes", "No"]}
        )

    def test_chip_answer_fn_returns_none_uses_skip_chip_if_present(self):
        state = self._state(chip_texts=["Yes", "No", "Skip this question"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "click_skip_chip")
        self.assertEqual(action["index"], 2)
        self.assertEqual(action["qa_entry"]["options"], ["Yes", "No"])

    def test_chip_answer_fn_returns_none_no_skip_chip_is_manual_review(self):
        state = self._state(chip_texts=["Yes", "No"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "no_answer")
        self.assertEqual(action["reason"], "questionnaire_required_manual_review")
        self.assertIsNone(action["qa_entry"]["answer"])

    def test_chip_answer_fn_returns_unmatched_string_uses_skip_if_present(self):
        state = self._state(chip_texts=["Yes", "No", "Skip this question"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "Maybe")
        self.assertEqual(action["type"], "click_skip_chip")

    def test_chip_answer_fn_returns_unmatched_string_no_skip_is_manual_review(self):
        state = self._state(chip_texts=["Yes", "No"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "Maybe")
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "no_chip_match")
        self.assertEqual(action["answer"], "Maybe")
        self.assertIsNone(action["qa_entry"]["answer"])  # never records the unmatched drafted answer as-is

    def test_chip_list_of_only_skip_chip_is_not_a_chip_choice_question(self):
        # Regression test (see DECISIONS.md, "Skip this question chip
        # doesn't mean this is a chip-choice question"): a lone skip chip
        # must fall through to check radio/text/file, not be treated as
        # the entire (empty) answer set for a chip-choice question.
        state = self._state(chip_texts=["Skip this question"], text_input_present=True)
        action = naukri_client._decide_chatbot_turn(
            state, answer_fn=lambda q, o: "typed answer" if o is None else None
        )
        self.assertEqual(action["type"], "fill_text")
        self.assertEqual(action["text"], "typed answer")

    # --- radio ---

    def test_radio_matched_answer(self):
        state = self._state(radio_options=["3 Months", "Serving Notice Period"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "3 Months")
        self.assertEqual(action["type"], "select_radio")
        self.assertEqual(action["index"], 0)

    def test_radio_answer_fn_returns_none_no_skip_is_manual_review(self):
        state = self._state(radio_options=["3 Months", "Serving Notice Period"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "no_answer")

    def test_radio_answer_fn_returns_unmatched_is_manual_review_with_answer(self):
        # Regression test: "Serving Notice Period" for a resume that states
        # a specific notice length (e.g. "3 months") is exactly the kind of
        # plausible-but-wrong match the verification pass (scoring.py) was
        # built to catch upstream -- this layer just needs to correctly
        # treat an unmatched drafted answer as unmatched.
        state = self._state(radio_options=["3 Months", "Serving Notice Period"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "1 Month")
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "no_radio_match")
        self.assertEqual(action["answer"], "1 Month")

    def test_chips_checked_before_radio(self):
        state = self._state(chip_texts=["Yes"], radio_options=["A", "B"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "Yes")
        self.assertEqual(action["type"], "click_chip")

    # --- text ---

    def test_text_input_answered(self):
        state = self._state(text_input_present=True)
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "2+ years")
        self.assertEqual(
            action,
            {
                "type": "fill_text",
                "text": "2+ years",
                "qa_entry": {"question": "Some question?", "answer": "2+ years", "options": None},
            },
        )

    def test_text_input_cant_answer_no_skip(self):
        state = self._state(text_input_present=True)
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "no_answer")
        self.assertIsNone(action["qa_entry"]["options"])

    def test_radio_checked_before_text(self):
        state = self._state(radio_options=["A"], text_input_present=True)
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "A")
        self.assertEqual(action["type"], "select_radio")

    # --- file upload ---

    def test_file_input_with_resume_present_attempts_upload(self):
        state = self._state(file_input_present=True, resume_pdf_exists=True)
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action, {"type": "attempt_resume_upload"})

    def test_file_input_with_missing_resume_is_manual_review(self):
        state = self._state(file_input_present=True, resume_pdf_exists=False)
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "resume_pdf_not_found")

    def test_file_input_is_checked_last_not_first(self):
        # Regression test for the ORIGINAL bug in this function: the file
        # input is persistently present in the DOM regardless of the
        # current question. Checking it before radio/text/chip misreads
        # every question type as a resume-upload prompt.
        state = self._state(file_input_present=True, radio_options=["Yes", "No"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: "Yes")
        self.assertEqual(action["type"], "select_radio")

    def test_file_input_ignores_a_coexisting_skip_chip(self):
        # Deliberate, documented behavior difference from every other
        # branch: the file-upload path never checks for a skip chip, even
        # when chip_texts happens to contain only the skip chip this turn.
        state = self._state(chip_texts=["Skip this question"], file_input_present=True, resume_pdf_exists=True)
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action, {"type": "attempt_resume_upload"})

    # --- final catch-all ---

    def test_nothing_matched_uses_skip_if_present(self):
        state = self._state(chip_texts=["Skip this question"])
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "click_skip_chip")

    def test_nothing_matched_no_skip_is_manual_review(self):
        state = self._state()
        action = naukri_client._decide_chatbot_turn(state, answer_fn=lambda q, o: None)
        self.assertEqual(action["type"], "manual_review")
        self.assertEqual(action["detail"], "no_mechanism")
        self.assertEqual(action["reason"], "chatbot_state_unrecognized_manual_review")


class HandleScreeningChatbotDispatchTest(unittest.TestCase):
    """Verifies _handle_screening_chatbot()'s dispatch loop carries out
    each action from _decide_chatbot_turn() correctly -- the right
    Playwright calls, the right qa_log entries, the right return value --
    using canned actions and a minimal mocked Page. _decide_chatbot_turn()
    and _read_chatbot_turn_state() are both mocked here (covered
    separately by DecideChatbotTurnTest and their own reading logic isn't
    under test in this class); jittered_wait() is mocked throughout so
    these run instantly instead of sleeping several seconds per turn.
    """

    def setUp(self):
        patcher = patch.object(naukri_client, "jittered_wait")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_applied_action_ends_the_walk_successfully(self):
        page = MagicMock()
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value={"type": "applied"}):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result, {"applied": True, "reason": "applied", "qa_log": [], "external_url": None})

    def test_no_messages_action_ends_the_walk_for_manual_review(self):
        page = MagicMock()
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": ""}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value={"type": "no_messages"}):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "chatbot_state_unrecognized_manual_review")
        self.assertEqual(result["qa_log"], [])

    def test_click_chip_clicks_correct_index_and_continues(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "Yes", "options": ["Yes", "No"]}
        actions = [{"type": "click_chip", "index": 0, "qa_entry": qa_entry}, {"type": "applied"}]
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", side_effect=actions):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        page.locator.assert_any_call(naukri_client.CHATBOT_CHIP_SELECTOR)
        self.assertEqual(result["qa_log"], [qa_entry])
        self.assertTrue(result["applied"])

    def test_click_skip_chip_clicks_correct_index_and_continues(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "(skipped via Naukri's 'Skip this question')", "options": None}
        actions = [{"type": "click_skip_chip", "index": 0, "qa_entry": qa_entry}, {"type": "applied"}]
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", side_effect=actions):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        page.locator.assert_any_call(naukri_client.CHATBOT_CHIP_SELECTOR)
        self.assertEqual(result["qa_log"], [qa_entry])

    def test_manual_review_action_appends_qa_entry_and_returns(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": None, "options": None}
        action = {
            "type": "manual_review",
            "detail": "no_mechanism",
            "reason": "chatbot_state_unrecognized_manual_review",
            "qa_entry": qa_entry,
        }
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value=action):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(
            result,
            {
                "applied": False,
                "reason": "chatbot_state_unrecognized_manual_review",
                "qa_log": [qa_entry],
                "external_url": None,
            },
        )

    def test_manual_review_no_answer_case_logs_without_raising(self):
        # Regression guard for the log-argument wiring in the "no_answer" /
        # "resume_pdf_not_found" special cases (_MANUAL_REVIEW_LOG_MESSAGES
        # dispatch in _handle_screening_chatbot) -- a wrong arg count/type
        # here would raise inside logging, not just log something odd.
        page = MagicMock()
        action = {
            "type": "manual_review",
            "detail": "no_answer",
            "reason": "questionnaire_required_manual_review",
            "qa_entry": {"question": "q", "answer": None, "options": None},
        }
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value=action):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "questionnaire_required_manual_review")

    def test_manual_review_resume_pdf_not_found_case_logs_without_raising(self):
        page = MagicMock()
        action = {
            "type": "manual_review",
            "detail": "resume_pdf_not_found",
            "reason": "resume_pdf_not_found_manual_review",
            "qa_entry": {"question": "q", "answer": None, "options": None},
        }
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value=action):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "resume_pdf_not_found_manual_review")

    def test_select_radio_send_success_continues(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "3 Months", "options": ["3 Months"]}
        actions = [{"type": "select_radio", "index": 0, "qa_entry": qa_entry}, {"type": "applied"}]
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", side_effect=actions), patch.object(
            naukri_client, "_select_radio_option"
        ) as mock_select, patch.object(naukri_client, "_click_send_button", return_value=True) as mock_send:
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        mock_select.assert_called_once()
        mock_send.assert_called_once()
        self.assertEqual(result["qa_log"], [qa_entry])
        self.assertTrue(result["applied"])

    def test_select_radio_send_failure_ends_for_manual_review(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "3 Months", "options": ["3 Months"]}
        action = {"type": "select_radio", "index": 0, "qa_entry": qa_entry}
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value=action), patch.object(
            naukri_client, "_select_radio_option"
        ), patch.object(naukri_client, "_click_send_button", return_value=False):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "chatbot_state_unrecognized_manual_review")
        self.assertEqual(result["qa_log"], [qa_entry])  # entry still recorded even though send failed

    def test_fill_text_send_success_continues(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "2+ years", "options": None}
        actions = [{"type": "fill_text", "text": "2+ years", "qa_entry": qa_entry}, {"type": "applied"}]
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", side_effect=actions), patch.object(
            naukri_client, "_click_send_button", return_value=True
        ):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        page.locator.assert_any_call(naukri_client.CHATBOT_TEXT_INPUT_SELECTOR)
        self.assertEqual(result["qa_log"], [qa_entry])

    def test_fill_text_send_failure_ends_for_manual_review(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "2+ years", "options": None}
        action = {"type": "fill_text", "text": "2+ years", "qa_entry": qa_entry}
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value=action), patch.object(
            naukri_client, "_click_send_button", return_value=False
        ):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "chatbot_state_unrecognized_manual_review")

    def test_attempt_resume_upload_success_continues(self):
        page = MagicMock()
        failure_banner_locator = MagicMock()
        failure_banner_locator.count.return_value = 0

        def locator_side_effect(selector):
            if selector == "text=/file upload was unsuccessful/i":
                return failure_banner_locator
            return MagicMock()

        page.locator.side_effect = locator_side_effect
        actions = [{"type": "attempt_resume_upload"}, {"type": "applied"}]
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", side_effect=actions):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["qa_log"], [{"question": "q", "answer": "(uploaded resume)", "options": None}])

    def test_attempt_resume_upload_failure_ends_for_manual_review(self):
        page = MagicMock()
        failure_banner_locator = MagicMock()
        failure_banner_locator.count.return_value = 1  # Naukri's failure text IS present

        def locator_side_effect(selector):
            if selector == "text=/file upload was unsuccessful/i":
                return failure_banner_locator
            return MagicMock()

        page.locator.side_effect = locator_side_effect
        action = {"type": "attempt_resume_upload"}
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(naukri_client, "_decide_chatbot_turn", return_value=action):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "resume_upload_failed_manual_review")
        self.assertEqual(result["qa_log"], [{"question": "q", "answer": "(upload failed)", "options": None}])

    def test_turn_cap_exhaustion(self):
        page = MagicMock()
        qa_entry = {"question": "q", "answer": "Yes", "options": ["Yes"]}
        with patch.object(
            naukri_client, "_read_chatbot_turn_state", return_value={"question": "q"}
        ), patch.object(
            naukri_client,
            "_decide_chatbot_turn",
            return_value={"type": "click_chip", "index": 0, "qa_entry": qa_entry},
        ):
            result = naukri_client._handle_screening_chatbot(page, "job1", answer_fn=lambda q, o: None)
        self.assertEqual(result["reason"], "questionnaire_too_long_manual_review")
        self.assertEqual(len(result["qa_log"]), naukri_client.MAX_CHATBOT_TURNS)


class ApplyResultBuilderTest(unittest.TestCase):
    """Added 2026-09-06 (see DECISIONS.md): _apply_result() is now the ONE
    place every apply_to_job()/_handle_screening_chatbot() return value is
    built, so all four keys are always present -- previously "external_url"
    appeared in just 1 of apply_to_job()'s 9 return statements."""

    def test_all_four_keys_always_present_with_defaults(self):
        result = naukri_client._apply_result(False, "some_reason")
        self.assertEqual(result, {"applied": False, "reason": "some_reason", "qa_log": [], "external_url": None})

    def test_qa_log_default_is_a_fresh_list_each_call_not_a_shared_mutable_default(self):
        result1 = naukri_client._apply_result(False, "reason1")
        result1["qa_log"].append({"question": "q", "answer": "a", "options": None})
        result2 = naukri_client._apply_result(False, "reason2")
        self.assertEqual(result2["qa_log"], [])  # unaffected by mutating result1's list

    def test_explicit_qa_log_and_external_url_passed_through(self):
        qa_log = [{"question": "q", "answer": "a", "options": None}]
        result = naukri_client._apply_result(True, "applied", qa_log=qa_log, external_url="https://example.com")
        self.assertEqual(
            result,
            {"applied": True, "reason": "applied", "qa_log": qa_log, "external_url": "https://example.com"},
        )


if __name__ == "__main__":
    unittest.main()
