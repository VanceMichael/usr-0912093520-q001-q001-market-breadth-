import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.trajectory_gate import TrajectoryGateError, validate_effective_trajectory


class TrajectoryGateTests(unittest.TestCase):
    def make_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "question"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        (repo / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "baseline"],
            check=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        return repo, sha

    def write_trajectory(self, root: Path, prompt: str, *, useful: bool) -> None:
        project = root / "projects" / "-workspace"
        project.mkdir(parents=True)
        events = [
            {"type": "user", "sessionId": "session-1", "promptId": "prompt-1", "message": {"role": "user", "content": prompt}},
        ]
        if useful:
            events += [
                {"type": "assistant", "sessionId": "session-1", "message": {"role": "assistant", "model": "auto_model/urm", "content": [{"type": "tool_use", "id": "tool-1", "name": "Write", "input": {}}]}},
                {"type": "user", "sessionId": "session-1", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]}},
                {"type": "assistant", "sessionId": "session-1", "message": {"role": "assistant", "model": "auto_model/urm", "content": [{"type": "text", "text": "完成并通过测试。"}]}},
            ]
        else:
            events.append(
                {"type": "assistant", "sessionId": "session-1", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "标题"}]}}
            )
        (project / "session-1.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )

    def test_effective_trajectory_requires_real_work_and_code_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            evidence = validate_effective_trajectory(root / "claude", prompt, repo, sha)
            self.assertEqual(evidence.session_id, "session-1")
            self.assertEqual(evidence.prompt_id, "prompt-1")
            self.assertEqual(evidence.tool_uses, 1)
            self.assertEqual(evidence.changed_files, 1)

    def test_synthetic_title_only_trajectory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=False)
            with self.assertRaisesRegex(TrajectoryGateError, "没有真实的 assistant 响应"):
                validate_effective_trajectory(root / "claude", prompt, repo, sha)

    def test_tool_call_without_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            trajectory = root / "claude" / "projects" / "-workspace" / "session-1.jsonl"
            events = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()]
            trajectory.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events if event["type"] != "user" or "tool_result" not in str(event)),
                encoding="utf-8",
            )
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            with self.assertRaisesRegex(TrajectoryGateError, "没有匹配结果"):
                validate_effective_trajectory(root / "claude", prompt, repo, sha)

    def test_external_instruction_file_read_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            trajectory = root / "claude" / "projects" / "-workspace" / "session-1.jsonl"
            events = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines()]
            events[1]["message"]["content"][0]["input"] = {"file_path": "/project/AGENTS.md"}
            trajectory.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8",
            )
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            with self.assertRaisesRegex(TrajectoryGateError, "工作区外的指令/配置"):
                validate_effective_trajectory(root / "claude", prompt, repo, sha)

    def test_trajectory_without_workspace_changes_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            with self.assertRaisesRegex(TrajectoryGateError, "没有任何代码变化"):
                validate_effective_trajectory(root / "claude", prompt, repo, sha)

    def test_internal_task_notification_is_not_a_prompt_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            path = root / "claude" / "projects" / "-workspace" / "session-1.jsonl"
            notification = {
                "type": "user", "sessionId": "session-1", "promptId": "internal-1",
                "message": {"role": "user", "content": (
                    "<task-notification><task-id>1</task-id><tool-use-id>t</tool-use-id>"
                    "<status>completed</status></task-notification>"
                )},
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(notification, ensure_ascii=False) + "\n")
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            evidence = validate_effective_trajectory(root / "claude", prompt, repo, sha)
            self.assertEqual(evidence.prompt_id, "prompt-1")

    def test_foreign_session_in_primary_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            path = root / "claude" / "projects" / "-workspace" / "session-1.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "assistant", "sessionId": "session-2",
                    "message": {"role": "assistant", "model": "m", "content": "other"},
                }) + "\n")
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            with self.assertRaisesRegex(TrajectoryGateError, "混入其他 SessionID"):
                validate_effective_trajectory(root / "claude", prompt, repo, sha)

    def test_duplicate_primary_trajectory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, sha = self.make_repo(root)
            prompt = "实现后端功能"
            self.write_trajectory(root / "claude", prompt, useful=True)
            source = root / "claude" / "projects" / "-workspace" / "session-1.jsonl"
            duplicate = source.parent / "session-2.jsonl"
            duplicate.write_text(
                source.read_text(encoding="utf-8").replace("session-1", "session-2"),
                encoding="utf-8",
            )
            (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
            with self.assertRaisesRegex(TrajectoryGateError, "多份主轨迹"):
                validate_effective_trajectory(root / "claude", prompt, repo, sha)


if __name__ == "__main__":
    unittest.main()
