import unittest
from pathlib import Path
from unittest import mock

from tools import approve_and_submit_ready as worker


class ApproveAndSubmitReadyTests(unittest.TestCase):
    @mock.patch.object(worker, "review_queue")
    @mock.patch.object(worker, "connect")
    def test_next_record_prefers_one_unsubmitted_approval(self, connect, review_queue):
        connection = connect.return_value.__enter__.return_value
        review_queue.return_value = {
            "records": [
                {
                    "record_id": "retry",
                    "can_solo2_submit": True,
                    "solo2_status": "retry_wait",
                    "solo2_attempt_count": 1,
                },
                {
                    "record_id": "new",
                    "can_solo2_submit": True,
                    "solo2_status": "",
                    "solo2_attempt_count": 0,
                },
                {
                    "record_id": "blocked",
                    "can_solo2_submit": True,
                    "solo2_status": "schema_blocked",
                    "solo2_attempt_count": 1,
                },
            ],
            "meta": {"total_pages": 1},
        }

        self.assertEqual(worker._next_record(Path("db.sqlite3"), 3), ("new", False))
        connection.execute.assert_called_once()
        connection.commit.assert_called_once()

    @mock.patch.object(worker, "review_queue")
    @mock.patch.object(worker, "connect")
    def test_next_record_returns_only_one_ready_record(self, connect, review_queue):
        review_queue.side_effect = [
            {"records": [], "meta": {"total_pages": 0}},
            {
                "records": [{"record_id": "ready"}],
                "meta": {"total_pages": 2},
            },
        ]

        self.assertEqual(worker._next_record(Path("db.sqlite3"), 3), ("ready", True))
        self.assertEqual(review_queue.call_count, 2)


if __name__ == "__main__":
    unittest.main()
