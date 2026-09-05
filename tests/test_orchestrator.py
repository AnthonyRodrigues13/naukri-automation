"""Unit tests for orchestrator.run_apply_cycle()'s safety checkpoints (all
added 2026-09-05, see DECISIONS.md):

- --live gating: a real (non-dry-run) apply cycle requires BOTH
  config.DRY_RUN=False on disk AND --live passed on that invocation.
- Pre-flight confirmation: a real cycle must show a candidate summary and
  get a typed "yes" before the loop starts at all.
- Circuit breaker: config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES real applies
  in a row (nothing skipped in between) pauses for another confirmation.

All of storage/naukri_client/excel_log are mocked -- no real browser,
Ollama, or database writes. Confirmation prompts are patched directly
(never via real stdin), except in PromptYesTest, which tests the
input()-wrapping helper itself.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import excel_log
import naukri_client
import orchestrator
import storage

FAKE_JOB = {
    "job_id": "123",
    "url": "https://example.com/job/123",
    "title": "Test Job",
    "company": "Test Co",
    "fit_score": 90,
    "reason": "great fit",
    "description": "some description",
}


def _make_jobs(n):
    return [
        {
            "job_id": f"job{i}",
            "url": f"https://example.com/job/{i}",
            "title": f"Test Job {i}",
            "company": "Test Co",
            "fit_score": 90,
            "reason": "great fit",
            "description": "some description",
        }
        for i in range(n)
    ]


class RunApplyCycleLiveFlagTest(unittest.TestCase):
    def setUp(self):
        self._original_dry_run = config.DRY_RUN
        self._original_paused = config.PAUSED
        config.PAUSED = False

    def tearDown(self):
        config.DRY_RUN = self._original_dry_run
        config.PAUSED = self._original_paused

    def _run(self, dry_run_on_disk, live):
        config.DRY_RUN = dry_run_on_disk
        seen_dry_run_at_apply_call = []

        def fake_apply_to_job(job_id, url, answer_fn=None):
            seen_dry_run_at_apply_call.append(config.DRY_RUN)
            if config.DRY_RUN:
                return {"applied": False, "reason": "dry_run", "qa_log": []}
            return {"applied": True, "reason": "applied", "qa_log": []}

        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=[dict(FAKE_JOB)]
        ), patch.object(naukri_client, "apply_to_job", side_effect=fake_apply_to_job), patch.object(
            excel_log, "log_application"
        ) as mock_log_application, patch.object(storage, "mark_applied") as mock_mark_applied, patch.object(
            orchestrator, "_preflight_summary_and_confirm", return_value=True
        ), patch.object(
            orchestrator, "_confirm_after_apply_streak", return_value=True
        ):
            orchestrator.run_apply_cycle(live=live)

        return seen_dry_run_at_apply_call, mock_log_application, mock_mark_applied

    def test_config_dry_run_true_stays_dry_run_regardless_of_live(self):
        for live in (False, True):
            with self.subTest(live=live):
                seen, mock_log_application, mock_mark_applied = self._run(dry_run_on_disk=True, live=live)
                self.assertEqual(seen, [True])
                self.assertTrue(mock_log_application.call_args.kwargs["dry_run"])
                self.assertTrue(mock_mark_applied.call_args.kwargs["dry_run"])
                self.assertTrue(config.DRY_RUN)  # restored to original (True)

    def test_config_dry_run_false_without_live_is_forced_dry_run(self):
        seen, mock_log_application, mock_mark_applied = self._run(dry_run_on_disk=False, live=False)
        self.assertEqual(seen, [True])  # forced dry run for the duration of the call
        self.assertTrue(mock_log_application.call_args.kwargs["dry_run"])
        self.assertTrue(mock_mark_applied.call_args.kwargs["dry_run"])
        self.assertFalse(config.DRY_RUN)  # restored to original (False) after the cycle

    def test_config_dry_run_false_with_live_goes_real(self):
        seen, mock_log_application, mock_mark_applied = self._run(dry_run_on_disk=False, live=True)
        self.assertEqual(seen, [False])
        self.assertFalse(mock_log_application.call_args.kwargs["dry_run"])
        self.assertFalse(mock_mark_applied.call_args.kwargs["dry_run"])
        self.assertFalse(config.DRY_RUN)  # restored (still False, same as original)

    def test_dry_run_override_is_restored_even_if_the_cycle_raises(self):
        config.DRY_RUN = False
        with patch.object(storage, "count_applications_today", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                orchestrator.run_apply_cycle(live=True)
        self.assertFalse(config.DRY_RUN)  # restored even though the function raised


class PreflightConfirmationTest(unittest.TestCase):
    def setUp(self):
        self._original_dry_run = config.DRY_RUN
        self._original_paused = config.PAUSED
        config.PAUSED = False

    def tearDown(self):
        config.DRY_RUN = self._original_dry_run
        config.PAUSED = self._original_paused

    def test_declining_preflight_aborts_before_any_apply_attempt(self):
        config.DRY_RUN = False
        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=[dict(FAKE_JOB)]
        ), patch.object(naukri_client, "apply_to_job") as mock_apply_to_job, patch.object(
            excel_log, "log_application"
        ) as mock_log_application, patch.object(
            storage, "mark_applied"
        ) as mock_mark_applied, patch.object(
            orchestrator, "_preflight_summary_and_confirm", return_value=False
        ) as mock_preflight:
            orchestrator.run_apply_cycle(live=True)

        mock_preflight.assert_called_once()
        mock_apply_to_job.assert_not_called()
        mock_log_application.assert_not_called()
        mock_mark_applied.assert_not_called()
        self.assertFalse(config.DRY_RUN)  # restored, not left overridden

    def test_preflight_not_shown_during_a_dry_run(self):
        config.DRY_RUN = True
        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=[dict(FAKE_JOB)]
        ), patch.object(
            naukri_client, "apply_to_job", return_value={"applied": False, "reason": "dry_run", "qa_log": []}
        ), patch.object(excel_log, "log_application"), patch.object(storage, "mark_applied"), patch.object(
            orchestrator, "_preflight_summary_and_confirm"
        ) as mock_preflight:
            orchestrator.run_apply_cycle(live=False)

        mock_preflight.assert_not_called()

    def test_preflight_not_shown_when_forced_back_to_dry_run(self):
        config.DRY_RUN = False  # on disk, but --live not passed
        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=[dict(FAKE_JOB)]
        ), patch.object(
            naukri_client, "apply_to_job", return_value={"applied": False, "reason": "dry_run", "qa_log": []}
        ), patch.object(excel_log, "log_application"), patch.object(storage, "mark_applied"), patch.object(
            orchestrator, "_preflight_summary_and_confirm"
        ) as mock_preflight:
            orchestrator.run_apply_cycle(live=False)

        mock_preflight.assert_not_called()

    def test_preflight_not_shown_when_no_candidates(self):
        config.DRY_RUN = False
        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=[]
        ), patch.object(orchestrator, "_preflight_summary_and_confirm") as mock_preflight:
            orchestrator.run_apply_cycle(live=True)

        mock_preflight.assert_not_called()

    def test_preflight_called_with_candidates_and_applied_today(self):
        config.DRY_RUN = False
        jobs = _make_jobs(2)
        with patch.object(storage, "count_applications_today", return_value=4), patch.object(
            storage, "get_applicable_jobs", return_value=jobs
        ), patch.object(
            naukri_client, "apply_to_job", return_value={"applied": True, "reason": "applied", "qa_log": []}
        ), patch.object(excel_log, "log_application"), patch.object(storage, "mark_applied"), patch.object(
            orchestrator, "_preflight_summary_and_confirm", return_value=True
        ) as mock_preflight:
            orchestrator.run_apply_cycle(live=True)

        mock_preflight.assert_called_once_with(jobs, 4)


class CircuitBreakerTest(unittest.TestCase):
    def setUp(self):
        self._original_dry_run = config.DRY_RUN
        self._original_paused = config.PAUSED
        config.DRY_RUN = False
        config.PAUSED = False

    def tearDown(self):
        config.DRY_RUN = self._original_dry_run
        config.PAUSED = self._original_paused

    def _run_with_results(self, results, confirm_streak_return=True):
        jobs = _make_jobs(len(results))
        apply_mock = MagicMock(side_effect=results)
        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=jobs
        ), patch.object(naukri_client, "apply_to_job", apply_mock), patch.object(
            excel_log, "log_application"
        ), patch.object(storage, "mark_applied") as mock_mark_applied, patch.object(
            orchestrator, "_preflight_summary_and_confirm", return_value=True
        ), patch.object(
            orchestrator, "_confirm_after_apply_streak", return_value=confirm_streak_return
        ) as mock_confirm_streak:
            orchestrator.run_apply_cycle(live=True)
        return apply_mock, mock_mark_applied, mock_confirm_streak

    def test_trips_after_threshold_consecutive_applies_and_continues_on_confirm(self):
        self.assertEqual(config.CIRCUIT_BREAKER_CONSECUTIVE_APPLIES, 3, "test assumes the default threshold")
        applied = {"applied": True, "reason": "applied", "qa_log": []}
        results = [dict(applied) for _ in range(5)]
        apply_mock, mock_mark_applied, mock_confirm_streak = self._run_with_results(
            results, confirm_streak_return=True
        )
        self.assertEqual(apply_mock.call_count, 5)  # confirmed once, all 5 attempted
        self.assertEqual(mock_mark_applied.call_count, 5)
        mock_confirm_streak.assert_called_once()

    def test_stops_when_user_declines_to_continue(self):
        applied = {"applied": True, "reason": "applied", "qa_log": []}
        results = [dict(applied) for _ in range(5)]
        apply_mock, mock_mark_applied, mock_confirm_streak = self._run_with_results(
            results, confirm_streak_return=False
        )
        self.assertEqual(apply_mock.call_count, 3)  # stopped right at the trip point
        self.assertEqual(mock_mark_applied.call_count, 3)
        mock_confirm_streak.assert_called_once()

    def test_non_applied_outcome_resets_the_streak(self):
        applied = {"applied": True, "reason": "applied", "qa_log": []}
        skipped = {"applied": False, "reason": "questionnaire_required_manual_review", "qa_log": []}
        # applied, applied, skipped, applied, applied -- never 3 in a row
        results = [dict(applied), dict(applied), dict(skipped), dict(applied), dict(applied)]
        apply_mock, mock_mark_applied, mock_confirm_streak = self._run_with_results(results)
        self.assertEqual(apply_mock.call_count, 5)  # never tripped, all 5 attempted
        mock_confirm_streak.assert_not_called()


class PromptYesTest(unittest.TestCase):
    def test_eof_error_aborts(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(orchestrator._prompt_yes("test message"))

    def test_keyboard_interrupt_aborts(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertFalse(orchestrator._prompt_yes("test message"))

    def test_yes_confirms_case_insensitively(self):
        for response in ("yes", "YES", "Yes", "  yes  "):
            with self.subTest(response=response):
                with patch("builtins.input", return_value=response):
                    self.assertTrue(orchestrator._prompt_yes("test message"))

    def test_anything_else_aborts(self):
        for response in ("no", "", "y", "yes please"):
            with self.subTest(response=response):
                with patch("builtins.input", return_value=response):
                    self.assertFalse(orchestrator._prompt_yes("test message"))


if __name__ == "__main__":
    unittest.main()
