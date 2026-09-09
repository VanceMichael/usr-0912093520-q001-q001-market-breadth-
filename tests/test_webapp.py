import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from tools.batch_pipeline import connect, create_batch, prompt_hash, set_repository
from webapp.server import ConsoleData, open_local_path, runtime_info, safe_console_print, secure_file


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
    def test_event_message_accepts_scalar_json_output(self):
        self.assertEqual(ConsoleData._event_message('"plain output"'), '"plain output"')

    def test_safe_console_print_does_not_raise_on_unencodable_output(self):
        encoding_error = UnicodeEncodeError("gbk", "�", 0, 1, "illegal multibyte sequence")
        with mock.patch("builtins.print", side_effect=[encoding_error, None]) as printer:
            safe_console_print("�")
        self.assertIn("\\ufffd", printer.call_args_list[1].args[0])

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

    def test_export_passes_batch_runs_as_trajectory_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            data = ConsoleData(database, root)
            try:
                with mock.patch.object(
                    data, "run_tool", return_value={"ok": True, "output": ""}
                ) as run_tool:
                    data.export("0911", None)
                command = run_tool.call_args.args[0]
                self.assertIn("--claude-root", command)
                root_index = command.index("--claude-root") + 1
                self.assertEqual(Path(command[root_index]), root / "0911" / ".runs")
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

    def test_delivery_package_contains_only_registered_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            batch_folder = root / "0911"
            (batch_folder / "CC_Codex 用户满意度标注（0911-第1题）.xlsx").write_bytes(b"xlsx")
            (batch_folder / "轨迹_0911-001.jsonl").write_text("{}\n", encoding="utf-8")
            (batch_folder / "production.sqlite3").write_bytes(b"private")
            nested = batch_folder / "nested"
            nested.mkdir()
            (nested / "轨迹_unsafe.jsonl").write_text("unsafe\n", encoding="utf-8")
            data = ConsoleData(database, root)
            try:
                filename, payload = data.delivery_package("0911")
                self.assertEqual(filename, "ccusr-delivery-0911.zip")
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.assertEqual(
                        set(archive.namelist()),
                        {"CC_Codex 用户满意度标注（0911-第1题）.xlsx", "轨迹_0911-001.jsonl"},
                    )
                batch = data.dashboard("0911")["batch"]
                self.assertEqual(batch["download_count"], 1)
                self.assertTrue(batch["last_downloaded_at"])
                data.delivery_package("0911")
                self.assertEqual(data.dashboard("0911")["batch"]["download_count"], 2)
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

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
            self.assertEqual(result["config"]["model_mode"], "local")
            content = env_file.read_text(encoding="utf-8")
            self.assertIn('CC_SWITCH_BASE_URL="https://relay.example.com/v1"', content)
            self.assertIn('CC_SWITCH_API_KEY="new-secret"', content)
            self.assertIn('CC_PIPELINE_MODEL_MODE="local"', content)
            self.assertIn('CC_AUTHOR_BATCH_SIZE="10"', content)
            self.assertNotIn("old-secret", content)
            self.assertEqual(len(result["config"]["news_feeds"]), 3)
            if os.name != "nt":
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_news_feed_configuration_is_persisted_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            data = ConsoleData(database, root)
            feeds = [
                {"url": "https://news.example.com/a", "enabled": True},
                {"url": "https://news.example.com/b", "enabled": False},
            ]
            result = data.update_news_feeds(feeds)
            self.assertEqual(result, feeds)
            self.assertEqual(data.env_config()["news_feeds"], [
                {**feeds[0], "id": 1, "created_at": mock.ANY, "updated_at": mock.ANY},
                {**feeds[1], "id": 2, "created_at": mock.ANY, "updated_at": mock.ANY},
            ])
            with self.assertRaisesRegex(ValueError, "至少启用"):
                data.update_news_feeds([{"url": "https://news.example.com/a", "enabled": False}])

    def test_vps_node_registry_and_remote_dashboard_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            data = ConsoleData(database, root)
            with mock.patch.object(
                data, "_vps_request", return_value=(json.dumps({"summary": {"total": 1}}).encode(), {})
            ):
                nodes = data.vps_nodes()
            self.assertEqual(nodes[0]["status"], "online")
            self.assertEqual(nodes[0]["dashboard"]["summary"]["total"], 1)
            saved = data.save_vps_node({
                "name": "VPS-02", "base_url": "http://127.0.0.1:18788",
                "ssh_command": "ssh -N -L 18788:127.0.0.1:8787 ubuntu@example.com", "enabled": True,
            })
            self.assertEqual(saved["name"], "VPS-02")

    def test_scheduler_snapshot_and_control_include_operational_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            try:
                data.scheduler_store.startup()
                with mock.patch.object(data, "_scheduler_containers", return_value=[]), mock.patch.object(
                    data, "_scheduler_git", return_value={"branch": "test", "commit": "abc1234", "dirty": False}
                ):
                    snapshot = data.scheduler_snapshot()
                self.assertTrue(snapshot["state"]["process_online"])
                self.assertEqual(snapshot["git"]["branch"], "test")
                self.assertEqual(snapshot["queue"]["questions_ready"], 1)
                self.assertEqual(snapshot["config"]["batch_size"], 10)

                result = data.scheduler_control("pause")
                self.assertEqual(result["desired_state"], "paused")
                events = data.scheduler_events({"limit": ["10"]})["events"]
                self.assertEqual(events[-1]["event_type"], "control_requested")
            finally:
                data.shutdown()

    def test_start_control_launches_scheduler_when_heartbeat_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            try:
                with mock.patch.object(data, "_start_scheduler_process", return_value=True) as start:
                    result = data.scheduler_control("start")
                start.assert_called_once_with()
                self.assertTrue(result["started_process"])
            finally:
                data.shutdown()

    def test_running_scheduler_blocks_manual_work_on_the_same_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            try:
                data.scheduler_store.startup()
                with self.assertRaisesRegex(RuntimeError, "自动调度器正在运行"):
                    data._ensure_manual_work_allowed()
                data.scheduler_store.request_control("stop")
                data.scheduler_store.update(actual_state="stopped")
                data._ensure_manual_work_allowed()
            finally:
                data.shutdown()

    def test_remote_scheduler_control_is_proxied_to_selected_vps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            try:
                response = {"ok": True, "desired_state": "paused"}
                with mock.patch.object(
                    data, "_vps_request", return_value=(json.dumps(response).encode(), {})
                ) as request:
                    result = data.vps_scheduler_control(1, "pause")
                self.assertEqual(result, response)
                self.assertEqual(request.call_args.kwargs["method"], "POST")
                self.assertEqual(request.call_args.kwargs["payload"], {"action": "pause"})
            finally:
                data.shutdown()

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

    def test_runtime_config_stores_github_token_and_author_difficulty_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            token = "ghp_example_token_1234567890"
            result = data.update_env({
                "base_url": "https://relay.example.com/v1",
                "model": "claude-test",
                "api_key": "api-secret",
                "submitter": "提交人",
                "github_token": token,
                "author_difficulty_weights": {"中等": 50, "困难": 30, "地狱": 20},
                "author_batch_size": 10,
            })
            self.assertEqual(
                result["config"]["author_difficulty_weights"],
                {"中等": 50, "困难": 30, "地狱": 20},
            )
            self.assertEqual(result["config"]["author_batch_size"], 10)
            self.assertEqual(result["config"]["github_token_hint"], "已配置（末尾 7890）")
            self.assertNotIn(token, json.dumps(result["config"], ensure_ascii=False))
            content = (root / ".env").read_text(encoding="utf-8")
            self.assertIn(f'CC_GITHUB_TOKEN="{token}"', content)
            self.assertIn('CC_AUTHOR_DIFFICULTY_WEIGHTS="{\\"中等\\":50,\\"困难\\":30,\\"地狱\\":20}"', content)
            prompt = ConsoleData.author_prompt(
                "batch", 10, "新闻", "", "", difficulty={"中等": 50, "困难": 30, "地狱": 20}
            )
            self.assertIn("难度分配：中等 5 道（50%）、困难 3 道（30%）、地狱 2 道（20%）", prompt)
            with self.assertRaisesRegex(ValueError, "合计必须等于 100"):
                data.update_env({
                    "base_url": "https://relay.example.com/v1", "model": "claude-test",
                    "api_key": "", "submitter": "提交人",
                    "author_difficulty_weights": {"中等": 50, "困难": 20},
                })

    def test_author_prompts_restrict_zero_to_one_and_derived_tasks_to_backend(self):
        zero_to_one = ConsoleData.author_prompt(
            "backend-new", 10, "城市数据服务", "Python", "包含异步任务",
            difficulty={"中等": 100},
        )
        derived = ConsoleData.author_prompt(
            "backend-derived", 2, "", "", "", mode="derived", task_type="Feature 迭代",
            mother={"id": 7, "title": "订单服务", "workspace_path": "/tmp/order", "repo_url": "https://example.com/order"},
            difficulty={"困难": 100},
        )

        for prompt in (zero_to_one, derived):
            self.assertIn("只允许纯后端项目", prompt)
            self.assertIn("Go、Python、Node.js（JavaScript 或 TypeScript）、Java", prompt)
            for excluded in ("Kotlin", "C#/.NET", "Rust", "PHP"):
                self.assertNotIn(excluded, prompt)
            self.assertIn("不得要求或创建任何前端页面", prompt)
            self.assertIn("不得生成全栈题", prompt)
            self.assertIn("不依赖浏览器操作", prompt)

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
            stdin = mock.Mock()
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
                with mock.patch.object(data, "_author_result_error", return_value=""), mock.patch(
                    "webapp.server.shutil.which", return_value="codex"
                ), mock.patch(
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
                self.assertEqual(command[-1], "-")
                prompt = job["prompt"]
                Process.stdin.write.assert_called_once_with(prompt)
                Process.stdin.close.assert_called_once_with()
                self.assertIn("批次名：codex1", prompt)
                self.assertIn("题目数量：2", prompt)
                self.assertEqual(job["status"], "completed")
                self.assertIn("finished", job["output"])
            finally:
                data.author_executor.shutdown(wait=True)

    def test_codex_author_job_records_failure(self):
        class Process:
            pid = 4322
            stdin = mock.Mock()
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

    def test_codex_author_job_rejects_zero_exit_without_batch(self):
        class Process:
            pid = 4323
            stdin = mock.Mock()
            stdout = iter([])

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connect(database).close()
            data = ConsoleData(database, root)
            try:
                with mock.patch("webapp.server.shutil.which", return_value="codex"), mock.patch(
                    "webapp.server.subprocess.Popen", return_value=Process()
                ):
                    data.create_author_job({"batch": "codex3", "count": 1, "business": "本地服务"})
                    data.author_executor.shutdown(wait=True)
                job = data.author_jobs()[0]
                self.assertEqual(job["status"], "failed")
                self.assertIn("未创建批次 codex3", job["error"])
            finally:
                data.author_executor.shutdown(wait=True)

    def test_codex_author_result_accepts_complete_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = ConsoleData(self.make_database(root), root)
            try:
                self.assertEqual(data._author_result_error("0911", 1), "")
            finally:
                data.author_executor.shutdown(wait=True)

    def test_failed_author_retry_creates_linked_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "production.sqlite3"
            connect(database).close()
            data = ConsoleData(database, root)
            try:
                with mock.patch("webapp.server.shutil.which", return_value="codex"), mock.patch.object(
                    data, "_run_author_job"
                ):
                    first = data.create_author_job({
                        "batch": "retry-author", "count": 1, "business": "本地服务",
                    })
                    data.author_executor.shutdown(wait=True)
                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE author_jobs SET status='failed', error='失败' WHERE id=?",
                    (first["job_id"],),
                )
                connection.commit()
                connection.close()
                data.author_executor = mock.Mock()

                with mock.patch("webapp.server.shutil.which", return_value="codex"):
                    retried = data.retry_author_job({"job_id": first["job_id"]})
                jobs = data.author_jobs()
                self.assertEqual(retried["retry_of_job_id"], first["job_id"])
                self.assertEqual(jobs[0]["status"], "queued")
                self.assertFalse(jobs[0]["can_retry"])
                data.author_executor.submit.assert_called_once()
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
                    self.assertEqual(runner.call_args.args[7], "local")
                jobs = data.pipeline_jobs()
                self.assertEqual(jobs[0]["status"], "queued")
                self.assertEqual(jobs[0]["model_mode"], "local")
                self.assertEqual(jobs[0]["model_concurrency"], 2)
                self.assertEqual(len(jobs[0]["items"]), 1)
                self.assertIn("heartbeat_at", jobs[0]["items"][0])
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

    def test_single_question_pipeline_job_contains_only_requested_question(self):
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
                    result = data.create_pipeline_job({"batch": "0911", "numbers": [1]})
                    self.assertEqual(result["job_id"], 1)
                    runner.assert_called_once()
                jobs = data.pipeline_jobs()
                self.assertEqual(jobs[0]["question_count"], 1)
                self.assertEqual([item["question_no"] for item in jobs[0]["items"]], [1])
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
                self.assertEqual(jobs[0]["model_mode"], "local")
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

    def test_docker_pipeline_mode_is_persisted_and_passed_to_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            (root / ".env").write_text(
                "CC_SWITCH_BASE_URL=https://relay.example.com\n"
                "CC_SWITCH_MODEL=claude-test\n"
                "CC_SWITCH_API_KEY=test-secret\n"
                "CC_USR_SUBMITTER=测试提交人\n"
                "CC_PIPELINE_MODEL_MODE=docker\n",
                encoding="utf-8",
            )
            data = ConsoleData(database, root)
            try:
                with mock.patch.object(data, "_run_pipeline_process") as runner:
                    data.create_pipeline_job({"batch": "0911"})
                    runner.assert_called_once()
                    self.assertEqual(runner.call_args.args[7], "docker")
                self.assertEqual(data.pipeline_jobs()[0]["model_mode"], "docker")
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

    def test_local_environment_status_does_not_require_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.make_database(root)
            (root / ".env").write_text(
                "CC_SWITCH_BASE_URL=https://relay.example.com\n"
                "CC_SWITCH_MODEL=claude-test\n"
                "CC_SWITCH_API_KEY=test-secret\n"
                "CC_USR_SUBMITTER=测试提交人\n"
                "CC_PIPELINE_MODEL_MODE=local\n",
                encoding="utf-8",
            )
            data = ConsoleData(database, root)
            try:
                def which(name):
                    return {"codex": "codex", "claude": "claude"}.get(name)

                def run(command, **_kwargs):
                    return mock.Mock(returncode=0, stdout="1.0.0\n")

                with mock.patch("webapp.server.shutil.which", side_effect=which), mock.patch(
                    "webapp.server.subprocess.run", side_effect=run
                ), mock.patch("webapp.server.docker_info", side_effect=AssertionError("Docker should not be checked")):
                    status = data.environment_status()
                names = {check["name"] for check in status["checks"]}
                self.assertTrue(status["ok"])
                self.assertIn("本地 Claude CLI", names)
                self.assertNotIn("Docker CLI", names)
            finally:
                data.author_executor.shutdown(wait=True)
                data.pipeline_executor.shutdown(wait=True)

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
