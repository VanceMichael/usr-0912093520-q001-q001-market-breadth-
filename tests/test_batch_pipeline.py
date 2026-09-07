import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.batch_pipeline import (  # noqa: E402
    approve_questions,
    connect,
    create_batch,
    list_questions,
    prompt_hash,
    run_mechanical_qc,
    set_repository,
    set_semantic_qc,
)


def make_question(index: int, *, difficulty: str = "困难") -> dict:
    domains = {
        1: ("结算重试状态机", "梳理支付回调、任务队列和账务落库之间的状态流，修复重复回调时的竞态，并补齐故障恢复测试。", "payment-race"),
        2: ("文档协作离线合并", "为现有编辑器、同步服务和冲突存储增加离线变更合并能力，覆盖重连、乱序事件和权限变化。", "offline-merge"),
        3: ("媒体处理背压", "重构上传入口、转码队列与进度推送的背压策略，保持外部协议兼容并加入压力场景验证。", "media-backpressure"),
    }
    title, prompt, tag = domains[index]
    return {
        "folder": f"q{index:03d}",
        "task_id": f"0911-{index:03d}",
        "title": title,
        "prompt": prompt,
        "task_type": "Feature 迭代",
        "difficulty": difficulty,
        "languages": ["Go", "TypeScript"],
        "repo_url": f"https://github.com/example/project-{index}",
        "initial_snapshot": "",
        "reproducibility": "无外部依赖",
        "expected_areas": ["service", "web", "tests"],
        "difficulty_evidence": ["需要跨模块状态设计与异常链路验证"],
        "similarity_tags": [tag],
    }


class BatchPipelineTests(unittest.TestCase):
    def write_spec(self, root: Path, count: int = 3, *, difficulty: str = "困难") -> Path:
        spec = {
            "batch": "0911",
            "brief": "三道不同领域的跨模块任务",
            "questions": [
                make_question(index, difficulty=difficulty) for index in range(1, count + 1)
            ],
        }
        path = root / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        return path

    def test_create_batch_makes_exact_folders_markdown_and_git_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "production.sqlite3")
            create_batch(connection, root, self.write_spec(root))
            batch_dir = root / "0911"
            folders = sorted(path for path in batch_dir.iterdir() if path.is_dir())
            self.assertEqual([path.name for path in folders], ["q001", "q002", "q003"])
            self.assertTrue((batch_dir / "题目_0911.md").is_file())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 3)
            for folder in folders:
                sha = subprocess.run(
                    ["git", "-C", str(folder), "rev-parse", "HEAD"],
                    text=True, stdout=subprocess.PIPE, check=True,
                ).stdout.strip()
                self.assertEqual(len(sha), 40)
                status = subprocess.run(
                    ["git", "-C", str(folder), "status", "--porcelain"],
                    text=True, stdout=subprocess.PIPE, check=True,
                ).stdout
                self.assertEqual(status, "")
            connection.close()

    def test_duplicate_batch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "production.sqlite3")
            spec = self.write_spec(root, count=1)
            create_batch(connection, root, spec)
            with self.assertRaises(FileExistsError):
                create_batch(connection, root, spec)
            connection.close()

    def test_duplicate_question_folder_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {
                "batch": "0911",
                "brief": "duplicate folder check",
                "questions": [make_question(1), make_question(2)],
            }
            spec["questions"][1]["folder"] = spec["questions"][0]["folder"]
            path = root / "spec.json"
            path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            connection = connect(root / "production.sqlite3")
            with self.assertRaisesRegex(ValueError, "duplicate folder name"):
                create_batch(connection, root, path)
            self.assertFalse((root / "0911").exists())
            connection.close()

    def test_simple_first_turn_is_rejected_without_creating_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "production.sqlite3")
            with self.assertRaisesRegex(ValueError, "must be 中等/困难/地狱"):
                create_batch(connection, root, self.write_spec(root, count=1, difficulty="简单"))
            self.assertFalse((root / "0911").exists())
            connection.close()

    def test_qc_and_human_approval_make_question_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "production.sqlite3")
            create_batch(connection, root, self.write_spec(root, count=1))
            row = connection.execute("SELECT * FROM questions").fetchone()
            set_repository(
                connection,
                "0911",
                1,
                "https://github.com/example/project-1",
                f"https://github.com/example/project-1/commit/{row['local_initial_sha']}",
            )
            self.assertEqual(run_mechanical_qc(connection, "0911", "1"), 0)
            set_semantic_qc(connection, "0911", "1", "pass", "人工查看仓库证据后建议通过")
            approve_questions(connection, "0911", "1", "reviewer")
            row = connection.execute("SELECT * FROM questions").fetchone()
            self.assertEqual(row["status"], "approved")
            self.assertEqual(row["mechanical_qc"], "pass")
            self.assertEqual(row["qc_decision"], "pass")
            self.assertEqual(row["qc_prompt_sha256"], prompt_hash(row["prompt"]))
            self.assertEqual(row["human_approved"], 1)
            connection.close()

    def test_mechanical_qc_blocks_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = json.loads(self.write_spec(root, count=1).read_text(encoding="utf-8"))
            spec["questions"][0]["initial_snapshot"] = ""
            (root / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            connection = connect(root / "production.sqlite3")
            create_batch(connection, root, root / "spec.json")
            self.assertEqual(run_mechanical_qc(connection, "0911", "1"), 1)
            self.assertEqual(
                connection.execute("SELECT mechanical_qc FROM questions").fetchone()[0],
                "reject",
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
