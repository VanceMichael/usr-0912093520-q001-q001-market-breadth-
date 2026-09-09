import importlib.util
import io
import json
import shlex
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

    def test_claude_command_skips_permission_prompts_and_keeps_exact_prompt(self):
        prompt = "  原始 prompt，不能改写。\n第二行  "
        command = launch_task.build_claude_command("/usr/local/bin/claude", prompt)
        self.assertEqual(
            command,
            ["/usr/local/bin/claude", "--dangerously-skip-permissions", prompt],
        )

    def test_headless_command_cannot_wait_for_trust_or_permission_input(self):
        prompt = "服务器原始 prompt"
        command = launch_task.build_claude_command(
            "/usr/local/bin/claude", prompt, headless=True
        )
        self.assertEqual(command[-1], prompt)
        self.assertIn("--print", command)
        self.assertIn("--verbose", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--permission-mode", command)
        self.assertIn("bypassPermissions", command)
        self.assertIn("--permission-prompts", command)
        self.assertIn("none", command)

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

    def test_headless_environment_marks_noninteractive_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            config = launch_task.load_claude_config(self.write_env(Path(directory)))
            environment = launch_task.build_claude_environment(config, headless=True)
            self.assertEqual(environment["CI"], "1")

    def test_auto_mode_uses_server_when_iterm_is_unavailable(self):
        with mock.patch.object(run_tasks.platform, "system", return_value="Linux"):
            self.assertEqual(run_tasks.select_launch_mode("auto"), "server")

    def test_auto_mode_uses_visible_headless_iterm_on_macos(self):
        with mock.patch.object(
            run_tasks.platform, "system", return_value="Darwin"
        ), mock.patch.object(run_tasks, "iterm_available", return_value=True):
            self.assertEqual(run_tasks.select_launch_mode("auto"), "iterm-headless")

    def test_explicit_iterm_mode_remains_interactive(self):
        self.assertEqual(run_tasks.select_launch_mode("iterm"), "iterm")

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
            self.assertEqual(
                command,
                ["/usr/local/bin/claude", "--dangerously-skip-permissions", row["prompt"]],
            )
            self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "super-secret")

    def test_preview_redacts_prompt_and_all_env_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root, "preview-secret")
            stdout = io.StringIO()
            argv = [
                "run_tasks.py", "--db", str(database), "--batch", "0911",
                "--env-file", str(env_file), "--select", "1", "--mode", "server",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                run_tasks, "find_claude", return_value="/usr/local/bin/claude"
            ), mock.patch.object(
                run_tasks, "claude_version", return_value="2.1.259"
            ), redirect_stdout(stdout):
                self.assertEqual(run_tasks.main(), 0)
            output = stdout.getvalue()
            self.assertIn(f"cd {shlex.quote(str(row['folder_path']))}", output)
            self.assertIn(
                "claude --print --verbose --output-format stream-json "
                "--dangerously-skip-permissions "
                "--permission-mode bypassPermissions --permission-prompts none "
                "<SQLite 原始 prompt>",
                output,
            )
            for secret in ("preview-secret", "relay.example.com", "claude-test", row["prompt"]):
                self.assertNotIn(secret, output)

    def test_stream_display_shows_tool_activity_and_results(self):
        display = launch_task.StreamDisplay()
        assistant = json.dumps({
            "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-1", "name": "Bash",
                "input": {"command": "python3 -m unittest"},
            }]},
        })
        result = json.dumps({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-1",
                "content": "Ran 12 tests\nOK",
            }]},
        })
        self.assertEqual(display.render(assistant), ["\n▶ Bash  python3 -m unittest"])
        rendered = display.render(result)
        self.assertIn("完成 Bash", rendered[0])
        self.assertIn("Ran 12 tests", rendered[0])

    def test_stream_display_keeps_non_json_diagnostics_visible(self):
        display = launch_task.StreamDisplay(("secret-token",))
        self.assertEqual(
            display.render("model warning secret-token\n"),
            ["model warning [已隐藏]"],
        )

    def test_headless_stream_wraps_windows_command_shims(self):
        process = mock.Mock()
        process.stdout = []
        process.wait.return_value = 0
        command = ["C:/npm/claude.cmd", "--print", "prompt"]
        environment = {"COMSPEC": "C:/Windows/System32/cmd.exe"}
        with mock.patch.object(launch_task.sys, "platform", "win32"), mock.patch.object(
            launch_task.subprocess, "Popen", return_value=process
        ) as popen_mock:
            self.assertEqual(launch_task.run_headless_stream(command, environment), 0)
        launched = popen_mock.call_args.args[0]
        self.assertEqual(launched[:4], [
            "C:/Windows/System32/cmd.exe", "/d", "/s", "/c",
        ])
        self.assertIn("claude.cmd", launched[4])

    def test_mocked_macos_launch_registers_claude_run_without_config_or_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root, "generated-file-secret")
            argv = [
                "run_tasks.py", "--db", str(database), "--batch", "0911",
                "--env-file", str(env_file), "--select", "1", "--launch", "--mode", "iterm",
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
            launcher = next((root / "0911/.runs").glob("**/launch.command"))
            self.assertTrue(
                launcher.read_text(encoding="utf-8").startswith("#!/bin/zsh\nset -eu\n")
            )
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

    def test_mocked_windows_launch_uses_powershell_without_config_or_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root, "windows-file-secret")
            argv = [
                "run_tasks.py", "--db", str(database), "--batch", "0911",
                "--env-file", str(env_file), "--select", "1", "--launch",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                run_tasks, "find_claude", return_value="C:/npm/claude.cmd"
            ), mock.patch.object(
                run_tasks, "claude_version", return_value="2.1.259"
            ), mock.patch.object(
                run_tasks, "powershell_executable", return_value="powershell.exe"
            ), mock.patch.object(
                run_tasks, "open_windows"
            ) as open_mock, mock.patch.object(
                run_tasks.platform, "system", return_value="Windows"
            ):
                self.assertEqual(run_tasks.main(), 0)
            launcher = next((root / "0911/.runs").glob("**/launch.ps1"))
            generated = launcher.read_text(encoding="utf-8-sig")
            open_mock.assert_called_once_with(launcher)
            for forbidden in (
                "windows-file-secret", "relay.example.com", "claude-test", row["prompt"]
            ):
                self.assertNotIn(forbidden, generated)
            self.assertIn("--question-id", generated)
            self.assertIn("--headless", generated)
            self.assertIn("C:/npm/claude.cmd", generated)
            connection = connect(database)
            run = connection.execute("SELECT * FROM runs").fetchone()
            status = connection.execute(
                "SELECT status FROM questions WHERE id=?", (row["id"],)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(run["harness"], "Claude Code")
            self.assertEqual(status, "running")

    def test_windows_command_shim_runs_without_changing_claude_arguments(self):
        command = ["claude.cmd", "--dangerously-skip-permissions", "原始 prompt"]
        environment = {"ANTHROPIC_MODEL": "claude-test"}
        completed = subprocess.CompletedProcess(command, 7)
        with mock.patch.object(
            launch_task.subprocess, "run", return_value=completed
        ) as run_mock:
            self.assertEqual(
                launch_task.run_claude_on_windows(command, environment), 7
            )
        run_mock.assert_called_once_with(command, env=environment, check=False)
    def test_mocked_server_launch_is_detached_and_records_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, row = self.make_batch(root)
            env_file = self.write_env(root)
            argv = [
                "run_tasks.py", "--db", str(database), "--batch", "0911",
                "--env-file", str(env_file), "--select", "1", "--launch",
                "--mode", "server",
            ]
            process = mock.Mock(pid=43210)
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                run_tasks, "find_claude", return_value="/usr/local/bin/claude"
            ), mock.patch.object(
                run_tasks, "claude_version", return_value="2.1.259"
            ), mock.patch.object(
                run_tasks, "open_server", return_value=process
            ) as server_mock:
                self.assertEqual(run_tasks.main(), 0)
            server_mock.assert_called_once()
            run_root = next((root / "0911/.runs").iterdir()) / row["task_id"]
            self.assertEqual((run_root / "process.pid").read_text(), "43210\n")
            self.assertIn('"$@"', (run_root / "launch.command").read_text())

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
