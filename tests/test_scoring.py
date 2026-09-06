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


if __name__ == "__main__":
    unittest.main()
