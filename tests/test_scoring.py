"""Unit tests for the deterministic (no-LLM-call) parts of scoring.py — the
salary/CTC gate and the score-response parser. Everything here runs offline,
no Ollama instance required, no browser.

Run with: python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
