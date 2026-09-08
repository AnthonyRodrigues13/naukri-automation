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

import hashlib
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import excel_log
import naukri_client
import orchestrator
import scoring
import storage

# storage.record_run() (added 2026-09-06 for cadence/staleness tracking,
# see DECISIONS.md) is called at the top of every cycle function. Patched
# module-wide here so every existing cycle-invoking test below doesn't
# need its own mock for it (none of them care about its call args) and,
# more importantly, doesn't silently write a real row into the real
# jobs.db's run_history table. RecordRunWiringTest below overrides this
# locally (nested patches compose correctly) to verify the actual call
# args per cycle.
#
# storage.record_search_run() (added 2026-09-06 for search-query rotation,
# roadmap item 4) is called unconditionally at the top of run_search_cycle()
# the same way -- patched module-wide for the same reason. RecordRunWiringTest
# overrides it locally too where it matters.
_record_run_patcher = None
_record_search_run_patcher = None


def setUpModule():
    global _record_run_patcher, _record_search_run_patcher
    _record_run_patcher = patch.object(storage, "record_run")
    _record_run_patcher.start()
    _record_search_run_patcher = patch.object(storage, "record_search_run")
    _record_search_run_patcher.start()


def tearDownModule():
    _record_run_patcher.stop()
    _record_search_run_patcher.stop()


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
            storage, "upsert_job"
        ), patch.object(
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


class RunApplyCycleDurableScreeningCacheTest(unittest.TestCase):
    """Cross-cycle screening-answer memory, added 2026-09-06 (see
    DECISIONS.md and JOB_SEARCH_STRATEGY.md roadmap item 3): run_apply_cycle()
    wires storage.get/save_screening_answer_verification in as
    scoring.draft_screening_answer's durable_lookup/durable_save hooks,
    closed over a hash of resume_profile computed once per cycle."""

    def setUp(self):
        self._original_dry_run = config.DRY_RUN
        self._original_paused = config.PAUSED
        self._original_auto_answer = config.AUTO_ANSWER_SCREENING_QUESTIONS
        config.DRY_RUN = True
        config.PAUSED = False
        config.AUTO_ANSWER_SCREENING_QUESTIONS = True

    def tearDown(self):
        config.DRY_RUN = self._original_dry_run
        config.PAUSED = self._original_paused
        config.AUTO_ANSWER_SCREENING_QUESTIONS = self._original_auto_answer

    def test_answer_fn_threads_durable_hooks_backed_by_storage_keyed_on_resume_hash(self):
        captured = {}

        def fake_apply_to_job(job_id, url, answer_fn=None):
            captured["answer_fn"] = answer_fn
            return {"applied": False, "reason": "dry_run", "qa_log": []}

        with patch.object(storage, "count_applications_today", return_value=0), patch.object(
            storage, "get_applicable_jobs", return_value=[dict(FAKE_JOB)]
        ), patch.object(naukri_client, "apply_to_job", side_effect=fake_apply_to_job), patch.object(
            excel_log, "log_application"
        ), patch.object(storage, "mark_applied"), patch.object(storage, "upsert_job"), patch.object(
            scoring, "load_resume_profile", return_value="resume text"
        ), patch.object(
            storage, "get_screening_answer_verification", return_value=True
        ) as mock_get_verification, patch.object(
            storage, "save_screening_answer_verification"
        ), patch.object(
            scoring, "_call_screening_llm", return_value="Yes"
        ):
            orchestrator.run_apply_cycle(live=False)

            # Called while the patches above are still active -- answer_fn
            # is a closure over run_apply_cycle's local scope, so calling it
            # after this `with` block exits would hit the REAL (unpatched)
            # scoring/storage functions instead of these mocks.
            answer_fn = captured["answer_fn"]
            result = answer_fn("Notice period?", ["Yes", "No"])

        self.assertEqual(result, "Yes")
        expected_hash = hashlib.sha256("resume text".encode()).hexdigest()
        # A durable hit means _verify_screening_answer never had to make a
        # real LLM verification call at all -- storage.get_screening_answer_verification
        # was reached with the correct (question, answer, resume_hash).
        mock_get_verification.assert_called_once_with("Notice period?", "Yes", expected_hash)


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
            storage, "upsert_job"
        ), patch.object(
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
            storage, "upsert_job"
        ), patch.object(
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
            storage, "upsert_job"
        ), patch.object(
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
            storage, "upsert_job"
        ), patch.object(
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


class RunScoringCycleEmbeddingReuseTest(unittest.TestCase):
    """Added 2026-09-06: run_scoring_cycle() computes the resume embedding
    once (scoring.embed_resume) and passes it into every
    score_job_with_reverification() call, instead of scoring.py
    recomputing it per job. Mocks score_job_with_reverification directly
    (the actual function run_scoring_cycle() calls) rather than the
    lower-level score_job() it wraps, so these tests don't depend on
    whether a given mocked fit_score happens to land in the gray zone."""

    def test_embed_resume_called_once_and_threaded_into_every_score_job_call(self):
        jobs = [
            {"job_id": "1", "title": "A", "description": "d1"},
            {"job_id": "2", "title": "B", "description": "d2"},
            {"job_id": "3", "title": "C", "description": "d3"},
        ]
        with patch.object(scoring, "load_resume_profile", return_value="resume text"), patch.object(
            storage, "get_unscored_jobs", return_value=jobs
        ), patch.object(scoring, "embed_resume", return_value=[1.0, 0.0]) as mock_embed_resume, patch.object(
            scoring,
            "score_job_with_reverification",
            return_value={"fit_score": 80, "reason": "x", "recommend_apply": True},
        ) as mock_score_job, patch.object(storage, "upsert_job"):
            orchestrator.run_scoring_cycle()

        mock_embed_resume.assert_called_once_with("resume text")
        self.assertEqual(mock_score_job.call_count, 3)
        for call in mock_score_job.call_args_list:
            self.assertEqual(call.kwargs.get("resume_embedding"), [1.0, 0.0])

    def test_no_unscored_jobs_skips_embedding_entirely(self):
        with patch.object(scoring, "load_resume_profile", return_value="resume text"), patch.object(
            storage, "get_unscored_jobs", return_value=[]
        ), patch.object(scoring, "embed_resume") as mock_embed_resume:
            orchestrator.run_scoring_cycle()

        mock_embed_resume.assert_not_called()

    def test_embedding_failure_skips_the_whole_cycle(self):
        jobs = [{"job_id": "1", "title": "A", "description": "d1"}]
        with patch.object(scoring, "load_resume_profile", return_value="resume text"), patch.object(
            storage, "get_unscored_jobs", return_value=jobs
        ), patch.object(scoring, "embed_resume", return_value=None), patch.object(
            scoring, "score_job_with_reverification"
        ) as mock_score_job:
            orchestrator.run_scoring_cycle()

        mock_score_job.assert_not_called()


class RunCheckStatusCycleTest(unittest.TestCase):
    """Outcome tracking, added 2026-09-06 (see DECISIONS.md): wiring
    between naukri_client.get_application_status_history() and
    storage.record_application_status()."""

    def test_each_history_entry_is_recorded(self):
        history = [
            {
                "job_id": "1",
                "ars_score": 44,
                "is_open": True,
                "statuses": [{"status_id": 1, "status_value": "Applied", "status_datetime": "2026-09-01 10:00:00"}],
            },
            {
                "job_id": "2",
                "ars_score": 10,
                "is_open": False,
                "statuses": [{"status_id": 1, "status_value": "Shortlisted", "status_datetime": "2026-09-02 09:00:00"}],
            },
        ]
        with patch.object(
            naukri_client, "get_application_status_history", return_value=history
        ), patch.object(storage, "record_application_status") as mock_record, patch.object(
            storage, "get_outcome_correlation", return_value=[]
        ):
            orchestrator.run_check_status_cycle()

        self.assertEqual(mock_record.call_count, 2)
        mock_record.assert_any_call("1", 44, history[0]["statuses"])
        mock_record.assert_any_call("2", 10, history[1]["statuses"])

    def test_empty_history_records_nothing(self):
        with patch.object(naukri_client, "get_application_status_history", return_value=[]), patch.object(
            storage, "record_application_status"
        ) as mock_record, patch.object(storage, "get_outcome_correlation", return_value=[]):
            orchestrator.run_check_status_cycle()

        mock_record.assert_not_called()


class RecordRunWiringTest(unittest.TestCase):
    """Verifies storage.record_run(cycle_name) is called with the correct
    name at the start of each cycle function -- cadence/staleness
    tracking, added 2026-09-06 (see DECISIONS.md). Overrides the
    module-wide storage.record_run patch locally to assert call args
    (nested unittest.mock patches compose correctly)."""

    def setUp(self):
        self._original_dry_run = config.DRY_RUN
        self._original_paused = config.PAUSED
        config.DRY_RUN = True  # keeps run_apply_cycle on its simplest (dry-run) path
        config.PAUSED = False

    def tearDown(self):
        config.DRY_RUN = self._original_dry_run
        config.PAUSED = self._original_paused

    def test_search_cycle_records_search(self):
        with patch.object(storage, "record_run") as mock_record_run, patch.object(
            storage, "get_job_ids_with_description", return_value=set()
        ), patch.object(naukri_client, "search_jobs_with_details", return_value=[]), patch.object(
            storage, "upsert_job"
        ), patch.object(storage, "get_original_job_embeddings", return_value=[]), patch.object(
            storage, "record_search_run"
        ) as mock_record_search_run:
            orchestrator.run_search_cycle("python developer", "Pune")
        mock_record_run.assert_called_once_with("search")
        mock_record_search_run.assert_called_once_with("python developer", "Pune")

    def test_scoring_cycle_records_score(self):
        with patch.object(storage, "record_run") as mock_record_run, patch.object(
            scoring, "load_resume_profile", return_value="resume"
        ), patch.object(storage, "get_unscored_jobs", return_value=[]):
            orchestrator.run_scoring_cycle()
        mock_record_run.assert_called_once_with("score")

    def test_apply_cycle_records_apply(self):
        with patch.object(storage, "record_run") as mock_record_run, patch.object(
            storage, "count_applications_today", return_value=0
        ), patch.object(storage, "get_applicable_jobs", return_value=[]):
            orchestrator.run_apply_cycle(live=False)
        mock_record_run.assert_called_once_with("apply")

    def test_check_status_cycle_records_check_status(self):
        with patch.object(storage, "record_run") as mock_record_run, patch.object(
            naukri_client, "get_application_status_history", return_value=[]
        ), patch.object(storage, "get_outcome_correlation", return_value=[]):
            orchestrator.run_check_status_cycle()
        mock_record_run.assert_called_once_with("check-status")


class RunSearchCycleDuplicateDetectionTest(unittest.TestCase):
    """Repost/duplicate detection, added 2026-09-06 (see DECISIONS.md and
    JOB_SEARCH_STRATEGY.md roadmap item 7). Only scoring.embed_job_description
    is mocked (to control embeddings deterministically) -- scoring.find_duplicate_job
    runs for real, so these tests exercise the actual cosine-similarity
    comparison, not just the wiring."""

    def _run(self, jobs, existing_candidates, embeddings_by_job_id):
        upserted = []
        with patch.object(storage, "get_job_ids_with_description", return_value=set()), patch.object(
            naukri_client, "search_jobs_with_details", return_value=jobs
        ), patch.object(storage, "get_original_job_embeddings", return_value=existing_candidates), patch.object(
            storage, "upsert_job", side_effect=lambda job: upserted.append(dict(job))
        ), patch.object(
            scoring, "embed_job_description", side_effect=lambda desc: embeddings_by_job_id.get(desc)
        ):
            orchestrator.run_search_cycle("python developer", "Pune")
        return upserted

    def test_job_matching_an_existing_original_is_flagged_as_a_duplicate(self):
        job = {"job_id": "new1", "title": "T", "company": "C", "url": "u", "description": "desc-new1"}
        upserted = self._run(
            jobs=[job],
            existing_candidates=[{"job_id": "orig1", "embedding": [1.0, 0.0]}],
            embeddings_by_job_id={"desc-new1": [1.0, 0.0]},  # identical -> similarity 1.0
        )
        self.assertEqual(upserted[0]["duplicate_of"], "orig1")
        self.assertEqual(json.loads(upserted[0]["description_embedding"]), [1.0, 0.0])

    def test_job_not_matching_any_existing_original_is_not_flagged(self):
        job = {"job_id": "new1", "title": "T", "company": "C", "url": "u", "description": "desc-new1"}
        upserted = self._run(
            jobs=[job],
            existing_candidates=[{"job_id": "orig1", "embedding": [1.0, 0.0]}],
            embeddings_by_job_id={"desc-new1": [0.0, 1.0]},  # orthogonal -> similarity 0.0
        )
        self.assertNotIn("duplicate_of", upserted[0])
        self.assertEqual(json.loads(upserted[0]["description_embedding"]), [0.0, 1.0])

    def test_second_repost_within_the_same_batch_matches_the_first_new_original_not_just_stored_ones(self):
        # No pre-existing originals in storage at all -- job_b is a repost
        # of job_a, and both are discovered in the SAME search_jobs_with_details
        # call, so job_a must become a candidate the moment it's processed,
        # not only on the NEXT search cycle.
        job_a = {"job_id": "a", "title": "T", "company": "C", "url": "ua", "description": "desc-a"}
        job_b = {"job_id": "b", "title": "T", "company": "C", "url": "ub", "description": "desc-b"}
        upserted = self._run(
            jobs=[job_a, job_b],
            existing_candidates=[],
            embeddings_by_job_id={"desc-a": [1.0, 0.0], "desc-b": [1.0, 0.0]},
        )
        self.assertNotIn("duplicate_of", upserted[0])  # job_a: nothing to match yet
        self.assertEqual(upserted[1]["duplicate_of"], "a")  # job_b: matches job_a from this same batch

    def test_a_duplicate_is_never_added_to_the_in_batch_candidate_pool(self):
        # job_b is flagged as a duplicate of job_a; job_c is near-identical
        # to job_b too, but must resolve back to job_a (the true original),
        # never to job_b (itself already a duplicate).
        job_a = {"job_id": "a", "title": "T", "company": "C", "url": "ua", "description": "desc-a"}
        job_b = {"job_id": "b", "title": "T", "company": "C", "url": "ub", "description": "desc-b"}
        job_c = {"job_id": "c", "title": "T", "company": "C", "url": "uc", "description": "desc-c"}
        upserted = self._run(
            jobs=[job_a, job_b, job_c],
            existing_candidates=[],
            embeddings_by_job_id={"desc-a": [1.0, 0.0], "desc-b": [1.0, 0.0], "desc-c": [1.0, 0.0]},
        )
        self.assertNotIn("duplicate_of", upserted[0])
        self.assertEqual(upserted[1]["duplicate_of"], "a")
        self.assertEqual(upserted[2]["duplicate_of"], "a")

    def test_empty_description_skips_duplicate_check_entirely(self):
        job = {"job_id": "new1", "title": "T", "company": "C", "url": "u", "description": ""}
        with patch.object(storage, "get_job_ids_with_description", return_value=set()), patch.object(
            naukri_client, "search_jobs_with_details", return_value=[job]
        ), patch.object(storage, "get_original_job_embeddings", return_value=[]), patch.object(
            storage, "upsert_job"
        ) as mock_upsert, patch.object(scoring, "embed_job_description") as mock_embed:
            orchestrator.run_search_cycle("python developer", "Pune")
        mock_embed.assert_not_called()
        self.assertNotIn("duplicate_of", mock_upsert.call_args.args[0])
        self.assertNotIn("description_embedding", mock_upsert.call_args.args[0])

    def test_embedding_failure_is_not_fatal_and_leaves_job_unflagged(self):
        job = {"job_id": "new1", "title": "T", "company": "C", "url": "u", "description": "desc-new1"}
        upserted = self._run(
            jobs=[job],
            existing_candidates=[{"job_id": "orig1", "embedding": [1.0, 0.0]}],
            embeddings_by_job_id={},  # embed_job_description returns None (dict.get default)
        )
        self.assertNotIn("duplicate_of", upserted[0])
        self.assertNotIn("description_embedding", upserted[0])


class PickLeastRecentlyRunQueryTest(unittest.TestCase):
    """Search-term/location rotation, added 2026-09-06 (see DECISIONS.md
    and JOB_SEARCH_STRATEGY.md roadmap item 4). Pure function -- no I/O,
    no mocking needed."""

    def test_a_never_run_entry_beats_one_with_any_recorded_run(self):
        queries = [
            {"keywords": "A", "location": "X"},
            {"keywords": "B", "location": "Y"},
        ]
        run_times = {("A", "X"): "2026-01-01T00:00:00"}  # B/Y never run
        result = orchestrator._pick_least_recently_run_query(queries, run_times)
        self.assertEqual(result, {"keywords": "B", "location": "Y"})

    def test_among_run_entries_the_oldest_is_picked(self):
        queries = [
            {"keywords": "A", "location": "X"},
            {"keywords": "B", "location": "Y"},
        ]
        run_times = {
            ("A", "X"): "2026-01-05T00:00:00",
            ("B", "Y"): "2026-01-01T00:00:00",  # older -> more overdue
        }
        result = orchestrator._pick_least_recently_run_query(queries, run_times)
        self.assertEqual(result, {"keywords": "B", "location": "Y"})

    def test_ties_among_never_run_entries_keep_original_order(self):
        queries = [
            {"keywords": "A", "location": "X"},
            {"keywords": "B", "location": "Y"},
        ]
        result = orchestrator._pick_least_recently_run_query(queries, {})
        self.assertEqual(result, {"keywords": "A", "location": "X"})

    def test_single_query_is_always_picked(self):
        queries = [{"keywords": "A", "location": "X"}]
        result = orchestrator._pick_least_recently_run_query(queries, {("A", "X"): "2026-01-01T00:00:00"})
        self.assertEqual(result, {"keywords": "A", "location": "X"})


class RunSearchCycleAutoTest(unittest.TestCase):
    """`search --auto`, added 2026-09-06 (see DECISIONS.md and
    JOB_SEARCH_STRATEGY.md roadmap item 4)."""

    def setUp(self):
        self._original_queries = config.SEARCH_QUERIES

    def tearDown(self):
        config.SEARCH_QUERIES = self._original_queries

    def test_picks_the_least_recently_run_query_and_delegates_to_run_search_cycle(self):
        config.SEARCH_QUERIES = [
            {"keywords": "AI Engineer", "location": "Pune"},
            {"keywords": "WordPress Developer", "location": "Goa"},
        ]
        with patch.object(
            storage,
            "get_search_run_times",
            return_value={("AI Engineer", "Pune"): "2026-01-01T00:00:00"},  # WordPress/Goa never run
        ), patch.object(orchestrator, "run_search_cycle", return_value=["job"]) as mock_run_search_cycle:
            result = orchestrator.run_search_cycle_auto()

        mock_run_search_cycle.assert_called_once_with("WordPress Developer", "Goa")
        self.assertEqual(result, ["job"])

    def test_empty_search_queries_does_nothing(self):
        config.SEARCH_QUERIES = []
        with patch.object(orchestrator, "run_search_cycle") as mock_run_search_cycle:
            orchestrator.run_search_cycle_auto()
        mock_run_search_cycle.assert_not_called()


class FormatTimeAgoTest(unittest.TestCase):
    """Cadence/staleness tracking, added 2026-09-06 (see DECISIONS.md)."""

    def test_days_ago(self):
        ts = (datetime.now() - timedelta(days=3, hours=1)).isoformat()
        self.assertEqual(orchestrator._format_time_ago(ts), "3 days ago")

    def test_singular_day(self):
        ts = (datetime.now() - timedelta(days=1, hours=1)).isoformat()
        self.assertEqual(orchestrator._format_time_ago(ts), "1 day ago")

    def test_hours_ago(self):
        ts = (datetime.now() - timedelta(hours=4)).isoformat()
        self.assertEqual(orchestrator._format_time_ago(ts), "4 hours ago")

    def test_singular_hour(self):
        ts = (datetime.now() - timedelta(hours=1, minutes=5)).isoformat()
        self.assertEqual(orchestrator._format_time_ago(ts), "1 hour ago")

    def test_minutes_ago(self):
        ts = (datetime.now() - timedelta(minutes=5)).isoformat()
        self.assertEqual(orchestrator._format_time_ago(ts), "5 minutes ago")

    def test_just_now(self):
        ts = datetime.now().isoformat()
        self.assertEqual(orchestrator._format_time_ago(ts), "just now")


if __name__ == "__main__":
    unittest.main()
