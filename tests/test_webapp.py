import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.batch_pipeline import connect, create_batch, prompt_hash, set_repository
from webapp.server import ConsoleData


def question_spec(batch: str) -> dict:
    return {
        "batch": batch,
        "brief": "生产控制台测试批次",
        "questions": [{
            "folder": "q001",
            "task_id": f"{batch}-001",
            "title": "跨模块状态服务",
            "prompt": "从零构建一套跨模块状态服务，覆盖事件接收、可靠存储、断线恢复、并发冲突处理和完整验证场景。",
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


class WebConsoleTests(unittest.TestCase):
    def make_database(self, root: Path) -> Path:
        database = root / "production.sqlite3"
        spec = root / "spec.json"
        spec.write_text(json.dumps(question_spec("0911"), ensure_ascii=False), encoding="utf-8")
        connection = connect(database)
        create_batch(connection, root, spec)
        question = connection.execute("SELECT * FROM questions").fetchone()
        set_repository(
            connection,
            "0911",
            1,
            "https://github.com/example/project",
            f"https://github.com/example/project/commit/{question['local_initial_sha']}",
        )
        connection.execute(
            "UPDATE questions SET mechanical_qc='pass', qc_decision='pass', "
            "qc_prompt_sha256=?, status='approved' WHERE id=?",
            (prompt_hash(question["prompt"]), question["id"]),
        )
        connection.commit()
        connection.close()
        return database

    def test_dashboard_derives_waiting_model_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            dashboard = data.dashboard("0911")
            self.assertEqual(dashboard["summary"]["total"], 1)
            self.assertEqual(dashboard["questions"][0]["stage_label"], "待模型跑题")
            self.assertTrue(dashboard["questions"][0]["can_launch"])
            self.assertEqual(dashboard["stages"][0]["complete"], 1)
            self.assertEqual(dashboard["stages"][0]["current"], 0)
            self.assertEqual(dashboard["stages"][1]["current"], 1)

    def test_changed_prompt_returns_to_question_qc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            connection = sqlite3.connect(database)
            connection.execute("UPDATE questions SET prompt=prompt || ' changed'")
            connection.commit()
            connection.close()
            question = ConsoleData(database, root).dashboard("0911")["questions"][0]
            self.assertFalse(question["question_qc"])
            self.assertEqual(question["stage_label"], "题目待质检")
            self.assertFalse(question["can_launch"])

    def test_selection_rejects_unknown_question(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            with self.assertRaisesRegex(ValueError, "不存在"):
                data.validate_selection("0911", [2])

    def test_exported_question_numbers_support_ranges_and_suffixes(self):
        workbooks = [
            {"name": "CC_Codex 用户满意度标注（0911-第1_3-5题）_2.xlsx"},
            {"name": "其他文件.xlsx"},
        ]
        self.assertEqual(
            ConsoleData._exported_question_numbers("0911", workbooks),
            {1, 3, 4, 5},
        )

    def test_env_config_masks_key_and_updates_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text(
                "# keep this comment\n"
                "CC_SWITCH_BASE_URL=https://old.example.com/v1\n"
                "CC_SWITCH_MODEL=old-model\n"
                "CC_SWITCH_API_KEY=old-secret\n"
                "CC_USR_SUBMITTER=旧提交人\n",
                encoding="utf-8",
            )
            data = ConsoleData(self.make_database(root), root)
            self.assertEqual(data.env_config()["api_key_hint"], "已配置（末尾 cret）")
            result = data.update_env({
                "base_url": "https://relay.example.com/v1",
                "model": "claude-new",
                "api_key": "new-secret",
                "submitter": "新提交人",
            })
            self.assertEqual(result["config"]["model"], "claude-new")
            content = env_file.read_text(encoding="utf-8")
            self.assertIn('CC_SWITCH_BASE_URL="https://relay.example.com/v1"', content)
            self.assertIn('CC_SWITCH_API_KEY="new-secret"', content)
            self.assertNotIn("old-secret", content)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_env_config_keeps_existing_key_when_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "CC_SWITCH_BASE_URL=https://relay.example.com/v1\n"
                "CC_SWITCH_MODEL=claude-test\n"
                "CC_SWITCH_API_KEY=keep-secret\n"
                "CC_USR_SUBMITTER=提交人\n",
                encoding="utf-8",
            )
            data = ConsoleData(self.make_database(root), root)
            data.update_env({"base_url": "https://relay.example.com/v2", "model": "claude-v2", "api_key": "", "submitter": "提交人"})
            self.assertIn('CC_SWITCH_API_KEY="keep-secret"', (root / ".env").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
