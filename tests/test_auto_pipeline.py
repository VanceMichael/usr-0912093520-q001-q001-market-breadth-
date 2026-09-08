import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AUTO_SPEC = importlib.util.spec_from_file_location(
    "auto_pipeline", ROOT / "tools/auto_pipeline.py"
)
auto_pipeline = importlib.util.module_from_spec(AUTO_SPEC)
assert AUTO_SPEC and AUTO_SPEC.loader
sys.modules[AUTO_SPEC.name] = auto_pipeline
AUTO_SPEC.loader.exec_module(auto_pipeline)

LOCATOR_SPEC = importlib.util.spec_from_file_location(
    "find_claude_turns",
    ROOT / ".agents/skills/cc-usr-delivery-producer/scripts/find_claude_turns.py",
)
find_claude_turns = importlib.util.module_from_spec(LOCATOR_SPEC)
assert LOCATOR_SPEC and LOCATOR_SPEC.loader
sys.modules[LOCATOR_SPEC.name] = find_claude_turns
LOCATOR_SPEC.loader.exec_module(find_claude_turns)


class AutoPipelineTests(unittest.TestCase):
    def test_codex_event_accepts_scalar_json_output(self):
        self.assertEqual(auto_pipeline.Pipeline.codex_event('"plain output"'), '"plain output"')

    def test_codex_sends_complete_prompt_through_stdin(self):
        pipeline = auto_pipeline.Pipeline(
            Path("production.sqlite3"), "0911", 1, lambda _message: None
        )
        prompt = "批次名：0911\n题目数量：5\n出题要求：城市服务"
        with mock.patch.object(auto_pipeline.shutil, "which", return_value="codex.CMD"), mock.patch.object(
            pipeline, "run_command", return_value=(0, "")
        ) as run_command:
            self.assertEqual(pipeline.codex(prompt), 0)

        command = run_command.call_args.args[0]
        self.assertEqual(command[-1], "-")
        self.assertEqual(run_command.call_args.kwargs["stdin_text"], prompt)

    def test_docker_command_contains_only_exact_prompt_as_user_content(self):
        prompt = "实现一个唯一的跨模块状态恢复流程。"
        command = auto_pipeline.build_docker_claude_command(
            "docker", "claude-cli:latest", "claude", Path("D:/batch/q001"),
            Path("D:/batch/.runs/claude-home"), "ccusr-1-1", 1, prompt,
        )

        self.assertEqual(command[-3:], ["claude", "--dangerously-skip-permissions", prompt])
        self.assertEqual(command.count(prompt), 1)
        self.assertIn("ccusr.pipeline_job=1", command)
        self.assertIn("1000:1000", command)
        self.assertIn("HOME=/home/node", command)
        self.assertIn("D:\\batch\\.runs\\claude-home:/home/node/.claude", command)
        for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
            self.assertIn(name, command)
        self.assertNotIn("https://relay.example.com", command)
        self.assertNotIn("secret-key-value", command)
        self.assertNotIn("claude-configured-model", command)

    def test_locator_accepts_container_workspace_and_preserves_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "projects" / "-workspace"
            project.mkdir(parents=True)
            prompt = "实现容器中的精确轨迹定位。"
            event = {
                "type": "user",
                "cwd": "/workspace",
                "sessionId": "session-container-001",
                "promptId": "prompt-container-001",
                "timestamp": "2026-09-08T10:00:01+08:00",
                "message": {"role": "user", "content": prompt},
            }
            (project / "session-container-001.jsonl").write_text(
                json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            result = find_claude_turns.locate(
                root, root / "host-question", prompt,
                datetime.fromisoformat("2026-09-08T10:00:00+08:00"),
                "/workspace",
            )

        self.assertEqual(result["session_id"], "session-container-001")
        self.assertEqual(result["turns"][0]["prompt_id"], "prompt-container-001")
        self.assertEqual(result["turns"][0]["user_prompt"], prompt)

    def test_docker_preflight_reports_stopped_engine(self):
        pipeline = auto_pipeline.Pipeline(Path("production.sqlite3"), "0911", 1, lambda _message: None)
        failure = mock.Mock(returncode=1, stdout="cannot connect to Docker engine")
        with mock.patch.object(auto_pipeline.shutil, "which", return_value="docker"), mock.patch.object(
            auto_pipeline.subprocess, "run", return_value=failure
        ), mock.patch.object(
            auto_pipeline, "repair_docker_engine", return_value=(False, "Docker Desktop 启动失败")
        ), mock.patch.object(
            pipeline, "log"
        ):
            with self.assertRaisesRegex(RuntimeError, "Docker 引擎未运行"):
                pipeline.docker_preflight("claude-cli:latest", "claude")

    def test_docker_preflight_continues_after_engine_repair(self):
        pipeline = auto_pipeline.Pipeline(Path("production.sqlite3"), "0911", 1, lambda _message: None)
        image = json.dumps([{"Config": {"User": "node", "Labels": {"ccusr.claude.nonroot": "true"}}}])
        results = [
            mock.Mock(returncode=0, stdout=image),
            mock.Mock(returncode=0, stdout="1000\n"),
            mock.Mock(returncode=0, stdout="2.1.263 (Claude Code)\n"),
        ]
        with mock.patch.object(auto_pipeline.shutil, "which", return_value="docker"), mock.patch.object(
            auto_pipeline, "docker_info", return_value=mock.Mock(returncode=1, stdout="not running")
        ), mock.patch.object(
            auto_pipeline, "repair_docker_engine", return_value=(True, "Docker 引擎可以连接")
        ), mock.patch.object(auto_pipeline.subprocess, "run", side_effect=results), mock.patch.object(
            pipeline, "log"
        ):
            version = pipeline.docker_preflight("claude-cli:latest", "claude")

        self.assertEqual(version, "2.1.263")

    def test_schema_includes_pipeline_pid_column(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "production.sqlite3"
            migrated = auto_pipeline.connect(database)
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(pipeline_jobs)")}
            item_columns = {
                row["name"] for row in migrated.execute("PRAGMA table_info(pipeline_items)")
            }
            migrated.close()

        self.assertIn("pid", columns)
        self.assertIn("retry_of_job_id", columns)
        self.assertTrue({"heartbeat_at", "activity_at", "health_status", "health_detail"} <= item_columns)

    def test_question_progress_treats_successful_run_as_completed_model_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connection = auto_pipeline.connect(database)
            created = "2026-09-08T10:00:00+08:00"
            batch_id = connection.execute(
                "INSERT INTO batches(name,folder_path,markdown_path,question_count,status,created_at,updated_at) "
                "VALUES('0911',?,?,1,'active',?,?)",
                (str(root), str(root / "questions.md"), created, created),
            ).lastrowid
            question_id = connection.execute(
                "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,"
                "task_type,difficulty,languages,repo_url,initial_snapshot,local_initial_sha,reproducibility,"
                "created_at,updated_at,status) VALUES(?,1,'0911-001','q001',?,'标题','需求',?,'0-1 代码生成',"
                "'简单','[\"Python\"]','https://github.com/example/repo','https://github.com/example/repo/commit/"
                "0000000000000000000000000000000000000000','0000000000000000000000000000000000000000',"
                "'无外部依赖',?,?,'completed')",
                (batch_id, str(root / "q001"), auto_pipeline.prompt_hash("需求"), created, created),
            ).lastrowid
            connection.execute(
                "INSERT INTO runs(question_id,batch_run_id,launched_at) VALUES(?,?,?)",
                (question_id, "run-1", created),
            )
            connection.commit()
            rows = auto_pipeline.question_rows(connection, "0911")
            connection.close()

            pipeline = auto_pipeline.Pipeline(database, "0911", 1, lambda _message: None)
            progress = pipeline.question_progress(rows)

        self.assertTrue(progress[question_id]["model_done"])
        self.assertEqual(progress[question_id]["record_count"], 0)

    def test_restore_question_workspace_removes_failed_attempt_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "question"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            (repo / "app.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "app.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "baseline"],
                check=True,
            )
            baseline = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            (repo / "app.txt").write_text("failed attempt\n", encoding="utf-8")
            (repo / "new.txt").write_text("generated\n", encoding="utf-8")
            (repo / ".secret").write_text("credential\n", encoding="utf-8")
            (repo / ".gitignore").write_text(".secret\n", encoding="utf-8")

            pipeline = auto_pipeline.Pipeline(Path(directory) / "db.sqlite3", "0911", 1, lambda _message: None)
            with mock.patch.object(pipeline, "log"):
                pipeline.restore_question_workspace({
                    "task_id": "0911-001",
                    "folder_path": str(repo),
                    "local_initial_sha": baseline,
                    "initial_snapshot": f"https://github.com/example/repo/commit/{baseline}",
                })

            self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "baseline\n")
            self.assertFalse((repo / "new.txt").exists())
            self.assertFalse((repo / ".secret").exists())
            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                text=True, stdout=subprocess.PIPE, check=True,
            )
            self.assertEqual(status.stdout, "")

    def test_docker_run_health_uses_recent_trajectory_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "session.jsonl").write_text("{}\n", encoding="utf-8")
            running = mock.Mock(returncode=0, stdout="true|running\n")
            with mock.patch.object(auto_pipeline.subprocess, "run", return_value=running):
                status, detail, activity = auto_pipeline.Pipeline.docker_run_health(
                    "docker", "ccusr-1-1", root, "2026-09-08T10:00:00+08:00",
                )

        self.assertEqual(status, "healthy")
        self.assertIn("容器运行正常", detail)
        self.assertNotEqual(activity, "2026-09-08T10:00:00+08:00")

    def test_docker_run_health_marks_long_silence_as_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            fallback = (datetime.now().astimezone() - timedelta(minutes=11)).isoformat(timespec="seconds")
            running = mock.Mock(returncode=0, stdout="true|running\n")
            with mock.patch.object(auto_pipeline.subprocess, "run", return_value=running):
                status, detail, _activity = auto_pipeline.Pipeline.docker_run_health(
                    "docker", "ccusr-1-1", Path(directory), fallback,
                )

        self.assertEqual(status, "idle")
        self.assertIn("没有更新", detail)

    def test_docker_run_health_marks_thirty_minute_silence_as_stalled(self):
        with tempfile.TemporaryDirectory() as directory:
            fallback = (datetime.now().astimezone() - timedelta(minutes=31)).isoformat(timespec="seconds")
            running = mock.Mock(returncode=0, stdout="true|running\n")
            with mock.patch.object(auto_pipeline.subprocess, "run", return_value=running):
                status, detail, _activity = auto_pipeline.Pipeline.docker_run_health(
                    "docker", "ccusr-1-1", Path(directory), fallback,
                )

        self.assertEqual(status, "stalled")
        self.assertIn("判定为停滞", detail)

    def test_interrupted_job_cannot_be_overwritten_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "production.sqlite3"
            connection = auto_pipeline.connect(database)
            connection.execute(
                "INSERT INTO pipeline_jobs(id,batch_name,question_count,docker_image,status,created_at) "
                "VALUES(1,'0911',1,'claude-cli:latest','interrupted','2026-09-08T10:00:00+08:00')"
            )
            connection.commit()
            connection.close()
            pipeline = auto_pipeline.Pipeline(database, "0911", 1, lambda _message: None)

            pipeline.set_job(status="completed", finished_at="2026-09-08T11:00:00+08:00")

            connection = auto_pipeline.connect(database)
            status = connection.execute(
                "SELECT status FROM pipeline_jobs WHERE id=1"
            ).fetchone()["status"]
            connection.close()
        self.assertEqual(status, "interrupted")

    def test_pipeline_log_redacts_configured_values(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "production.sqlite3"
            connection = auto_pipeline.connect(database)
            connection.execute(
                "INSERT INTO pipeline_jobs(id,batch_name,question_count,docker_image,status,created_at) "
                "VALUES(1,'0911',1,'claude-cli:latest','running','2026-09-08T10:00:00+08:00')"
            )
            connection.commit()
            connection.close()
            pipeline = auto_pipeline.Pipeline(database, "0911", 1, lambda _message: None)
            pipeline.redacted_values = {
                "https://relay.private.example.com", "secret-key-value", "private-model"
            }

            pipeline.log(
                "https://relay.private.example.com secret-key-value private-model"
            )

            connection = auto_pipeline.connect(database)
            output = connection.execute(
                "SELECT output FROM pipeline_jobs WHERE id=1"
            ).fetchone()["output"]
            connection.close()
        self.assertNotIn("relay.private", output)
        self.assertNotIn("secret-key-value", output)
        self.assertNotIn("private-model", output)
        self.assertEqual(output.count("[REDACTED]"), 3)


if __name__ == "__main__":
    unittest.main()
