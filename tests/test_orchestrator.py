import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.batch_pipeline import SCHEMA, connect
from tools import orchestrator


class OrchestratorTest(unittest.TestCase):
    def test_two_workers_create_independent_runs_and_logs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            with connect(db) as connection:
                connection.executescript(
                    "INSERT INTO batches(name,folder_path,markdown_path,question_count,created_at,updated_at) "
                    "VALUES('b','%s','%s',2,'now','now');" % (root / 'b', root / 'b.md')
                )
                batch_id = connection.execute("SELECT id FROM batches").fetchone()[0]
                for number in (1, 2):
                    folder = root / "b" / f"q{number:03d}"
                    folder.mkdir(parents=True)
                    connection.execute(
                        "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,task_type,difficulty,languages,reproducibility,mechanical_qc,qc_decision,qc_prompt_sha256,status,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, number, f"b-{number:03d}", folder.name, str(folder), "t", f"prompt {number}", orchestrator.prompt_hash(f"prompt {number}"), "0-1 代码生成", "中等", "Python", "无外部依赖", "pass", "pass", orchestrator.prompt_hash(f"prompt {number}"), "approved", "now", "now"),
                    )
                connection.commit()
            env = root / ".env"
            env.write_text("CC_SWITCH_BASE_URL=https://relay.example\nCC_SWITCH_API_KEY=secret\nCC_SWITCH_MODEL=model\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                Path(kwargs["stdout"].name).write_text("ok\n", encoding="utf-8")
                return mock.Mock(returncode=0)

            with mock.patch.object(orchestrator.subprocess, "run", side_effect=fake_run):
                orchestrator.main_args = None
                with mock.patch("sys.argv", ["orchestrator", "--db", str(db), "--batch", "b", "--env-file", str(env), "--data-root", str(root / "runs"), "--image", "fake", "--concurrency", "2"]):
                    self.assertEqual(orchestrator.main(), 0)
            with connect(db) as connection:
                statuses = connection.execute("SELECT status FROM runs ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in statuses], ["succeeded", "succeeded"])
            self.assertEqual(len(list((root / "runs" / "b").glob("b-*/docker-*.log"))), 2)

    def test_exhausted_question_is_blocked_without_another_container(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            folder = root / "b" / "q001"
            folder.mkdir(parents=True)
            with connect(db) as connection:
                connection.execute(
                    "INSERT INTO batches(name,folder_path,markdown_path,question_count,created_at,updated_at) "
                    "VALUES('b',?,?,1,'now','now')",
                    (str(root / "b"), str(root / "b.md")),
                )
                batch_id = connection.execute("SELECT id FROM batches").fetchone()[0]
                prompt = "build a complete service"
                connection.execute(
                    "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,task_type,difficulty,languages,reproducibility,mechanical_qc,qc_decision,qc_prompt_sha256,status,created_at,updated_at) "
                    "VALUES(?,1,'b-001','q001',?,'t',?,?,?,?,?,'none','pass','pass',?,'approved','now','now')",
                    (batch_id, str(folder), prompt, orchestrator.prompt_hash(prompt), "0-1 代码生成", "中等", "Python", orchestrator.prompt_hash(prompt)),
                )
                question_id = connection.execute("SELECT id FROM questions").fetchone()[0]
                for attempt in (1, 2):
                    connection.execute(
                        "INSERT INTO runs(question_id,batch_run_id,launched_at,status) VALUES(?,?,?,'failed')",
                        (question_id, f"failed-{attempt}", f"time-{attempt}"),
                    )
                connection.commit()
            env = root / ".env"
            env.write_text("", encoding="utf-8")
            with mock.patch.object(orchestrator.subprocess, "run") as run:
                with mock.patch("sys.argv", [
                    "orchestrator", "--db", str(db), "--batch", "b",
                    "--env-file", str(env), "--data-root", str(root / "runs"),
                    "--max-attempts", "2",
                ]):
                    self.assertEqual(orchestrator.main(), 0)
                run.assert_not_called()
            with connect(db) as connection:
                status = connection.execute("SELECT status FROM questions").fetchone()[0]
            self.assertEqual(status, "blocked")


if __name__ == "__main__":
    unittest.main()
