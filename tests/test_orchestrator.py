import json
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

    def test_clear_question_leases_reclaims_model_and_delivery_claims(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            rows = self.make_question(root, db)
            with connect(db) as connection:
                connection.execute(
                    "UPDATE questions SET model_lease_owner='old-model',"
                    "model_lease_expires_at='2999-01-01T00:00:00+00:00',"
                    "delivery_lease_owner='old-delivery',"
                    "delivery_lease_expires_at='2999-01-01T00:00:00+00:00' WHERE id=?",
                    (rows[0]["id"],),
                )
                connection.commit()

            orchestrator.clear_question_leases(db)

            with connect(db) as connection:
                row = connection.execute(
                    "SELECT model_lease_owner,model_lease_expires_at,"
                    "delivery_lease_owner,delivery_lease_expires_at FROM questions"
                ).fetchone()
            self.assertEqual(tuple(row), ("", "", "", ""))

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

            commands = []

            def fake_popen(command, **kwargs):
                commands.append(command)
                kwargs["stdout"].write(json.dumps({"type": "result", "is_error": False}) + "\n")
                kwargs["stdout"].flush()
                return mock.Mock(wait=mock.Mock(return_value=0))

            gate_evidence = mock.Mock(
                session_id="session-test", assistant_messages=2,
                tool_uses=1, changed_files=1,
            )
            with mock.patch.object(orchestrator.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
                orchestrator, "validate_effective_trajectory", return_value=gate_evidence
            ):
                orchestrator.main_args = None
                with mock.patch("sys.argv", ["orchestrator", "--db", str(db), "--batch", "b", "--env-file", str(env), "--data-root", str(root / "runs"), "--image", "fake", "--concurrency", "2"]):
                    self.assertEqual(orchestrator.main(), 0)
            with connect(db) as connection:
                statuses = connection.execute("SELECT status FROM runs ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in statuses], ["succeeded", "succeeded"])
            self.assertEqual(len(list((root / "runs" / "b").glob("b-*/docker-*.log"))), 2)
            self.assertEqual(len(commands), 2)
            for command in commands:
                self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
                self.assertIn("--verbose", command)
                self.assertIn("--include-partial-messages", command)
                self.assertIn("--safe-mode", command)
                self.assertIn("--disable-slash-commands", command)
            with connect(db) as connection:
                metadata = connection.execute(
                    "SELECT container_cwd,operating_system,model FROM runs ORDER BY id"
                ).fetchall()
            self.assertTrue(all(row[0] == "/workspace" for row in metadata))
            self.assertTrue(all(row[1].startswith("Linux (Docker container on ") for row in metadata))
            self.assertEqual([row[2] for row in metadata], ["model", "model"])

    def test_stream_result_and_structured_task_state_are_required(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            log = root / "worker.log"
            log.write_text("docker noise\n", encoding="utf-8")
            self.assertEqual(orchestrator.stream_result_state(log), "missing")
            log.write_text(
                json.dumps({"type": "result", "is_error": True}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(orchestrator.stream_result_state(log), "failure")
            log.write_text(
                json.dumps({"type": "result", "is_error": False}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(orchestrator.stream_result_state(log), "success")

            task = root / "claude" / "tasks" / "session" / "1.json"
            task.parent.mkdir(parents=True)
            task.write_text(json.dumps({"status": "in_progress"}), encoding="utf-8")
            self.assertEqual(orchestrator.claude_task_state(root / "claude"), "incomplete")
            task.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            self.assertEqual(orchestrator.claude_task_state(root / "claude"), "complete")

    def test_failure_classifier_separates_gateway_auth_and_catalog_warning(self):
        self.assertEqual(
            orchestrator.classify_failure("failed", "API Error: 504 Gateway Time-out").kind,
            "transient_gateway",
        )
        self.assertEqual(
            orchestrator.classify_failure("failed", "API Error: 403 unauthorized").kind,
            "permanent_auth",
        )
        warning = (
            '"auto_model/urm" is not described by this version catalog '
            "[claude-code:unrecognized_model]"
        )
        self.assertEqual(
            orchestrator.classify_failure("failed", "worker failed", warning).kind,
            "worker_failure",
        )

    def test_gateway_backoff_has_independent_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            row = self.make_question(root, db)[0]
            calls = 0

            def run_one(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    with connect(db) as connection:
                        connection.execute(
                            "INSERT INTO runs(question_id,batch_run_id,launched_at,status,error_message,failure_kind,retryable) "
                            "VALUES(?,?,'now','failed','API Error: 504 Gateway Time-out','transient_gateway',1)",
                            (row["id"], "gateway-1"),
                        )
                        connection.commit()
                    return "b-001", 1, "failed: 504 Gateway Time-out"
                return "b-001", 0, "succeeded"

            with mock.patch.object(orchestrator, "run_one", side_effect=run_one), mock.patch.object(
                orchestrator, "restore_question_workspace"
            ), mock.patch.object(orchestrator.time, "sleep") as sleep:
                result = orchestrator.run_with_retries(
                    db, row, root / ".env", root / "runs", "image", 1.0, "2g", 60,
                    1, gateway_max_attempts=3, gateway_backoff_base=30,
                    gateway_backoff_max=300,
                )
            self.assertEqual(result[1], 0)
            self.assertEqual(calls, 2)
            sleep.assert_called_once()
            with connect(db) as connection:
                run = connection.execute(
                    "SELECT retry_delay_seconds FROM runs WHERE batch_run_id='gateway-1'"
                ).fetchone()
            self.assertGreaterEqual(run[0], 30)
            self.assertLessEqual(run[0], 45)

    def test_permanent_auth_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            row = self.make_question(root, db)[0]

            def run_one(*_args, **_kwargs):
                with connect(db) as connection:
                    connection.execute(
                        "INSERT INTO runs(question_id,batch_run_id,launched_at,status,error_message,failure_kind) "
                        "VALUES(?,'auth-1','now','failed','API Error: 401 unauthorized','permanent_auth')",
                        (row["id"],),
                    )
                    connection.commit()
                return "b-001", 1, "failed: 401 unauthorized"

            with mock.patch.object(orchestrator, "run_one", side_effect=run_one) as runner:
                result = orchestrator.run_with_retries(
                    db, row, root / ".env", root / "runs", "image", 1.0, "2g", 60, 2,
                )
            self.assertEqual(result[1], 1)
            runner.assert_called_once()

    def test_gateway_circuit_opens_after_threshold(self):
        breaker = orchestrator.GatewayCircuitBreaker(2, 120, 180)
        breaker.record_transient_failure()
        self.assertFalse(breaker.snapshot()["open"])
        breaker.record_transient_failure()
        self.assertTrue(breaker.snapshot()["open"])

    def test_global_model_claims_span_batches_without_duplicate_leases(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            with connect(db) as connection:
                for batch_index in (1, 2):
                    batch = f"b{batch_index}"
                    connection.execute(
                        "INSERT INTO batches(name,folder_path,markdown_path,question_count,status,created_at,updated_at) "
                        "VALUES(?,?,?,?, 'ready','now','now')",
                        (batch, str(root / batch), str(root / f"{batch}.md"), 1),
                    )
                    batch_id = connection.execute(
                        "SELECT id FROM batches WHERE name=?", (batch,)
                    ).fetchone()[0]
                    prompt = f"实现后端服务 {batch_index}"
                    digest = orchestrator.prompt_hash(prompt)
                    connection.execute(
                        "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,"
                        "task_type,difficulty,languages,reproducibility,mechanical_qc,qc_decision,qc_prompt_sha256,status,created_at,updated_at) "
                        "VALUES(?,1,?,?,?,'title',?,?,'0-1 代码生成','中等','Python','无外部依赖','pass','pass',?,'approved','now','now')",
                        (batch_id, f"{batch}-001", "q001", str(root / batch / "q001"), prompt, digest, digest),
                    )
                connection.commit()

            first = orchestrator._claim_rows(db, "owner-a", "model", 1, 3600)
            second = orchestrator._claim_rows(db, "owner-b", "model", 2, 3600)
            self.assertEqual([row["task_id"] for row in first], ["b1-001"])
            self.assertEqual([row["task_id"] for row in second], ["b2-001"])
            orchestrator.release_question_lease(db, int(first[0]["id"]), "model", "owner-a")
            reclaimed = orchestrator._claim_rows(db, "owner-c", "model", 1, 3600)
            self.assertEqual([row["task_id"] for row in reclaimed], ["b1-001"])

    def test_global_delivery_claim_requires_successful_model_run(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            row = self.make_question(root, db)[0]
            with connect(db) as connection:
                connection.execute("UPDATE questions SET status='completed' WHERE id=?", (row["id"],))
                connection.execute(
                    "INSERT INTO runs(question_id,batch_run_id,launched_at,status) "
                    "VALUES(?,'success','now','succeeded')",
                    (row["id"],),
                )
                connection.commit()
            claimed = orchestrator._claim_rows(db, "delivery", "delivery", 1, 3600)
            self.assertEqual([item["task_id"] for item in claimed], [row["task_id"]])

    def test_monitor_reclaims_worker_that_never_produces_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            process = mock.Mock()
            process.wait.side_effect = [subprocess.TimeoutExpired(["docker"], 1), 0]
            with mock.patch.object(
                orchestrator.time, "monotonic", side_effect=[0.0, 6.0]
            ), mock.patch.object(
                orchestrator, "activity_signature", return_value=(0, 0, 0)
            ), mock.patch.object(
                orchestrator, "update_run_heartbeat"
            ) as heartbeat, mock.patch.object(
                orchestrator, "stop_worker_container"
            ) as stop:
                code, status, error = orchestrator.monitor_worker(
                    process, root / "db", 1, "run", "container",
                    root / "log", root / "claude", 100, 1, 5, 20, (0, 0, 0),
                )
            self.assertEqual((code, status), (-9, "timeout"))
            self.assertIn("no output", error)
            heartbeat.assert_called_once()
            stop.assert_called_once_with("container")

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

    def test_invalid_worker_configuration_is_recorded_before_launch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "production.sqlite3"
            row = self.make_question(root, db)[0]
            env = root / ".env"
            env.write_text("CC_SWITCH_MODEL=model\n", encoding="utf-8")
            with mock.patch.object(orchestrator.subprocess, "Popen") as popen:
                task_id, code, message = orchestrator.run_one(
                    db, row, env, root / "runs", "image", 1.0, "2g", 60,
                )
            self.assertEqual((task_id, code), ("b-001", 78))
            self.assertIn("missing worker configuration", message)
            popen.assert_not_called()
            with connect(db) as connection:
                run = connection.execute("SELECT status,exit_code FROM runs").fetchone()
                question = connection.execute("SELECT status FROM questions").fetchone()
            self.assertEqual(tuple(run), ("failed", 78))
            self.assertEqual(question[0], "approved")

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
