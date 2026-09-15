import io
import json
import subprocess
import sys
import tempfile
import unittest
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.batch_pipeline import (  # noqa: E402
    check_duplicates,
    connect,
    create_batch,
    list_questions,
    prompt_hash,
    run_duplicate_qc,
    run_mechanical_qc,
    set_repository,
    set_semantic_qc,
    prompt_style_issues,
    repeated_terminal_sentence,
    snapshot_content_issues,
    sqlite_only_technology_issues,
    technology_diversity_issues,
)


def make_question(index: int, *, difficulty: str = "困难") -> dict:
    domains = {
        1: ("结算重试状态机", "财务团队需要统一处理支付回调、结算调度和账务记录，服务在故障恢复后仍要识别已经入账的通知，并保证重复投递与并发处理不会造成余额或结算状态分叉。", "payment-race"),
        2: ("文档协作离线合并", "编辑团队经常在断网期间修改同一份材料，重新联网后需要按照权限和版本顺序合并离线变更，保留无法自动解决的冲突，并让参与者能够追溯每次取舍。", "offline-merge"),
        3: ("媒体处理背压", "视频运营希望上传任务在流量高峰时仍能稳定排队，转码服务需要根据下游容量施加背压、持续通知进度，并在进程重启后恢复失败或未完成的处理记录。", "media-backpressure"),
    }
    title, prompt, tag = domains[index]
    return {
        "folder": f"q{index:03d}",
        "task_id": f"0911-{index:03d}",
        "title": title,
        "prompt": prompt,
        "task_type": "0-1 代码生成",
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
    def test_schema_15_migrates_old_record_identity_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connection = connect(database)
            create_batch(connection, root, self.write_spec(root, count=1))
            question_id = connection.execute("SELECT id FROM questions").fetchone()[0]
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone()[0]
            old_sql = table_sql.replace(
                "UNIQUE (question_id, turn_no)",
                "UNIQUE (session_id, turn_id), UNIQUE (question_id, turn_no)",
            )
            columns = [
                row["name"] for row in connection.execute("PRAGMA table_info(records)")
                if row["name"] != "id"
            ]
            values: list[object] = []
            for name in columns:
                if name == "question_id":
                    values.append(question_id)
                elif name == "turn_no":
                    values.append(1)
                elif name.endswith("_score"):
                    values.append(3)
                elif name in {
                    "human_authored", "human_qc_approved", "delivery_qc_passed",
                    "evidence_gate_passed", "history_gate_passed",
                    "is_continuation", "continuation_count", "reset_count",
                }:
                    values.append(0)
                elif name == "record_id":
                    values.append("legacy-record")
                elif name == "session_id":
                    values.append("legacy-session")
                elif name in {"turn_id", "raw_turn_id"}:
                    values.append("legacy-turn")
                elif name == "created_at":
                    values.append("2026-09-01T00:00:00+08:00")
                else:
                    values.append("legacy")
            connection.execute(
                f"INSERT INTO records({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                values,
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP INDEX IF EXISTS idx_records_question")
            connection.execute("ALTER TABLE records RENAME TO records_current")
            connection.execute(old_sql)
            connection.execute(
                f"INSERT INTO records({','.join(columns)}) SELECT {','.join(columns)} FROM records_current"
            )
            connection.execute("DROP TABLE records_current")
            connection.commit()
            connection.close()

            migrated = connect(database)
            migrated_sql = migrated.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone()[0]
            self.assertNotIn("UNIQUE (session_id, turn_id)", migrated_sql)
            self.assertEqual(
                migrated.execute("SELECT record_id FROM records").fetchone()[0],
                "legacy-record",
            )
            copied = dict(migrated.execute("SELECT * FROM records").fetchone())
            copied.pop("id")
            copied.update({
                "record_id": "continued-record", "turn_no": 2,
                "parent_record": "legacy-record", "is_continuation": 1,
                "continuation_count": 1, "raw_user_prompt": "继续",
                "raw_turn_id": "raw-continue",
            })
            migrated.execute(
                f"INSERT INTO records({','.join(copied)}) VALUES({','.join('?' for _ in copied)})",
                list(copied.values()),
            )
            migrated.commit()
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
            migrated.close()

    def test_schema_15_migrates_existing_solo2_receipts_as_succeeded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            with connect(database) as connection:
                create_batch(connection, root, self.write_spec(root, count=1))
                question_id = connection.execute("SELECT id FROM questions").fetchone()[0]
                connection.execute("DROP TABLE solo2_submissions")
                connection.execute(
                    "CREATE TABLE solo2_submissions("
                    "id INTEGER PRIMARY KEY,record_id TEXT NOT NULL UNIQUE,"
                    "question_id INTEGER NOT NULL,remote_submission_id TEXT NOT NULL,"
                    "remote_status TEXT NOT NULL DEFAULT '',schema_fingerprint TEXT NOT NULL,"
                    "payload_sha256 TEXT NOT NULL,response_summary TEXT NOT NULL DEFAULT '{}',"
                    "submitted_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO solo2_submissions(record_id,question_id,remote_submission_id,"
                    "remote_status,schema_fingerprint,payload_sha256,response_summary,submitted_at) "
                    "VALUES('record-1',?,'remote-1','SUBMITTED','schema','hash','{}',"
                    "'2026-09-01T00:00:00+08:00')",
                    (question_id,),
                )
                connection.commit()

            with connect(database) as migrated:
                row = migrated.execute("SELECT * FROM solo2_submissions").fetchone()
                self.assertEqual(row["status"], "succeeded")
                self.assertEqual(row["attempt_count"], 1)
                self.assertEqual(row["remote_submission_id"], "remote-1")
                self.assertEqual(row["updated_at"], row["submitted_at"])

    def test_technology_diversity_rejects_go_sqlite_monoculture(self):
        issues = technology_diversity_issues([["Go", "SQLite"] for _ in range(10)])
        self.assertTrue(any("完整技术组合" in issue for issue in issues))
        self.assertTrue(any("主要编程语言" in issue for issue in issues))
        self.assertTrue(any("sqlite" in issue for issue in issues))

    def test_technology_diversity_accepts_supported_backend_batch(self):
        stacks = [
            ["Go", "Gin", "PostgreSQL"],
            ["Go", "Fiber", "Redis"],
            ["Python", "FastAPI", "PostgreSQL"],
            ["Python", "Django", "SQLite"],
            ["TypeScript", "NestJS", "MongoDB"],
            ["JavaScript", "Fastify", "Redis"],
            ["Java", "Spring Boot", "MySQL"],
            ["Java", "Quarkus", "PostgreSQL"],
        ]
        self.assertEqual(technology_diversity_issues(stacks), [])

    def test_sqlite_only_technology_gate_rejects_external_stores(self):
        self.assertEqual(sqlite_only_technology_issues(["Python", "FastAPI", "SQLite"]), [])
        issues = sqlite_only_technology_issues(["Python", "FastAPI", "PostgreSQL"])
        self.assertTrue(any("PostgreSQL".casefold() in issue.casefold() for issue in issues))
        self.assertTrue(sqlite_only_technology_issues(["Go", "Chi"]))

    def test_fixed_technology_policy_requires_explicit_reason(self):
        stacks = [["Go", "SQLite"] for _ in range(10)]
        self.assertTrue(technology_diversity_issues(stacks, "fixed", ""))
        self.assertEqual(
            technology_diversity_issues(
                stacks, "fixed", "用户明确要求全部使用 Go 和 SQLite"
            ),
            [],
        )

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
            with self.assertRaisesRegex(ValueError, "must be 困难/地狱"):
                create_batch(connection, root, self.write_spec(root, count=1, difficulty="简单"))
            self.assertFalse((root / "0911").exists())
            connection.close()

    def test_non_zero_to_one_first_turn_is_rejected_without_creating_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = json.loads(self.write_spec(root, count=1).read_text(encoding="utf-8"))
            spec["questions"][0]["task_type"] = "Feature 迭代"
            path = root / "spec.json"
            path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            connection = connect(root / "production.sqlite3")
            with self.assertRaisesRegex(ValueError, "first-turn task_type must be 0-1 代码生成"):
                create_batch(connection, root, path)
            self.assertFalse((root / "0911").exists())
            connection.close()

    def test_multi_paragraph_first_turn_is_rejected_without_creating_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = json.loads(self.write_spec(root, count=1).read_text(encoding="utf-8"))
            spec["questions"][0]["prompt"] += "\n第二段验收要求。"
            path = root / "spec.json"
            path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            connection = connect(root / "production.sqlite3")
            with self.assertRaisesRegex(ValueError, "first-turn prompt must be one paragraph"):
                create_batch(connection, root, path)
            self.assertFalse((root / "0911").exists())
            connection.close()

    def test_qc_pass_makes_question_ready_without_human_approval(self):
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
            set_semantic_qc(connection, "0911", "1", "pass", "ignored pass detail")
            row = connection.execute("SELECT * FROM questions").fetchone()
            self.assertEqual(row["status"], "approved")
            self.assertEqual(row["mechanical_qc"], "pass")
            self.assertEqual(row["qc_decision"], "pass")
            self.assertEqual(row["qc_report"], "质检通过")
            self.assertEqual(row["qc_prompt_sha256"], prompt_hash(row["prompt"]))
            self.assertEqual(row["human_approved"], 0)
            output = io.StringIO()
            with redirect_stdout(output):
                list_questions(connection, "0911")
            self.assertIn("READY", output.getvalue())
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

    def test_mechanical_qc_does_not_judge_legacy_task_type(self):
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
            connection.execute(
                "UPDATE questions SET task_type='Feature 迭代' WHERE id=?", (row["id"],)
            )
            connection.commit()
            self.assertEqual(run_mechanical_qc(connection, "0911", "1"), 0)
            self.assertEqual(
                connection.execute("SELECT mechanical_qc FROM questions").fetchone()[0],
                "pass",
            )
            connection.close()

    def test_duplicate_qc_checks_all_stored_questions_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = json.loads(self.write_spec(root, count=2).read_text(encoding="utf-8"))
            spec["questions"][1]["prompt"] = spec["questions"][0]["prompt"]
            path = root / "spec.json"
            path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            connection = connect(root / "production.sqlite3")
            create_batch(connection, root, path)
            before = connection.execute(
                "SELECT mechanical_qc, qc_decision FROM questions ORDER BY question_no"
            ).fetchall()
            self.assertEqual(run_duplicate_qc(connection, "0911", "1"), 1)
            report = check_duplicates(
                connection,
                connection.execute("SELECT * FROM questions WHERE question_no=1").fetchone(),
            )
            self.assertEqual(report["duplicates"][0]["task_id"], "0911-002")
            self.assertIn("overall_similarity", report["duplicates"][0]["duplicate_reasons"])
            after = connection.execute(
                "SELECT mechanical_qc, qc_decision FROM questions ORDER BY question_no"
            ).fetchall()
            self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
            connection.close()

    def test_mechanical_qc_blocks_legacy_multi_paragraph_prompt(self):
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
            connection.execute(
                "UPDATE questions SET prompt=prompt || char(10) || '第二段' WHERE id=?",
                (row["id"],),
            )
            connection.commit()
            self.assertEqual(run_mechanical_qc(connection, "0911", "1"), 1)
            report = json.loads(
                connection.execute("SELECT qc_report FROM questions").fetchone()[0]
            )
            self.assertIn("首轮 User Prompt 必须是一个自然语言段落", report["errors"])
            connection.close()

    def test_prompt_style_rejects_canned_opening_and_label_chain(self):
        issues = prompt_style_issues(
            "从零构建一套结算平台。背景：财务需要处理重复回调；功能：保存账务记录；验收：并发重试后余额一致。"
        )
        self.assertIn("User Prompt 使用了“从零构建一套”式固定开头", issues)
        self.assertIn("User Prompt 不能把背景、功能、技术、验收等标签串成模板", issues)

    def test_prompt_style_rejects_todo_literal_even_when_negated(self):
        issues = prompt_style_issues(
            "县域培训管理员需要核对课程学分，请补全可启动的后端代码和自动化测试，"
            "不能用接口草案、TODO 或后续计划代替实际实现。"
        )
        self.assertIn(
            "User Prompt 不得出现易被常见应用题库误判的 TODO 字面词，请用中文描述完整交付要求",
            issues,
        )

    def test_prompt_style_accepts_natural_chinese_completion_boundary(self):
        issues = prompt_style_issues(
            "县域培训管理员需要核对教师课程学分，请补全仓库中的后端代码并确保服务可以启动，"
            "所有要求都须落实为可执行代码，不能只说明思路或把工作留到以后；"
            "执行自动化测试确认重复签到不会覆盖原记录。"
        )
        self.assertEqual(issues, [])

    def test_prompt_style_rejects_scenario_checklist_and_no_docker_tail(self):
        issues = prompt_style_issues(
            "检索接口需要反查全部权利依据，也能从一份授权定位受影响渠道；"
            "SQLite 要保存文件元数据、作业队列和不可改写的决策历史，请用重复上传、"
            "撤回级联、并发放行、越权下载与重启续作场景验证，项目不设置 Docker 环境。"
        )
        self.assertIn("User Prompt 不能使用“列举多个场景 + 统一验证”的模板化验收尾句", issues)
        self.assertIn("User Prompt 不能追加“项目不设置 Docker 环境”式通用尾句", issues)

    def test_prompt_style_rejects_evaluator_english_but_allows_technical_tokens(self):
        issues = prompt_style_issues("Overall，这个模型的表现基本符合预期，请完成后台服务。")
        self.assertIn("User Prompt 不能使用可由中文直接表达的英文评价或衔接词", issues)
        technical = prompt_style_issues(
            "为后端增加 /summary 路由，并让 SummaryService 返回 JSON 字段 `summary`，完成后执行 pytest。"
        )
        self.assertNotIn("User Prompt 不能使用可由中文直接表达的英文评价或衔接词", technical)

    def test_repeated_terminal_sentence_rejects_shared_tail(self):
        common_tail = "沿用仓库现有的启动方式，不增加额外的容器编排配置。"
        self.assertTrue(
            repeated_terminal_sentence(
                "调度员需要恢复中断的排班记录。" + common_tail,
                "管理员需要迁移仍在生效的目录。" + common_tail,
            )
        )

    def test_snapshot_documents_must_be_chinese_and_project_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect(root / "production.sqlite3")
            create_batch(connection, root, self.write_spec(root, count=1))
            folder = root / "0911" / "q001"
            readme = folder / "README.md"
            readme.write_text(
                "# Starting workspace\n\nThis implementation is intentionally left to the task owner.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(folder), "add", "README.md"], check=True)
            issues = snapshot_content_issues(folder)
            self.assertTrue(any("中文书面语" in issue for issue in issues))
            self.assertTrue(any("脚手架或答题说明" in issue for issue in issues))

            readme.write_text(
                "# 结算状态服务\n\n本项目保存支付回调和账务流水，并为财务人员提供可追溯的结算结果。本文不包含评测或标注任务。\n",
                encoding="utf-8",
            )
            issues = snapshot_content_issues(folder)
            self.assertTrue(any("评测或标注语境" in issue for issue in issues))

            readme.write_text(
                "# 结算状态服务\n\n本项目保存支付回调和账务流水，并为财务人员提供可追溯的结算结果。重复通知沿用原有流水号，服务重启后可以继续核对未完成结算。\n",
                encoding="utf-8",
            )
            self.assertEqual(snapshot_content_issues(folder), [])
            source = folder / "src" / "说明.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text('"""Starting workspace implementation."""\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(folder), "add", str(source.relative_to(folder))], check=True)
            self.assertTrue(any("Python 文档字符串" in issue for issue in snapshot_content_issues(folder)))
            connection.close()


if __name__ == "__main__":
    unittest.main()
