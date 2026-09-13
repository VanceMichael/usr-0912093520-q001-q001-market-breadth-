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

    @mock.patch.object(worker, "submit_records")
    @mock.patch.object(worker, "_next_record", return_value=("record-1", False))
    @mock.patch.object(worker, "connect")
    def test_submit_passes_extended_timeout_and_single_record(self, connect, next_record, submit):
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (0,)
        submit.return_value = {"submitted": 0, "skipped": 0, "failed": 0, "results": []}

        worker.approve_and_submit(
            Path("db.sqlite3"), Path("cookies"), "https://solo2.jzxhnh.com",
            reviewer="gaoyong", max_attempts=3, timeout=120,
        )
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["limit"], 1)
        self.assertEqual(submit.call_args.kwargs["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
