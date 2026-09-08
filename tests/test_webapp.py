import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.batch_pipeline import connect, create_batch, prompt_hash, set_repository
from webapp.server import ConsoleData, open_local_path, runtime_info, secure_file


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
            if os.name != "nt":
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

    def test_empty_dashboard_includes_zero_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connection = connect(database)
            connection.close()
            dashboard = ConsoleData(database, root).dashboard()
            self.assertEqual(
                dashboard["summary"],
                {"total": 0, "delivered": 0, "qc_passed": 0, "waiting": 0},
            )

    def test_runtime_info_reports_supported_terminal(self):
        info = runtime_info()
        self.assertIn(info["platform"], {"macOS", "Windows", "Linux"})
        self.assertTrue(info["terminal"])

    def test_codex_author_job_persists_output_and_command(self):
        class Process:
            pid = 4321
            stdout = iter([
                '{"type":"thread.started"}\n',
                '{"type":"item.completed","item":{"type":"agent_message","text":"finished"}}\n',
            ])

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connection = connect(database)
            connection.close()
            data = ConsoleData(database, root)
            try:
                with mock.patch("webapp.server.shutil.which", return_value="codex"), mock.patch(
                    "webapp.server.subprocess.Popen", return_value=Process()
                ) as popen:
                    result = data.create_author_job({
                        "batch": "codex1", "count": 2, "business": "城市服务",
                        "technology": "Python", "notes": "持久化",
                    })
                    self.assertEqual(result["job_id"], 1)
                    data.author_executor.shutdown(wait=True)
                    job = data.author_jobs()[0]
                command = popen.call_args.args[0]
                self.assertEqual(command[1:3], ["exec", "--json"])
                self.assertEqual(job["status"], "completed")
                self.assertIn("finished", job["output"])
            finally:
                data.author_executor.shutdown(wait=True)

    def test_codex_author_job_records_failure(self):
        class Process:
            pid = 4322
            stdout = iter(["plain output\n"])

            def wait(self):
                return 7

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connection = connect(database)
            connection.close()
            data = ConsoleData(database, root)
            try:
                with mock.patch("webapp.server.shutil.which", return_value="codex"), mock.patch(
                    "webapp.server.subprocess.Popen", return_value=Process()
                ):
                    data.create_author_job({"batch": "codex2", "count": 1, "business": "本地服务"})
                    data.author_executor.shutdown(wait=True)
                job = data.author_jobs()[0]
                self.assertEqual(job["status"], "failed")
                self.assertIn("退出码：7", job["error"])
            finally:
                data.author_executor.shutdown(wait=True)

    def test_auto_pipeline_job_persists_items_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            (root / ".env").write_text(
                "CC_SWITCH_BASE_URL=https://relay.example.com\n"
                "CC_SWITCH_MODEL=claude-test\n"
                "CC_SWITCH_API_KEY=test-secret\n"
                "CC_USR_SUBMITTER=测试提交人\n",
                encoding="utf-8",
            )
            data = ConsoleData(database, root)
            try:
                with mock.patch.object(data, "_run_pipeline_process") as runner:
                    result = data.create_pipeline_job({"batch": "0911"})
                    self.assertEqual(result["job_id"], 1)
                    runner.assert_called_once()
                jobs = data.pipeline_jobs()
                self.assertEqual(jobs[0]["status"], "queued")
                self.assertEqual(jobs[0]["model_concurrency"], 2)
                self.assertEqual(len(jobs[0]["items"]), 1)
                self.assertIn("heartbeat_at", jobs[0]["items"][0])
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

    def test_failed_pipeline_retry_creates_linked_job_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            (root / ".env").write_text(
                "CC_SWITCH_BASE_URL=https://relay.example.com\n"
                "CC_SWITCH_MODEL=claude-test\n"
                "CC_SWITCH_API_KEY=test-secret\n"
                "CC_USR_SUBMITTER=测试提交人\n",
                encoding="utf-8",
            )
            data = ConsoleData(database, root)
            try:
                with mock.patch.object(data, "_run_pipeline_process"):
                    first = data.create_pipeline_job({"batch": "0911"})
                    data.pipeline_executor.shutdown(wait=True)
                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE pipeline_jobs SET status='failed', error='模型执行失败' WHERE id=?",
                    (first["job_id"],),
                )
                connection.execute(
                    "UPDATE pipeline_items SET status='failed', error='模型执行失败' WHERE pipeline_job_id=?",
                    (first["job_id"],),
                )
                connection.commit()
                connection.close()
                data.pipeline_executor = mock.Mock()

                retried = data.retry_pipeline_job({"job_id": first["job_id"]})
                jobs = data.pipeline_jobs()

                self.assertEqual(retried["retry_of_job_id"], first["job_id"])
                self.assertEqual(jobs[0]["status"], "queued")
                self.assertEqual(jobs[0]["retry_of_job_id"], first["job_id"])
                self.assertEqual(len(jobs), 1)
                connection = sqlite3.connect(database)
                source = connection.execute(
                    "SELECT status,error FROM pipeline_jobs WHERE id=?", (first["job_id"],)
                ).fetchone()
                connection.close()
                self.assertEqual(source, ("failed", "模型执行失败"))
                data.pipeline_executor.submit.assert_called_once()
            finally:
                data.author_executor.shutdown(wait=True)
                shutdown = getattr(data.pipeline_executor, "shutdown", None)
                if shutdown:
                    shutdown(wait=True)

    def test_pipeline_jobs_returns_only_latest_attempt_and_log_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            connection = sqlite3.connect(database)
            created = "2026-09-08T10:00:00+08:00"
            connection.execute(
                "INSERT INTO pipeline_jobs(id,batch_name,question_count,docker_image,status,output,created_at) "
                "VALUES(1,'0911',1,'claude-cli:latest','failed',?,?)",
                ("a" * 40000, created),
            )
            connection.execute(
                "INSERT INTO pipeline_jobs(id,batch_name,question_count,docker_image,status,output,created_at) "
                "VALUES(2,'0911',1,'claude-cli:latest','running',?,?)",
                ("b" * 40000, created),
            )
            connection.commit()
            connection.close()
            data = ConsoleData(database, root)
            try:
                jobs = data.pipeline_jobs()
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

        self.assertEqual([job["id"] for job in jobs], [2])
        self.assertEqual(jobs[0]["output_length"], 40000)
        self.assertEqual(len(jobs[0]["output"]), 30000)

    def test_windows_open_uses_startfile(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with mock.patch("webapp.server.sys.platform", "win32"), mock.patch(
                "webapp.server.os.startfile", create=True
            ) as startfile:
                open_local_path(target, target)
            startfile.assert_called_once_with(str(target))

    def test_macos_open_still_uses_open_command(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with mock.patch("webapp.server.sys.platform", "darwin"), mock.patch(
                "webapp.server.subprocess.Popen"
            ) as popen:
                open_local_path(target, target)
            popen.assert_called_once_with(["open", str(target)], cwd=target)

    def test_windows_secure_file_restricts_acl_to_current_user(self):
        identity = mock.Mock(returncode=0, stdout="machine\\operator\n")
        acl = mock.Mock(returncode=0, stdout="processed")
        with mock.patch("webapp.server.os.name", "nt"), mock.patch(
            "webapp.server.subprocess.run", side_effect=[identity, acl]
        ) as run_mock:
            secure_file(Path("C:/workspace/.env.tmp"))
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(
            run_mock.call_args_list[1].args[0],
            [
                "icacls", "C:\\workspace\\.env.tmp", "/inheritance:r",
                "/grant:r", "machine\\operator:F",
            ],
        )


if __name__ == "__main__":
    unittest.main()
