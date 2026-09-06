"""Unit tests for storage.py. Uses a temporary SQLite file (config.DB_PATH
patched for the duration of each test) -- never touches the real jobs.db.
"""

import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
