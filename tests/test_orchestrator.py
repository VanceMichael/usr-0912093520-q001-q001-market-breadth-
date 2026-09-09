import sqlite3
import subprocess
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from tools.batch_pipeline import SCHEMA, connect
from tools import orchestrator


class OrchestratorTest(unittest.TestCase):
    @staticmethod
    def make_question(root: Path, db: Path, *, count: int = 1):
        with connect(db) as connection:
            connection.execute(
                "INSERT INTO batches(name,folder_path,markdown_path,question_count,created_at,updated_at) "
                "VALUES('b',?,?,?,'now','now')",
                (str(root / "b"), str(root / "b.md"), count),
            )
            batch_id = connection.execute("SELECT id FROM batches WHERE name='b'").fetchone()[0]
            for number in range(1, count + 1):
                folder = root / "b" / f"q{number:03d}"
                folder.mkdir(parents=True, exist_ok=True)
                prompt = f"prompt {number}"
                connection.execute(
                    "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,"
                    "task_type,difficulty,languages,reproducibility,mechanical_qc,qc_decision,qc_prompt_sha256,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, number, f"b-{number:03d}", folder.name, str(folder), "t", prompt,
                     orchestrator.prompt_hash(prompt), "0-1 代码生成", "中等", "Python", "无外部依赖",
                     "pass", "pass", orchestrator.prompt_hash(prompt), "approved", "now", "now"),
                )
            connection.commit()
            return connection.execute("SELECT * FROM questions ORDER BY question_no").fetchall()

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

    def test_retry_restores_registered_git_baseline_before_running_again(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            rows = self.make_question(root, db)
            folder = Path(rows[0]["folder_path"])
            subprocess.run(["git", "-C", str(folder), "init", "-q", "-b", "main"], check=True)
            (folder / "service.py").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(folder), "add", "service.py"], check=True)
            subprocess.run([
                "git", "-C", str(folder), "-c", "user.name=Test", "-c",
                "user.email=test@example.invalid", "commit", "-q", "-m", "baseline",
            ], check=True)
            sha = subprocess.run(
                ["git", "-C", str(folder), "rev-parse", "HEAD"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            with connect(db) as connection:
                connection.execute(
                    "UPDATE questions SET local_initial_sha=?,initial_snapshot=? WHERE id=?",
                    (sha, f"https://github.com/example/repo/commit/{sha}", rows[0]["id"]),
                )
                connection.execute(
                    "INSERT INTO runs(question_id,batch_run_id,launched_at,status) VALUES(?,?,'now','failed')",
                    (rows[0]["id"], "failed-1"),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM questions").fetchone()
            (folder / "service.py").write_text("failed change\n", encoding="utf-8")
            (folder / "untracked.txt").write_text("remove me\n", encoding="utf-8")

            def verify_clean(*_args, **_kwargs):
                self.assertEqual((folder / "service.py").read_text(encoding="utf-8"), "baseline\n")
                self.assertFalse((folder / "untracked.txt").exists())
                return "b-001", 0, "succeeded"

            with mock.patch.object(orchestrator, "run_one", side_effect=verify_clean):
                result = orchestrator.run_with_retries(
                    db, row, root / ".env", root / "runs", "image", 1.0, "2g", 60, 2,
                )
            self.assertEqual(result[1], 0)

    def test_interrupted_attempt_also_restores_baseline_without_consuming_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            rows = self.make_question(root, db)
            folder = Path(rows[0]["folder_path"])
            subprocess.run(["git", "-C", str(folder), "init", "-q", "-b", "main"], check=True)
            (folder / "service.py").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(folder), "add", "service.py"], check=True)
            subprocess.run([
                "git", "-C", str(folder), "-c", "user.name=Test", "-c",
                "user.email=test@example.invalid", "commit", "-q", "-m", "baseline",
            ], check=True)
            sha = subprocess.run(
                ["git", "-C", str(folder), "rev-parse", "HEAD"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            with connect(db) as connection:
                connection.execute(
                    "UPDATE questions SET local_initial_sha=?,initial_snapshot=? WHERE id=?",
                    (sha, f"https://github.com/example/repo/commit/{sha}", rows[0]["id"]),
                )
                connection.execute(
                    "INSERT INTO runs(question_id,batch_run_id,launched_at,status) VALUES(?,?,'now','interrupted')",
                    (rows[0]["id"], "interrupted-1"),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM questions").fetchone()
            (folder / "service.py").write_text("partial work\n", encoding="utf-8")

            def verify_clean(*_args, **_kwargs):
                self.assertEqual((folder / "service.py").read_text(encoding="utf-8"), "baseline\n")
                return "b-001", 0, "succeeded"

            with mock.patch.object(orchestrator, "run_one", side_effect=verify_clean):
                result = orchestrator.run_with_retries(
                    db, row, root / ".env", root / "runs", "image", 1.0, "2g", 60, 1,
                )
            self.assertEqual(result[1], 0)

    def test_non_runnable_questions_do_not_keep_batch_active_forever(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            self.make_question(root, db, count=2)
            with connect(db) as connection:
                connection.execute("UPDATE questions SET status='draft' WHERE question_no=1")
                connection.execute("UPDATE questions SET status='rejected' WHERE question_no=2")
                connection.commit()
            args = Namespace(
                db=db, batch="b", max_attempts=2, concurrency=2, codex_concurrency=1,
                env_file=root / ".env", data_root=root / "runs", image="image", cpus=1.0,
                memory="2g", timeout=60, codex="codex", agent_timeout=60,
            )
            self.assertEqual(orchestrator.run_delivery_pipeline(args), 0)
            with connect(db) as connection:
                assert connection.execute("SELECT status FROM batches").fetchone()[0] == "failed"
                statuses = connection.execute("SELECT status FROM questions ORDER BY question_no").fetchall()
            self.assertEqual([row[0] for row in statuses], ["blocked", "blocked"])

    def test_delivery_starts_while_another_model_is_still_running(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            rows = self.make_question(root, db, count=2)
            second_started = threading.Event()
            delivery_started = threading.Event()
            completed: set[int] = set()

            def model(_db, row, *_args):
                question_id = int(row["id"])
                if row["question_no"] == 2:
                    second_started.set()
                    self.assertTrue(delivery_started.wait(2))
                else:
                    self.assertTrue(second_started.wait(2))
                completed.add(question_id)
                return row["task_id"], 0, "succeeded"

            def deliver(_db, row, *_args):
                delivery_started.set()
                return row["task_id"], 0, "passed"

            args = Namespace(
                db=db, batch="b", max_attempts=2, concurrency=2, codex_concurrency=1,
                env_file=root / ".env", data_root=root / "runs", image="image", cpus=1.0,
                memory="2g", timeout=60, codex="codex", agent_timeout=60,
            )
            with mock.patch.object(orchestrator, "run_with_retries", side_effect=model), mock.patch.object(
                orchestrator, "delivery_needed", side_effect=lambda _db, row: int(row["id"]) in completed
            ), mock.patch.object(orchestrator, "deliver_one", side_effect=deliver), mock.patch.object(
                orchestrator, "finalize_batch", return_value=(0, "done")
            ):
                self.assertEqual(orchestrator.run_delivery_pipeline(args), 0)
            self.assertTrue(delivery_started.is_set())

    def test_delivery_failure_keeps_batch_active_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            rows = self.make_question(root, db)
            with connect(db) as connection:
                connection.execute("UPDATE questions SET status='completed'")
                connection.execute(
                    "INSERT INTO runs(question_id,batch_run_id,launched_at,status) "
                    "VALUES(?,?,'now','succeeded')",
                    (rows[0]["id"], "success-1"),
                )
                connection.commit()
            args = Namespace(
                db=db, batch="b", max_attempts=2, concurrency=1, codex_concurrency=1,
                env_file=root / ".env", data_root=root / "runs", image="image", cpus=1.0,
                memory="2g", timeout=60, codex="codex", agent_timeout=60,
            )
            with mock.patch.object(
                orchestrator, "deliver_one", return_value=("b-001", 1, "producer failed")
            ):
                self.assertEqual(orchestrator.run_delivery_pipeline(args), 1)
            with connect(db) as connection:
                self.assertEqual(connection.execute("SELECT status FROM batches").fetchone()[0], "draft")

    def test_partial_batch_exports_only_delivery_passed_questions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            rows = self.make_question(root, db, count=2)
            with connect(db) as connection:
                connection.execute("UPDATE questions SET status='completed' WHERE id=?", (rows[0]["id"],))
                connection.execute("UPDATE questions SET status='blocked' WHERE id=?", (rows[1]["id"],))
                connection.execute(
                    "INSERT INTO runs(question_id,batch_run_id,launched_at,status) VALUES(?,?,'now','succeeded')",
                    (rows[0]["id"], "success-1"),
                )
                values = {
                    "question_id": rows[0]["id"], "record_id": "record-1", "turn_no": 1,
                    "user_prompt": "prompt", "session_id": "session", "turn_id": "turn",
                    "initial_snapshot": "snapshot", "trajectory_file": "trajectory.jsonl",
                    "reproducibility": "无外部依赖", "harness": "Claude Code", "harness_version": "1",
                    "operating_system": "Linux", "task_type": "0-1 代码生成", "difficulty": "中等",
                    "languages": "Python", "delivery_score": 5, "delivery_description": "证据完整",
                    "instruction_score": 5, "instruction_description": "需求一致", "planning_score": 5,
                    "planning_description": "步骤清楚", "reasoning_score": 5, "reasoning_description": "推理完整",
                    "execution_score": 5, "execution_description": "执行完成", "submitter": "测试",
                    "submitted_at": "now", "turn_completed_at": "now", "delivery_qc_passed": 1,
                    "delivery_qc_note": "质检通过", "created_at": "now",
                }
                columns = ",".join(values)
                connection.execute(
                    f"INSERT INTO records({columns}) VALUES({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
                connection.commit()

            def export(_codex, prompt, _log, _timeout):
                self.assertIn("--select 1", prompt)
                (root / "b" / "CC_Codex-partial.xlsx").write_bytes(b"xlsx")
                (root / "b" / "轨迹_record-1.jsonl").write_text("{}\n", encoding="utf-8")
                return 0

            with mock.patch.object(orchestrator, "run_codex", side_effect=export):
                code, message = orchestrator.finalize_batch(db, "b", "codex", root / "runs", 60)
            self.assertEqual(code, 0)
            self.assertIn("partial", message)
            with connect(db) as connection:
                self.assertEqual(connection.execute("SELECT status FROM batches").fetchone()[0], "partial")


if __name__ == "__main__":
    unittest.main()
