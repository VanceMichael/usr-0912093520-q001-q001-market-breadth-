import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / ".agents/skills/cc-usr-claude-runner/scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

LAUNCH_SPEC = importlib.util.spec_from_file_location("launch_task", SCRIPT_DIR / "launch_task.py")
launch_task = importlib.util.module_from_spec(LAUNCH_SPEC)
assert LAUNCH_SPEC and LAUNCH_SPEC.loader
sys.modules[LAUNCH_SPEC.name] = launch_task
LAUNCH_SPEC.loader.exec_module(launch_task)

RUNNER_SPEC = importlib.util.spec_from_file_location("run_tasks", SCRIPT_DIR / "run_tasks.py")
run_tasks = importlib.util.module_from_spec(RUNNER_SPEC)
assert RUNNER_SPEC and RUNNER_SPEC.loader
sys.modules[RUNNER_SPEC.name] = run_tasks
RUNNER_SPEC.loader.exec_module(run_tasks)

from tools.batch_pipeline import connect, create_batch, prompt_hash, set_repository  # noqa: E402


def question_spec(batch: str) -> dict:
    return {
        "batch": batch,
        "brief": "跨模块工程任务",
        "questions": [{
            "folder": "q001",
            "task_id": f"{batch}-001",
            "title": "跨模块状态服务",
            "prompt": "从零构建一套跨模块状态服务，覆盖事件接收、持久化、推送、重连恢复、并发更新冲突和完整验证场景。",
            "task_type": "0-1 代码生成",
            "difficulty": "困难",
            "languages": ["Python", "TypeScript"],
            "repo_url": "https://github.com/example/project",
            "initial_snapshot": "",
            "reproducibility": "无外部依赖",
            "expected_areas": ["api", "web", "tests"],
            "difficulty_evidence": ["需要追踪跨模块状态和并发时序"],
            "similarity_tags": ["event-ordering", "state-consistency"],
        }],
    }


class RunnerTests(unittest.TestCase):
    def make_batch(self, root: Path, batch: str = "0911") -> tuple[Path, object]:
        database = root / "production.sqlite3"
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(question_spec(batch), ensure_ascii=False), encoding="utf-8")
        connection = connect(database)
        create_batch(connection, root, spec_path)
        row = connection.execute("SELECT * FROM questions").fetchone()
        set_repository(
            connection,
            batch,
            1,
            "https://github.com/example/project",
            f"https://github.com/example/project/commit/{row['local_initial_sha']}",
        )
        row = connection.execute("SELECT * FROM questions").fetchone()
        connection.execute(
            "UPDATE questions SET mechanical_qc='pass', qc_decision='pass', "
            "qc_prompt_sha256=?, human_approved=0, human_reviewer='', "
            "approved_at='', status='approved' WHERE id=?",
            (prompt_hash(row["prompt"]), row["id"]),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM questions WHERE id=?", (row["id"],)).fetchone()
        connection.close()
        return database, row

    def write_env(self, root: Path, key: str = "super-secret") -> Path:
        path = root / ".env"
        path.write_text(
            "CC_SWITCH_BASE_URL=https://relay.example.com/v1\n"
            "CC_SWITCH_MODEL=claude-test\n"
            f"CC_SWITCH_API_KEY={key}\n",
            encoding="utf-8",
        )
        return path

    def test_claude_command_has_only_binary_and_exact_prompt(self):
        prompt = "  原始 prompt，不能改写。\n第二行  "
        command = launch_task.build_claude_command("/usr/local/bin/claude", prompt)
        self.assertEqual(command, ["/usr/local/bin/claude", prompt])

    def test_claude_version_keeps_only_numeric_version(self):
        completed = subprocess.CompletedProcess(
            ["claude", "--version"], 0, "2.1.259 (Claude Code)\n", ""
        )
        with mock.patch.object(run_tasks.subprocess, "run", return_value=completed):
            self.assertEqual(run_tasks.claude_version("/usr/local/bin/claude"), "2.1.259")

    def test_claude_version_requires_numeric_version(self):
        completed = subprocess.CompletedProcess(
            ["claude", "--version"], 0, "Claude Code\n", ""
        )
        with mock.patch.object(run_tasks.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "numeric version"):
                run_tasks.claude_version("/usr/local/bin/claude")

    def test_root_env_maps_to_claude_environment_without_cli_options(self):
        with tempfile.TemporaryDirectory() as directory:
            config = launch_task.load_claude_config(self.write_env(Path(directory)))
            environment = launch_task.build_claude_environment(config)
            self.assertEqual(environment["ANTHROPIC_BASE_URL"], "https://relay.example.com/v1")
            self.assertEqual(environment["ANTHROPIC_MODEL"], "claude-test")
            self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "super-secret")

    def test_launch_helper_uses_question_directory_and_exact_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root)
            argv = [
                "launch_task.py", "--env-file", str(env_file), "--db", str(database),
                "--question-id", str(row["id"]), "--claude", "/usr/local/bin/claude",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                launch_task.os, "chdir"
            ) as chdir_mock, mock.patch.object(
                launch_task.os, "execvpe"
            ) as exec_mock:
                self.assertEqual(launch_task.main(), 1)
            chdir_mock.assert_called_once_with(Path(row["folder_path"]).resolve())
            executable, command, environment = exec_mock.call_args.args
            self.assertEqual(executable, "/usr/local/bin/claude")
            self.assertEqual(command, ["/usr/local/bin/claude", row["prompt"]])
            self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "super-secret")

    def test_preview_redacts_prompt_and_all_env_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root, "preview-secret")
            stdout = io.StringIO()
            argv = [
                "run_tasks.py", "--db", str(database), "--batch", "0911",
                "--env-file", str(env_file), "--select", "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                run_tasks, "find_claude", return_value="/usr/local/bin/claude"
            ), mock.patch.object(
                run_tasks, "claude_version", return_value="2.1.259"
            ), redirect_stdout(stdout):
                self.assertEqual(run_tasks.main(), 0)
            output = stdout.getvalue()
            self.assertIn(f"cd {row['folder_path']}", output)
            self.assertIn("claude <SQLite 原始 prompt>", output)
            for secret in ("preview-secret", "relay.example.com", "claude-test", row["prompt"]):
                self.assertNotIn(secret, output)

    def test_mocked_launch_registers_claude_run_without_config_or_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root, "generated-file-secret")
            argv = [
                "run_tasks.py", "--db", str(database), "--batch", "0911",
                "--env-file", str(env_file), "--select", "1", "--launch",
            ]
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                run_tasks, "find_claude", return_value="/usr/local/bin/claude"
            ), mock.patch.object(
                run_tasks, "claude_version", return_value="2.1.259"
            ), mock.patch.object(
                run_tasks, "iterm_available", return_value=True
            ), mock.patch.object(
                run_tasks, "open_iterm", return_value=completed
            ) as open_mock, mock.patch.object(
                run_tasks.platform, "system", return_value="Darwin"
            ):
                self.assertEqual(run_tasks.main(), 0)
            open_mock.assert_called_once()
            generated = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "0911/.runs").glob("**/*") if path.is_file()
            )
            for forbidden in (
                "generated-file-secret", "relay.example.com", "claude-test", row["prompt"]
            ):
                self.assertNotIn(forbidden, generated)
            self.assertIn("--question-id", generated)
            connection = connect(database)
            run = connection.execute("SELECT * FROM runs").fetchone()
            status = connection.execute(
                "SELECT status FROM questions WHERE id=?", (row["id"],)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(run["harness"], "Claude Code")
            self.assertEqual(run["codex_version"], "2.1.259")
            self.assertEqual(run["harness_version"], "2.1.259")
            self.assertEqual(status, "running")

    def test_changed_prompt_is_blocked_after_qc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            connection = connect(database)
            connection.execute(
                "UPDATE questions SET prompt=prompt || ' changed' WHERE id=?", (row["id"],)
            )
            connection.commit()
            changed = connection.execute(
                "SELECT * FROM questions WHERE id=?", (row["id"],)
            ).fetchone()
            connection.close()
            self.assertFalse(run_tasks.is_ready(changed))
            with self.assertRaisesRegex(ValueError, "not QC-passed"):
                run_tasks.validate_question(changed)


if __name__ == "__main__":
    unittest.main()
