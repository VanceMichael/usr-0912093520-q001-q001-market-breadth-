import json
import tempfile
import unittest
from pathlib import Path

from tools.batch_pipeline import connect
from tools.run_supervisor import (
    RunMetadataError,
    primary_session_file,
    read_supervisor_state,
    register_run_metadata,
    write_supervisor_state,
)
from tools.delivery_quality import build_evidence_qc_receipt, evidence_ledger_sha256, trajectory_sha256


class RunSupervisorTests(unittest.TestCase):
    def test_primary_session_requires_exactly_one_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "projects" / "workspace").mkdir(parents=True)
            session = root / "projects" / "workspace" / "session-001.jsonl"
            session.write_text("{}\n", encoding="utf-8")
            self.assertEqual(primary_session_file(root), session.resolve())
            (root / "projects" / "workspace" / "session-002.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RunMetadataError, "恰好一个"):
                primary_session_file(root)

    def test_register_binds_session_and_rejects_conflicting_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "production.sqlite3"
            trajectory = root / "trajectory"
            trajectory.mkdir()
            (trajectory / "session-001.jsonl").write_text("{}\n", encoding="utf-8")
            with connect(db) as connection:
                connection.execute(
                    "INSERT INTO batches(name,folder_path,markdown_path,question_count,created_at,updated_at) "
                    "VALUES('b',?,?,1,'now','now')", (str(root / "b"), str(root / "b.md")),
                )
                batch_id = connection.execute("SELECT id FROM batches").fetchone()[0]
                connection.execute(
                    "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,"
                    "task_type,difficulty,languages,reproducibility,created_at,updated_at) "
                    "VALUES(?,1,'b-001','q001',?,'title','prompt','hash','0-1 代码生成','中等','Python','无外部依赖','now','now')",
                    (batch_id, str(root / "b" / "q001")),
                )
                question_id = connection.execute("SELECT id FROM questions").fetchone()[0]
                connection.execute(
                    "INSERT INTO runs(question_id,batch_run_id,launched_at,status,trajectory_root) VALUES(?,?,'now','running',?)",
                    (question_id, "run-1", str(trajectory)),
                )
                connection.commit()
            self.assertEqual(register_run_metadata(db, question_id, "run-1", trajectory)[0], "session-001")
            with self.assertRaisesRegex(RunMetadataError, "SessionID"):
                register_run_metadata(
                    db, question_id, "run-1", trajectory,
                    expected_session_id="different-session",
                )
            with connect(db) as connection:
                bound = connection.execute("SELECT session_id,trajectory_root FROM runs").fetchone()
            self.assertEqual(bound[0], "session-001")
            self.assertEqual(Path(bound[1]), trajectory.resolve())

            (trajectory / "session-002.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RunMetadataError, "恰好一个"):
                register_run_metadata(db, question_id, "run-1", trajectory)

    def test_supervisor_state_is_atomic_and_content_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_supervisor_state(
                root, state="running", container_id="ccusr-test",
                activity=(10, 20, 1), error="secret detail",
            )
            self.assertEqual(path, (root / "supervisor.json").resolve())
            payload = read_supervisor_state(root)
            self.assertEqual(payload["state"], "running")
            self.assertEqual(payload["container_id"], "ccusr-test")
            self.assertEqual(payload["activity"]["file_count"], 1)
            self.assertEqual(payload["error"], "secret detail")
            self.assertFalse(list(root.glob("*.tmp")))

    def test_evidence_hash_and_receipt_are_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-001.jsonl"
            path.write_bytes(b'{"type":"result"}\n')
            record = {
                "record_id": "b-001-T01",
                "evidence_ledger": [{"id": "e1", "claim": "实际测试通过"}],
            }
            receipt = build_evidence_qc_receipt(record, path)
            self.assertEqual(receipt["version"], 2)
            self.assertEqual(receipt["evidence_ledger_sha256"], evidence_ledger_sha256(record["evidence_ledger"]))
            self.assertEqual(receipt["trajectory_sha256"], trajectory_sha256(path))
            self.assertTrue(receipt["zero_errors"])


if __name__ == "__main__":
    unittest.main()
