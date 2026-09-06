"""Unit tests for the deterministic (no-LLM-call) parts of scoring.py — the
salary/CTC gate and the score-response parser. Everything here runs offline,
no Ollama instance required, no browser.

Run with: python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import scoring


class MentionsSalaryTest(unittest.TestCase):
    """Live phrasing checks (2026-09-05) found the original keyword list
    missed real questions — these are regression tests for that fix, not
    just illustrative examples."""

    def test_previously_missed_phrasings_now_caught(self):
        previously_false_negatives = [
            "What is your expected take-home pay?",
            "What is your expected monthly pay?",
            "What is your annual package?",
            "What is your budget expectation for this role?",
        ]
        for question in previously_false_negatives:
            with self.subTest(question=question):
                self.assertTrue(scoring._mentions_salary(question))

    def test_original_keywords_still_caught(self):
        for question in [
            "What is your current CTC?",
            "What is your expected salary?",
            "What compensation are you looking for?",
            "What is your remuneration expectation?",
        ]:
            with self.subTest(question=question):
                self.assertTrue(scoring._mentions_salary(question))

    def test_non_salary_questions_not_flagged(self):
        for question in [
            "What is your notice period?",
            "Are you willing to relocate to Bangalore?",
            "How many years of experience do you have with Python?",
            "Do you have a valid passport for travel?",
        ]:
            with self.subTest(question=question):
                self.assertFalse(scoring._mentions_salary(question))

    def test_case_insensitive(self):
        self.assertTrue(scoring._mentions_salary("WHAT IS YOUR EXPECTED CTC"))


class ParseScoreResponseTest(unittest.TestCase):
    def test_valid_response(self):
        raw = '{"fit_score": 82, "reason": "good match", "recommend_apply": true}'
        result = scoring._parse_score_response(raw)
        self.assertEqual(result, {"fit_score": 82, "reason": "good match", "recommend_apply": True})

    def test_malformed_json_returns_none(self):
        self.assertIsNone(scoring._parse_score_response("not json at all"))

    def test_missing_keys_returns_none(self):
        self.assertIsNone(scoring._parse_score_response('{"fit_score": 82}'))

    def test_out_of_range_score_is_clamped(self):
        raw = '{"fit_score": 150, "reason": "x", "recommend_apply": false}'
        self.assertEqual(scoring._parse_score_response(raw)["fit_score"], 100)
        raw = '{"fit_score": -5, "reason": "x", "recommend_apply": false}'
        self.assertEqual(scoring._parse_score_response(raw)["fit_score"], 0)

    def test_non_numeric_score_returns_none(self):
        raw = '{"fit_score": "high", "reason": "x", "recommend_apply": true}'
        self.assertIsNone(scoring._parse_score_response(raw))


class ScoreJobTransientFailureTest(unittest.TestCase):
    """Regression tests for the 2026-09-05 fix: a transport failure talking
    to Ollama must leave the job retryable (fit_score=None), never a fake 0
    that get_unscored_jobs() (WHERE fit_score IS NULL) would never re-queue.
    A real fit_score=0 (embedding below floor, or unparseable JSON twice)
    must NOT change — those are genuine scores, not failures to retry."""

    def test_embedding_failure_returns_none_not_zero(self):
        with patch.object(scoring, "_embed", side_effect=requests.RequestException("boom")):
            result = scoring.score_job("some job description", "some resume")
        self.assertIsNone(result["fit_score"])
        self.assertIn("embedding call failed", result["reason"])

    def test_scoring_llm_transport_failure_returns_none_not_zero(self):
        with patch.object(scoring, "_embed", return_value=[1.0, 0.0]), patch.object(
            scoring, "_call_scoring_llm", side_effect=requests.RequestException("read timed out")
        ):
            result = scoring.score_job("some job description", "some resume")
        self.assertIsNone(result["fit_score"])
        self.assertIn("LLM call failed", result["reason"])

    def test_unparseable_json_after_retries_still_returns_real_zero(self):
        with patch.object(scoring, "_embed", return_value=[1.0, 0.0]), patch.object(
            scoring, "_call_scoring_llm", return_value="not valid json"
        ):
            result = scoring.score_job("some job description", "some resume")
        self.assertEqual(result["fit_score"], 0)

    def test_empty_description_still_returns_real_zero(self):
        result = scoring.score_job("", "some resume")
        self.assertEqual(result["fit_score"], 0)


def _mock_verify_response(text):
    resp = MagicMock()
    resp.json.return_value = {"response": text}
    return resp


class VerifyScreeningAnswerCacheTest(unittest.TestCase):
    """Added 2026-09-06 (see DECISIONS.md): _verify_screening_answer()
    accepts a per-apply-cycle cache dict, since Naukri visibly reuses
    standard screening questions verbatim across postings -- a literal
    repeat of (question, answer) within one cycle should skip the LLM
    call entirely, not re-verify from scratch."""

    def test_cache_hit_skips_second_llm_call(self):
        cache = {}
        with patch.object(scoring.requests, "post", return_value=_mock_verify_response("YES")) as mock_post:
            result1 = scoring._verify_screening_answer("Q1", "A1", "resume", cache=cache)
            result2 = scoring._verify_screening_answer("Q1", "A1", "resume", cache=cache)
        self.assertTrue(result1)
        self.assertTrue(result2)
        mock_post.assert_called_once()

    def test_different_question_answer_pairs_dont_collide(self):
        cache = {}
        responses = [_mock_verify_response("YES"), _mock_verify_response("NO")]
        with patch.object(scoring.requests, "post", side_effect=responses) as mock_post:
            result1 = scoring._verify_screening_answer("Q1", "A1", "resume", cache=cache)
            result2 = scoring._verify_screening_answer("Q1", "A2", "resume", cache=cache)
        self.assertTrue(result1)
        self.assertFalse(result2)
        self.assertEqual(mock_post.call_count, 2)

    def test_no_cache_given_always_calls_llm(self):
        with patch.object(scoring.requests, "post", return_value=_mock_verify_response("YES")) as mock_post:
            scoring._verify_screening_answer("Q1", "A1", "resume")
            scoring._verify_screening_answer("Q1", "A1", "resume")
        self.assertEqual(mock_post.call_count, 2)

    def test_transient_failure_is_not_cached_and_can_be_retried(self):
        cache = {}
        with patch.object(scoring.requests, "post", side_effect=requests.RequestException("boom")):
            result1 = scoring._verify_screening_answer("Q1", "A1", "resume", cache=cache)
        self.assertFalse(result1)
        self.assertNotIn(("Q1", "A1"), cache)

        with patch.object(scoring.requests, "post", return_value=_mock_verify_response("YES")) as mock_post:
            result2 = scoring._verify_screening_answer("Q1", "A1", "resume", cache=cache)
        self.assertTrue(result2)
        mock_post.assert_called_once()

    def test_draft_screening_answer_threads_cache_through_to_verification(self):
        cache = {}
        with patch.object(scoring, "_call_screening_llm", return_value="Yes"), patch.object(
            scoring.requests, "post", return_value=_mock_verify_response("YES")
        ) as mock_post:
            result1 = scoring.draft_screening_answer("Q1", ["Yes", "No"], "resume", cache=cache)
            result2 = scoring.draft_screening_answer("Q1", ["Yes", "No"], "resume", cache=cache)
        self.assertEqual(result1, "Yes")
        self.assertEqual(result2, "Yes")
        mock_post.assert_called_once()  # verification cached on the second, identical call


class MalformedOllamaResponseTest(unittest.TestCase):
    """Added 2026-09-06: a 200 OK response with an unexpected body (e.g.
    Ollama returning {"error": "model not found"} instead of the expected
    key, because a configured model isn't pulled) raises KeyError from the
    ["embedding"]/["response"] lookup, not requests.RequestException -- it
    must be handled the same as a transport failure, not crash uncaught."""

    def test_score_job_embedding_missing_key_returns_none_not_crash(self):
        with patch.object(scoring, "_embed", side_effect=KeyError("embedding")):
            result = scoring.score_job("some job description", "some resume")
        self.assertIsNone(result["fit_score"])

    def test_score_job_scoring_llm_missing_key_returns_none_not_crash(self):
        with patch.object(scoring, "_embed", return_value=[1.0, 0.0]), patch.object(
            scoring, "_call_scoring_llm", side_effect=KeyError("response")
        ):
            result = scoring.score_job("some job description", "some resume")
        self.assertIsNone(result["fit_score"])

    def test_draft_screening_answer_missing_key_returns_none_not_crash(self):
        with patch.object(scoring, "_call_screening_llm", side_effect=KeyError("response")):
            result = scoring.draft_screening_answer("Some question?", ["Yes", "No"], "resume")
        self.assertIsNone(result)

    def test_verify_screening_answer_missing_key_returns_false_not_crash(self):
        resp = MagicMock()
        resp.json.return_value = {"error": "model not found"}  # no "response" key
        with patch.object(scoring.requests, "post", return_value=resp):
            result = scoring._verify_screening_answer("Q", "A", "resume")
        self.assertFalse(result)


class ResumeEmbeddingReuseTest(unittest.TestCase):
    """Added 2026-09-06: the resume embedding is computed once per scoring
    cycle (scoring.embed_resume(), called by orchestrator.run_scoring_cycle())
    and passed into score_job() instead of being recomputed per job."""

    def test_embed_resume_returns_the_embedding(self):
        with patch.object(scoring, "_embed", return_value=[1.0, 2.0, 3.0]) as mock_embed:
            result = scoring.embed_resume("some resume text")
        self.assertEqual(result, [1.0, 2.0, 3.0])
        mock_embed.assert_called_once_with("some resume text")

    def test_embed_resume_returns_none_on_failure_not_raise(self):
        with patch.object(scoring, "_embed", side_effect=requests.RequestException("boom")):
            result = scoring.embed_resume("some resume text")
        self.assertIsNone(result)

    def test_embed_resume_returns_none_on_malformed_response(self):
        with patch.object(scoring, "_embed", side_effect=KeyError("embedding")):
            result = scoring.embed_resume("some resume text")
        self.assertIsNone(result)

    def test_score_job_with_precomputed_embedding_does_not_recompute_resume_embedding(self):
        # _embed should be called exactly once (for job_description) --
        # NOT a second time for resume_profile, since resume_embedding was
        # already supplied.
        with patch.object(scoring, "_embed", return_value=[1.0, 0.0]) as mock_embed, patch.object(
            scoring, "_call_scoring_llm", return_value='{"fit_score": 80, "reason": "x", "recommend_apply": true}'
        ):
            result = scoring.score_job("some job description", "some resume", resume_embedding=[1.0, 0.0])
        mock_embed.assert_called_once_with("some job description")
        self.assertEqual(result["fit_score"], 80)

    def test_score_job_without_precomputed_embedding_still_computes_it(self):
        # Backward-compat: no resume_embedding given -> falls back to
        # computing it internally, same as before this change.
        with patch.object(scoring, "_embed", return_value=[1.0, 0.0]) as mock_embed, patch.object(
            scoring, "_call_scoring_llm", return_value='{"fit_score": 80, "reason": "x", "recommend_apply": true}'
        ):
            scoring.score_job("some job description", "some resume")
        self.assertEqual(mock_embed.call_count, 2)  # job_description AND resume_profile


class ScoreJobWithReverificationTest(unittest.TestCase):
    """Fit-score gray-zone re-verification, added 2026-09-06 (see
    DECISIONS.md and JOB_SEARCH_STRATEGY.md roadmap item 2). Assumes the
    project defaults: FIT_SCORE_THRESHOLD=70, FIT_SCORE_REVERIFY_MARGIN=10
    (gray zone [60, 80]), FIT_SCORE_REVERIFY_PASSES=3 (2 extra calls)."""

    def setUp(self):
        self.assertEqual(config.FIT_SCORE_THRESHOLD, 70, "tests assume the default threshold")
        self.assertEqual(config.FIT_SCORE_REVERIFY_MARGIN, 10, "tests assume the default margin")
        self.assertEqual(config.FIT_SCORE_REVERIFY_PASSES, 3, "tests assume the default pass count")

    def _pass(self, fit_score):
        return {"fit_score": fit_score, "reason": f"reason for {fit_score}", "recommend_apply": fit_score >= 70}

    def test_score_clearly_above_gray_zone_is_trusted_on_first_pass(self):
        with patch.object(scoring, "score_job", return_value=self._pass(95)) as mock_score_job:
            result = scoring.score_job_with_reverification("jd", "resume")
        mock_score_job.assert_called_once()
        self.assertEqual(result, self._pass(95))

    def test_score_clearly_below_gray_zone_is_trusted_on_first_pass(self):
        with patch.object(scoring, "score_job", return_value=self._pass(30)) as mock_score_job:
            result = scoring.score_job_with_reverification("jd", "resume")
        mock_score_job.assert_called_once()
        self.assertEqual(result, self._pass(30))

    def test_score_at_threshold_triggers_reverification_and_averages(self):
        with patch.object(
            scoring, "score_job", side_effect=[self._pass(70), self._pass(80), self._pass(90)]
        ) as mock_score_job:
            result = scoring.score_job_with_reverification("jd", "resume")
        self.assertEqual(mock_score_job.call_count, 3)
        self.assertEqual(result["fit_score"], 80)  # (70+80+90)/3 = 80
        self.assertTrue(result["recommend_apply"])  # 80 >= 70
        self.assertIn("re-verified across 3 passes", result["reason"])

    def test_score_at_upper_margin_boundary_still_triggers_reverification(self):
        # threshold + margin = 80 exactly -- inclusive, not just strictly inside
        with patch.object(scoring, "score_job", return_value=self._pass(80)) as mock_score_job:
            scoring.score_job_with_reverification("jd", "resume")
        self.assertEqual(mock_score_job.call_count, 3)

    def test_score_just_outside_upper_margin_boundary_skips_reverification(self):
        with patch.object(scoring, "score_job", return_value=self._pass(81)) as mock_score_job:
            scoring.score_job_with_reverification("jd", "resume")
        mock_score_job.assert_called_once()

    def test_averaged_score_below_threshold_does_not_recommend_apply(self):
        with patch.object(scoring, "score_job", side_effect=[self._pass(65), self._pass(60), self._pass(60)]):
            result = scoring.score_job_with_reverification("jd", "resume")
        self.assertEqual(result["fit_score"], 62)  # round((65+60+60)/3) = round(61.67) = 62
        self.assertFalse(result["recommend_apply"])  # 62 < 70

    def test_first_pass_failure_returns_immediately_without_reverifying(self):
        first_failure = {"fit_score": None, "reason": "embedding call failed: boom", "recommend_apply": False}
        with patch.object(scoring, "score_job", return_value=first_failure) as mock_score_job:
            result = scoring.score_job_with_reverification("jd", "resume")
        mock_score_job.assert_called_once()
        self.assertEqual(result, first_failure)

    def test_some_reverify_passes_failing_transiently_averages_only_successes(self):
        transient_failure = {"fit_score": None, "reason": "LLM call failed: timeout", "recommend_apply": False}
        with patch.object(
            scoring, "score_job", side_effect=[self._pass(70), transient_failure, self._pass(80)]
        ) as mock_score_job:
            result = scoring.score_job_with_reverification("jd", "resume")
        self.assertEqual(mock_score_job.call_count, 3)
        self.assertEqual(result["fit_score"], 75)  # (70+80)/2 = 75, the failed pass excluded
        self.assertIn("re-verified across 2 passes", result["reason"])

    def test_all_reverify_passes_failing_returns_first_pass_unchanged(self):
        transient_failure = {"fit_score": None, "reason": "LLM call failed: timeout", "recommend_apply": False}
        first_success = self._pass(70)
        with patch.object(
            scoring, "score_job", side_effect=[first_success, transient_failure, transient_failure]
        ) as mock_score_job:
            with self.assertLogs(scoring.log, level="WARNING") as logs:
                result = scoring.score_job_with_reverification("jd", "resume")
        self.assertEqual(mock_score_job.call_count, 3)
        self.assertEqual(result, first_success)
        self.assertTrue(any("every re-verification" in m for m in logs.output))

    def test_resume_embedding_threaded_through_every_pass(self):
        with patch.object(
            scoring, "score_job", side_effect=[self._pass(70), self._pass(70), self._pass(70)]
        ) as mock_score_job:
            scoring.score_job_with_reverification("jd", "resume", resume_embedding=[1.0, 0.0])
        for call in mock_score_job.call_args_list:
            self.assertEqual(call.kwargs.get("resume_embedding"), [1.0, 0.0])


if __name__ == "__main__":
    unittest.main()
