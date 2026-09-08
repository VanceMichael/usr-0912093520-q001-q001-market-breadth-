import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
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
    def test_docker_command_contains_only_exact_prompt_as_user_content(self):
        prompt = "实现一个唯一的跨模块状态恢复流程。"
        command = auto_pipeline.build_docker_claude_command(
            "docker", "claude-cli:latest", "claude", Path("D:/batch/q001"),
            Path("D:/batch/.runs/claude-home"), "ccusr-1-1", 1, prompt,
        )

        self.assertEqual(command[-3:], ["claude", "--dangerously-skip-permissions", prompt])
        self.assertEqual(command.count(prompt), 1)
        self.assertIn("ccusr.pipeline_job=1", command)
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
        ):
            with self.assertRaisesRegex(RuntimeError, "Docker 引擎未运行"):
                pipeline.docker_preflight("claude-cli:latest", "claude")

    def test_schema_includes_pipeline_pid_column(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "production.sqlite3"
            migrated = auto_pipeline.connect(database)
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(pipeline_jobs)")}
            migrated.close()

        self.assertIn("pid", columns)

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
