import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.batch_pipeline import connect, create_batch, set_repository
from tools.task_maintenance import _render_command, begin_takeover, finish_takeover, reset_question


def spec() -> dict:
    return {
        "batch": "0911",
        "brief": "人工接管测试",
        "questions": [{
            "folder": "q001",
            "task_id": "0911-001",
            "title": "状态恢复服务",
            "prompt": "实现一个纯后端状态恢复服务，处理并发写入、中断恢复和可核验的持久化结果。",
            "task_type": "0-1 代码生成",
            "difficulty": "困难",
            "languages": ["Python"],
            "repo_url": "https://github.com/example/project",
            "initial_snapshot": "",
            "reproducibility": "无外部依赖",
            "expected_areas": ["service", "tests"],
            "difficulty_evidence": ["需要处理并发状态和中断恢复"],
            "similarity_tags": ["state-recovery"],
        }],
    }


class TaskMaintenanceTests(unittest.TestCase):
    def test_takeover_command_uses_windows_quoting_for_bind_mounts(self):
        command = ["docker", "run", "-v", r"C:\work folder\q001:/workspace", "worker:test"]
        with mock.patch("tools.task_maintenance.os.name", "nt"):
            rendered = _render_command(command)
        self.assertIn('"C:\\work folder\\q001:/workspace"', rendered)

    def prepare(self, root: Path) -> tuple[Path, object, Path]:
        database = root / "production.sqlite3"
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(spec(), ensure_ascii=False), encoding="utf-8")
        with connect(database) as connection:
            create_batch(connection, root, spec_path)
            question = connection.execute("SELECT * FROM questions").fetchone()
            repository = "https://github.com/example/project"
            set_repository(
                connection, "0911", 1, repository,
                f"{repository}/commit/{question['local_initial_sha']}",
            )
            trajectory_root = root / "runs" / "attempt-1" / "claude"
            trajectory_root.mkdir(parents=True)
            events = [
                {
                    "type": "user", "sessionId": "session-1", "promptId": "prompt-1",
                    "message": {"role": "user", "content": question["prompt"]},
                },
                {
                    "type": "assistant", "sessionId": "session-1",
                    "message": {"role": "assistant", "content": [{
                        "type": "tool_use", "id": "tool-1", "name": "Read",
                        "input": {"file_path": "/workspace/README.md"},
                    }]},
                },
                {
                    "type": "user", "sessionId": "session-1", "promptId": "prompt-1",
                    "message": {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": "tool-1", "content": "ok",
                    }]},
                },
            ]
            (trajectory_root / "session-1.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
                encoding="utf-8",
            )
            connection.execute(
                "INSERT INTO runs(question_id,batch_run_id,launched_at,model,harness,"
                "harness_version,session_id,trajectory_root,status,started_at) "
                "VALUES(?,'attempt-1','now','claude-test','Claude Code','2.1.0',"
                "'session-1',?,'failed','now')",
                (question["id"], str(trajectory_root)),
            )
            connection.execute(
                "UPDATE questions SET status='approved' WHERE id=?", (question["id"],),
            )
            connection.commit()
            question = connection.execute("SELECT * FROM questions").fetchone()
        (root / ".env").write_text(
            "CC_SWITCH_BASE_URL=https://relay.example.com\n"
            "CC_SWITCH_API_KEY=secret\n"
            "CC_SWITCH_MODEL=claude-test\n",
            encoding="utf-8",
        )
        return database, question, trajectory_root

    def test_takeover_requires_a_new_turn_before_releasing_maintenance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, question, trajectory_root = self.prepare(root)
            result = begin_takeover(
                database, question["id"], root / ".env", "worker:test", 1, "2g",
            )
            self.assertIn("docker run --rm -it", result["command"])
            self.assertIn("--resume session-1", result["command"])
            self.assertIn(f"{question['folder_path']}:/workspace", result["command"])
            self.assertNotIn(str(root.resolve()) + ":/workspace", result["command"])
            with self.assertRaisesRegex(ValueError, "尚未产生新的用户轮次"):
                finish_takeover(database, question["id"])

            trajectory = trajectory_root / "session-1.jsonl"
            with trajectory.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "user", "sessionId": "session-1", "promptId": "prompt-continue",
                    "message": {"role": "user", "content": "继续"},
                }, ensure_ascii=False) + "\n")
                handle.write(json.dumps({
                    "type": "assistant", "sessionId": "session-1",
                    "message": {"role": "assistant", "content": [{
                        "type": "text", "text": "已经完成剩余实现并验证结果。",
                    }]},
                }, ensure_ascii=False) + "\n")
            workspace = Path(question["folder_path"])
            (workspace / "service.py").write_text("value = 1\n", encoding="utf-8")
            finished = finish_takeover(database, question["id"])
            self.assertEqual(finished["session_id"], "session-1")
            with connect(database) as connection:
                current = connection.execute(
                    "SELECT status,maintenance_mode FROM questions WHERE id=?",
                    (question["id"],),
                ).fetchone()
                run = connection.execute(
                    "SELECT status FROM runs WHERE batch_run_id=?", (result["run_id"],),
                ).fetchone()
            self.assertEqual((current["status"], current["maintenance_mode"]), ("completed", 0))
            self.assertEqual(run["status"], "succeeded")

    def test_full_reset_restores_snapshot_and_removes_local_run_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, question, trajectory_root = self.prepare(root)
            workspace = Path(question["folder_path"])
            tracked = workspace / ".gitignore"
            original = tracked.read_text(encoding="utf-8")
            tracked.write_text("changed\n", encoding="utf-8")
            (workspace / "untracked.tmp").write_text("temporary\n", encoding="utf-8")
            with mock.patch("tools.task_maintenance.subprocess.run", wraps=subprocess.run) as runner:
                result = reset_question(database, question["id"], "测试完整重置")
            self.assertEqual(result["removed_runs"], 1)
            self.assertEqual(tracked.read_text(encoding="utf-8"), original)
            self.assertFalse((workspace / "untracked.tmp").exists())
            self.assertFalse(trajectory_root.parent.exists())
            with connect(database) as connection:
                current = connection.execute(
                    "SELECT status,maintenance_mode,reset_count FROM questions WHERE id=?",
                    (question["id"],),
                ).fetchone()
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE question_id=?", (question["id"],),
                ).fetchone()[0], 0)
                audit = connection.execute(
                    "SELECT reason,removed_runs FROM question_reset_audit WHERE question_id=?",
                    (question["id"],),
                ).fetchone()
            self.assertEqual(tuple(current), ("approved", 0, 1))
            self.assertEqual(tuple(audit), ("测试完整重置", 1))
            self.assertFalse(any(call.args[0][:3] == ["git", "reset", "--hard"] for call in runner.call_args_list))

    def test_full_reset_refuses_a_question_already_submitted_to_solo2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, question, _trajectory_root = self.prepare(root)
            with connect(database) as connection:
                columns = [
                    row["name"] for row in connection.execute("PRAGMA table_info(records)")
                    if row["name"] != "id"
                ]
                record: dict[str, object] = {}
                for name in columns:
                    if name == "question_id":
                        record[name] = question["id"]
                    elif name == "turn_no":
                        record[name] = 1
                    elif name.endswith("_score"):
                        record[name] = 3
                    elif name in {
                        "human_authored", "human_qc_approved", "delivery_qc_passed",
                        "evidence_gate_passed", "history_gate_passed", "is_continuation",
                        "continuation_count",
                    }:
                        record[name] = 0
                    elif name == "record_id":
                        record[name] = "0911-001-T01"
                    elif name == "session_id":
                        record[name] = "session-1"
                    elif name in {"turn_id", "raw_turn_id"}:
                        record[name] = "prompt-1"
                    elif name == "created_at":
                        record[name] = "2026-09-01T00:00:00+08:00"
                    else:
                        record[name] = "value"
                connection.execute(
                    f"INSERT INTO records({','.join(record)}) VALUES({','.join('?' for _ in record)})",
                    list(record.values()),
                )
                connection.execute(
                    "INSERT INTO solo2_submissions(record_id,question_id,remote_submission_id,"
                    "status,attempt_count,submitted_at,updated_at) "
                    "VALUES('0911-001-T01',?,'remote-1','succeeded',1,'now','now')",
                    (question["id"],),
                )
                connection.commit()
            with self.assertRaisesRegex(ValueError, "已经提交到 SOLO2"):
                reset_question(database, question["id"])


if __name__ == "__main__":
    unittest.main()
