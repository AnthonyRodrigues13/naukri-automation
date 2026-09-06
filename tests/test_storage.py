"""Unit tests for storage.py. Uses a temporary SQLite file (config.DB_PATH
patched for the duration of each test) -- never touches the real jobs.db.
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import storage


class GetJobIdsWithDescriptionTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "test_jobs.db")
        self._patcher = patch.object(config, "DB_PATH", self._db_path)
        self._patcher.start()
        storage.init_db()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_only_jobs_with_non_empty_description_are_returned(self):
        storage.upsert_job({"job_id": "1", "title": "A", "company": "X", "url": "u1", "description": "real content"})
        storage.upsert_job({"job_id": "2", "title": "B", "company": "X", "url": "u2", "description": ""})
        storage.upsert_job({"job_id": "3", "title": "C", "company": "X", "url": "u3"})  # no description at all
        storage.upsert_job({"job_id": "4", "title": "D", "company": "X", "url": "u4", "description": "more content"})

        result = storage.get_job_ids_with_description()

        self.assertEqual(result, {"1", "4"})

    def test_empty_db_returns_empty_set(self):
        self.assertEqual(storage.get_job_ids_with_description(), set())


class RecordApplicationStatusTest(unittest.TestCase):
    """Outcome tracking, added 2026-09-06 (see DECISIONS.md)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "test_jobs.db")
        self._patcher = patch.object(config, "DB_PATH", self._db_path)
        self._patcher.start()
        storage.init_db()
        storage.upsert_job({"job_id": "1", "title": "A", "company": "X", "url": "u1", "fit_score": 80})

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_records_new_statuses_and_updates_latest(self):
        statuses = [
            {"status_id": 1, "status_value": "Applied", "status_datetime": "2026-09-01 10:00:00"},
            {"status_id": 2, "status_value": "Application Sent", "status_datetime": "2026-09-01 10:00:01"},
        ]
        storage.record_application_status("1", 44, statuses)

        job = storage.get_job("1")
        self.assertEqual(job["latest_apply_status"], "Application Sent")
        self.assertEqual(job["naukri_ars_score"], 44)

        with storage._connect() as conn:
            rows = conn.execute("SELECT * FROM application_status_history WHERE job_id = ?", ("1",)).fetchall()
        self.assertEqual(len(rows), 2)

    def test_recording_the_same_statuses_again_does_not_duplicate_rows(self):
        statuses = [{"status_id": 1, "status_value": "Applied", "status_datetime": "2026-09-01 10:00:00"}]
        storage.record_application_status("1", 44, statuses)
        storage.record_application_status("1", 44, statuses)  # same statuses again

        with storage._connect() as conn:
            rows = conn.execute("SELECT * FROM application_status_history WHERE job_id = ?", ("1",)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_a_later_check_with_a_new_status_appends_without_duplicating_old_ones(self):
        storage.record_application_status(
            "1", 44, [{"status_id": 1, "status_value": "Applied", "status_datetime": "2026-09-01 10:00:00"}]
        )
        storage.record_application_status(
            "1",
            60,
            [
                {"status_id": 1, "status_value": "Applied", "status_datetime": "2026-09-01 10:00:00"},
                {"status_id": 3, "status_value": "Shortlisted", "status_datetime": "2026-09-03 12:00:00"},
            ],
        )

        with storage._connect() as conn:
            rows = conn.execute(
                "SELECT status_value FROM application_status_history WHERE job_id = ? ORDER BY status_datetime",
                ("1",),
            ).fetchall()
        self.assertEqual([r["status_value"] for r in rows], ["Applied", "Shortlisted"])

        job = storage.get_job("1")
        self.assertEqual(job["latest_apply_status"], "Shortlisted")
        self.assertEqual(job["naukri_ars_score"], 60)

    def test_empty_statuses_list_does_nothing(self):
        storage.record_application_status("1", 44, [])
        job = storage.get_job("1")
        self.assertIsNone(job["latest_apply_status"])


class GetOutcomeCorrelationTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "test_jobs.db")
        self._patcher = patch.object(config, "DB_PATH", self._db_path)
        self._patcher.start()
        storage.init_db()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_only_jobs_with_a_recorded_status_are_returned_ordered_by_fit_score(self):
        storage.upsert_job({"job_id": "1", "title": "A", "company": "X", "url": "u1", "fit_score": 60})
        storage.upsert_job({"job_id": "2", "title": "B", "company": "X", "url": "u2", "fit_score": 90})
        storage.upsert_job({"job_id": "3", "title": "C", "company": "X", "url": "u3", "fit_score": 75})  # no status

        storage.record_application_status(
            "1", 20, [{"status_id": 1, "status_value": "Applied", "status_datetime": "2026-09-01 10:00:00"}]
        )
        storage.record_application_status(
            "2", 40, [{"status_id": 1, "status_value": "Shortlisted", "status_datetime": "2026-09-02 10:00:00"}]
        )

        result = storage.get_outcome_correlation()

        self.assertEqual([r["job_id"] for r in result], ["2", "1"])  # ordered by fit_score desc
        self.assertEqual(result[0]["latest_apply_status"], "Shortlisted")
        self.assertEqual(result[0]["naukri_ars_score"], 40)

    def test_empty_db_returns_empty_list(self):
        self.assertEqual(storage.get_outcome_correlation(), [])


class RecordRunTest(unittest.TestCase):
    """Cadence/staleness tracking, added 2026-09-06 (see DECISIONS.md)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmpdir.name) / "test_jobs.db")
        self._patcher = patch.object(config, "DB_PATH", self._db_path)
        self._patcher.start()
        storage.init_db()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_get_last_run_times_reflects_a_recorded_cycle(self):
        storage.record_run("search")
        result = storage.get_last_run_times()
        self.assertIn("search", result)
        datetime.fromisoformat(result["search"])  # a real, parseable ISO timestamp

    def test_never_run_cycle_is_absent(self):
        self.assertEqual(storage.get_last_run_times(), {})

    def test_multiple_cycles_tracked_independently(self):
        storage.record_run("search")
        storage.record_run("score")
        result = storage.get_last_run_times()
        self.assertIn("search", result)
        self.assertIn("score", result)
        self.assertNotIn("apply", result)

    def test_most_recent_run_is_returned_for_a_repeated_cycle(self):
        with patch.object(storage, "datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 1, 1, 10, 0, 0)
            storage.record_run("search")
            mock_datetime.now.return_value = datetime(2026, 1, 2, 10, 0, 0)
            storage.record_run("search")

        result = storage.get_last_run_times()
        self.assertEqual(result["search"], "2026-01-02T10:00:00")


if __name__ == "__main__":
    unittest.main()
