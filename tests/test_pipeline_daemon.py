import os
import tempfile
import threading
import time
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

from tools.pipeline_daemon import (
    AUTHOR_FAILURE_EXIT,
    active_batches,
    author_prompt,
    authored_batch_result,
    cleanup_logs,
    claim_topics,
    child_process_kwargs,
    cycle,
    create_batch,
    create_next_batch,
    difficulty_distribution,
    difficulty_plan,
    load_runtime_env,
    count_new_topics,
    maintain_ready_buffer,
    manual_jobs_active,
    pipeline_batch_timeout,
    recover_interrupted_authoring,
)
from tools.batch_pipeline import connect
from tools.scheduler_state import SchedulerStore


def test_runtime_env_loads_escaped_difficulty_weights() -> None:
    with tempfile.TemporaryDirectory() as raw:
        env_file = Path(raw) / ".env"
        env_file.write_text(
            'CC_AUTHOR_DIFFICULTY_WEIGHTS="{\\"中等\\":50,\\"困难\\":30,\\"地狱\\":20}"\n',
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            load_runtime_env(env_file)
            plan = difficulty_plan()
        assert plan == {"中等": 50, "困难": 30, "地狱": 20}
        assert difficulty_distribution(10, plan) == "中等 5 道（50%）、困难 3 道（30%）、地狱 2 道（20%）"


def test_child_process_kwargs_use_windows_process_group_without_importing_fcntl() -> None:
    with mock.patch("tools.pipeline_daemon.os.name", "nt"), mock.patch.object(
        __import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 512, create=True
    ), mock.patch.object(__import__("subprocess"), "CREATE_NO_WINDOW", 2048, create=True):
        assert child_process_kwargs() == {"creationflags": 2560}


def test_cleanup_logs_removes_expired_files_only() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        expired = root / "expired.log"
        current = root / "current.log"
        artifact = root / "delivery.jsonl"
        expired.write_bytes(b"old")
        current.write_bytes(b"new")
        artifact.write_bytes(b"keep")
        old = time.time() - 40 * 24 * 3600
        os.utime(expired, (old, old))

        removed, reclaimed = cleanup_logs(root, retention_days=30, max_log_gb=1)

        assert removed == 1
        assert reclaimed == 3
        assert not expired.exists()
        assert current.exists()
        assert artifact.exists()


def test_automatic_author_prompt_is_backend_only() -> None:
    topics = [
        {
            "title": f"城市公共数据服务升级 {index}",
            "summary": "围绕可靠的数据交换与异步处理能力建设",
            "article_url": f"https://news.example.com/article/{index}",
            "source_url": "https://news.example.com/",
            "published_at": "2026-09-09T08:00:00+08:00",
        }
        for index in range(1, 11)
    ]
    with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
        os.environ,
        {"CC_AUTHOR_BATCH_SIZE": "10", "CC_AUTHOR_DIFFICULTY_WEIGHTS": '{"中等":100}'},
        clear=False,
    ), mock.patch("tools.pipeline_daemon.run_command", return_value=0) as run:
        result = create_batch(
            Path(raw) / "production.sqlite3",
            "codex",
            topics,
            "news-20260909-001",
            Path(raw) / "author.log",
            600,
        )

    assert result == 0
    prompt = run.call_args.args[0][-1]
    assert prompt.startswith("使用 $cc-usr-question-author 创建批次。\n")
    assert "批次名：news-20260909-001" in prompt
    assert "题目数量：10" in prompt
    assert "难度分配：中等 10 道（100%）" in prompt
    assert "业务关键词：从 https://news.example.com/ 读取主题来进行出题" in prompt
    assert "自动出题批次只允许使用 SQLite 作为持久化存储" in prompt
    assert "不得使用 PostgreSQL、Redis、MongoDB、MySQL 或其他外部数据库和缓存服务" in prompt
    assert "Dockerfile" not in prompt
    assert "docker-compose" not in prompt
    assert "在整批中合理覆盖 Node.js（JavaScript 或 TypeScript）、Python、Go、Java" in prompt
    assert "每道题只选择其中一种主要后端技术栈" in prompt
    assert "每条新闻必须且只能生成一道题" in prompt
    assert '"question_no": 10' in prompt
    assert "只允许纯后端项目" in prompt
    assert "Go、Python、Node.js（JavaScript 或 TypeScript）、Java" in prompt
    for excluded in ("Kotlin", "C#/.NET", "Rust", "PHP"):
        assert excluded not in prompt
    assert "不得要求或创建任何前端页面" in prompt
    assert "不得生成全栈题" in prompt
    assert "不依赖浏览器操作" in prompt
    assert "每道题的 difficulty 字段必须严格按上述题数分配" in prompt
    assert "使用 $cc-usr-question-qc 完成重复、自然度和反模板质检" in prompt
    assert "不要启动目标模型" in prompt
    assert "不得读取、调用或修改 SchedulerStore" in prompt
    assert "不得为了阻止目标模型而暂停、停止或重启调度器" in prompt
    assert "由调度器自动接管后续模型流水线" in prompt


def test_author_prompt_lists_each_configured_news_source_once() -> None:
    topics = [
        {"source_url": "https://news.example.com/a", "title": "a"},
        {"source_url": "https://news.example.com/a", "title": "b"},
        {"source_url": "https://news.example.com/b", "title": "c"},
    ]
    prompt = author_prompt(topics, "091001", 3, "中等 2 道（67%）、困难 1 道（33%）")
    requirement = next(line for line in prompt.splitlines() if line.startswith("出题要求："))
    assert requirement.count("https://news.example.com/a") == 1
    assert requirement.count("https://news.example.com/b") == 1
    assert "批次名：091001" in prompt


def test_claim_topics_requires_enough_distinct_rows() -> None:
    with tempfile.TemporaryDirectory() as raw:
        database = Path(raw) / "production.sqlite3"
        with connect(database) as connection:
            for index in range(1, 4):
                connection.execute(
                    "INSERT INTO news_topics(source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'new','now','now')",
                    ("https://news.example", f"https://news.example/{index}", f"topic {index}", f"hash-{index}"),
                )
            connection.commit()

        assert claim_topics(database, 4) == []
        with connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM news_topics WHERE status='new'"
            ).fetchone()[0] == 3

        claimed = claim_topics(database, 3)
        assert [topic["id"] for topic in claimed] == [1, 2, 3]
        with connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM news_topics WHERE status='claimed'"
            ).fetchone()[0] == 3


def test_news_count_connections_do_not_accumulate_file_descriptors() -> None:
    descriptor_root = Path("/dev/fd")
    if not descriptor_root.exists():
        pytest.skip("file descriptor inventory is unavailable")
    with tempfile.TemporaryDirectory() as raw:
        database = Path(raw) / "production.sqlite3"
        before = len(list(descriptor_root.iterdir()))
        for _ in range(100):
            assert count_new_topics(database) == 0
        after = len(list(descriptor_root.iterdir()))
    assert after - before < 5


class StopAfterFirstWait:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.stopped = True
        return True


def test_news_below_watermark_refills_then_waits_for_a_complete_batch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        with connect(database) as connection:
            for index in range(18):
                connection.execute(
                    "INSERT INTO news_topics(source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'new','now','now')",
                    ("https://news.example", f"https://news.example/{index}", f"topic {index}", f"hash-{index}"),
                )
            connection.commit()
        args = Namespace(
            news_watermark=40, ready_watermark=40, dynamic_feeds=False,
            feeds=["https://news.example"], feed_timeout=1, poll_seconds=60,
        )
        store = mock.Mock()
        store.state.return_value = {"desired_state": "running"}
        stop = StopAfterFirstWait()
        sentinel = root / "producer.done"
        with mock.patch.dict(os.environ, {"CC_AUTHOR_BATCH_SIZE": "20"}), mock.patch(
            "tools.pipeline_daemon.count_ready", return_value=0
        ), mock.patch(
            "tools.pipeline_daemon.ingest_report", return_value={
                "added": 0, "parsed": 18, "duplicates": 18,
                "errors": [], "feeds": [],
            }
        ) as ingest_news, mock.patch(
            "tools.pipeline_daemon.create_next_batch"
        ) as create_next:
            maintain_ready_buffer(database, args, store, stop, sentinel, {})

        ingest_news.assert_called_once()
        create_next.assert_not_called()
        assert stop.waits == [60]
        assert "新闻待用 18/40 条" in store.heartbeat.call_args.kwargs["detail"]
        assert store.event.call_args.args[0] == "news_refill"
        assert "新增 0 条" in store.event.call_args.args[1]
        assert store.event.call_args.kwargs["details"]["duplicates"] == 18


def test_news_below_watermark_still_creates_batch_when_enough_exist() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        with connect(database) as connection:
            for index in range(39):
                connection.execute(
                    "INSERT INTO news_topics(source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'new','now','now')",
                    ("https://news.example", f"https://news.example/{index}", f"topic {index}", f"hash-{index}"),
                )
            connection.commit()
        args = Namespace(
            news_watermark=40, ready_watermark=40, dynamic_feeds=False,
            feeds=["https://news.example"], feed_timeout=1, poll_seconds=60,
        )
        store = mock.Mock()
        store.state.return_value = {"desired_state": "running"}
        stop = threading.Event()

        def create_and_stop(*_args):
            stop.set()
            return 0, "batch"

        with mock.patch.dict(os.environ, {"CC_AUTHOR_BATCH_SIZE": "20"}), mock.patch(
            "tools.pipeline_daemon.count_ready", return_value=0
        ), mock.patch(
            "tools.pipeline_daemon.ingest_report", return_value={
                "added": 0, "parsed": 39, "duplicates": 39,
                "errors": [], "feeds": [],
            }
        ) as ingest_news, mock.patch(
            "tools.pipeline_daemon.create_next_batch", side_effect=create_and_stop
        ) as create_next:
            maintain_ready_buffer(database, args, store, stop, root / "producer.done", {})

        ingest_news.assert_called_once()
        create_next.assert_called_once()


def test_news_refill_runs_even_when_question_buffer_is_full() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        stop = StopAfterFirstWait()
        args = Namespace(
            news_watermark=40, ready_watermark=40, dynamic_feeds=False,
            feeds=["https://news.example"], feed_timeout=1, poll_seconds=60,
        )
        store = mock.Mock()
        store.state.return_value = {"desired_state": "running"}
        with mock.patch(
            "tools.pipeline_daemon.count_ready", return_value=40
        ), mock.patch(
            "tools.pipeline_daemon.ingest_report", return_value={
                "added": 0, "parsed": 40, "duplicates": 40,
                "errors": [], "feeds": [],
            }
        ) as ingest_news, mock.patch(
            "tools.pipeline_daemon.create_next_batch"
        ) as create_next:
            maintain_ready_buffer(database, args, store, stop, root / "producer.done", {})

        ingest_news.assert_called_once()
        create_next.assert_not_called()
        assert stop.waits == [2]


def test_recover_interrupted_authoring_releases_incomplete_claims() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        SchedulerStore(database)
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO batches(name,folder_path,markdown_path,question_count,status,created_at,updated_at) "
                "VALUES('batch',?,?,2,'draft','now','now')",
                (str(root / "batch"), str(root / "batch.md")),
            )
            for topic_id in (1, 2):
                connection.execute(
                    "INSERT INTO news_topics(id,source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'claimed','now','now')",
                    (topic_id, "https://news.example", f"https://news.example/{topic_id}", "topic", f"hash-{topic_id}"),
                )
            connection.execute(
                "INSERT INTO scheduler_events(created_at,event_type,batch_name,message,details_json) "
                "VALUES('now','topics_claimed','batch','claimed',?)",
                ('{"topic_ids":[1,2]}',),
            )
            connection.commit()

        assert recover_interrupted_authoring(database) == (2, 0)
        with connect(database) as connection:
            assert connection.execute("SELECT status FROM batches WHERE name='batch'").fetchone()[0] == "failed"
            assert connection.execute("SELECT COUNT(*) FROM news_topics WHERE status='new'").fetchone()[0] == 2


def test_recover_interrupted_authoring_preserves_complete_batch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        SchedulerStore(database)
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO batches(name,folder_path,markdown_path,question_count,status,created_at,updated_at) "
                "VALUES('batch',?,?,1,'draft','now','now')",
                (str(root / "batch"), str(root / "batch.md")),
            )
            batch_id = connection.execute("SELECT id FROM batches WHERE name='batch'").fetchone()[0]
            connection.execute(
                "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,prompt_sha256,"
                "task_type,difficulty,languages,repo_url,initial_snapshot,local_initial_sha,reproducibility,"
                "mechanical_qc,qc_decision,qc_prompt_sha256,status,created_at,updated_at) "
                "VALUES(?,1,'task','q001',?,'title','prompt','hash','0-1 代码生成','中等','Python',"
                "'https://github.com/org/repo','https://github.com/org/repo/commit/sha','sha','ok',"
                "'pass','pass','hash','approved','now','now')",
                (batch_id, str(root / "q001")),
            )
            connection.execute(
                "INSERT INTO news_topics(id,source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                "VALUES(1,'https://news.example','https://news.example/1','topic','hash','claimed','now','now')"
            )
            connection.execute(
                "INSERT INTO scheduler_events(created_at,event_type,batch_name,message,details_json) "
                "VALUES('now','topics_claimed','batch','claimed','{\"topic_ids\":[1]}')"
            )
            connection.commit()

        assert recover_interrupted_authoring(database) == (0, 1)
        with connect(database) as connection:
            assert connection.execute("SELECT status FROM batches WHERE name='batch'").fetchone()[0] == "ready"
            topic = connection.execute("SELECT status,used_batch FROM news_topics WHERE id=1").fetchone()
            assert tuple(topic) == ("used", "batch")


def test_create_batch_rejects_topic_count_mismatch() -> None:
    with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
        os.environ, {"CC_AUTHOR_BATCH_SIZE": "2"}, clear=False,
    ):
        with pytest.raises(ValueError, match="expected 2 news topics, got 1"):
            create_batch(
                Path(raw) / "production.sqlite3",
                "codex",
                [{"title": "only one"}],
                "batch",
                Path(raw) / "author.log",
                60,
            )


def test_terminal_batches_are_not_scheduled_again() -> None:
    with tempfile.TemporaryDirectory() as raw:
        database = Path(raw) / "production.sqlite3"
        with connect(database) as connection:
            for name, status in (("active", "draft"), ("done", "completed"), ("some", "partial"), ("none", "failed")):
                connection.execute(
                    "INSERT INTO batches(name,folder_path,markdown_path,question_count,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'now','now')",
                    (name, str(Path(raw) / name), str(Path(raw) / f"{name}.md"), 1, status),
                )
            connection.execute(
                "INSERT INTO author_jobs(batch_name,question_count,prompt,status,created_at) "
                "VALUES('manual',1,'prompt','running','now')"
            )
            connection.commit()
        assert active_batches(database) == ["active"]
        assert manual_jobs_active(database)


def test_batch_timeout_scales_with_question_waves_and_delivery_pool() -> None:
    assert pipeline_batch_timeout(
        question_count=10,
        model_concurrency=2,
        codex_concurrency=2,
        worker_timeout=3600,
        agent_timeout=3600,
        max_attempts=2,
    ) == 40200


def test_author_only_cycle_creates_batch_without_running_model_pipeline() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        with connect(database) as connection:
            for index in range(1, 11):
                connection.execute(
                    "INSERT INTO news_topics(source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'new','now','now')",
                    ("https://news.example", f"https://news.example/{index}", f"topic {index}", f"hash-{index}"),
                )
            connection.commit()
        args = Namespace(
            db=database, env_file=root / ".env", dynamic_feeds=False, feeds=[],
            feed_timeout=1, concurrency=2, codex_concurrency=1,
            worker_image="image", run_mode="author_only", log_dir=root / "runs" / "daemon",
            agent_timeout=60, codex="codex", ready_watermark=4,
        )
        with mock.patch("tools.pipeline_daemon.ingest", return_value=(0, [])), mock.patch(
            "tools.pipeline_daemon.create_batch", return_value=0
        ) as author, mock.patch(
            "tools.pipeline_daemon.authored_batch_result", return_value=(True, 10, 10)
        ), mock.patch("tools.pipeline_daemon.run_batch") as run_batch:
            code, batch = cycle(args)
        assert code == 0
        assert len(batch) == 10 and batch.isdigit()
        author.assert_called_once()
        assert len(author.call_args.args[2]) == 10
        run_batch.assert_not_called()
        with connect(database) as connection:
            topics = connection.execute(
                "SELECT status,used_batch FROM news_topics ORDER BY id"
            ).fetchall()
        assert [tuple(topic) for topic in topics] == [("used", batch)] * 10


def test_authored_batch_must_have_every_question_ready() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO batches(name,folder_path,markdown_path,question_count,created_at,updated_at) "
                "VALUES('batch',?,?,2,'now','now')",
                (str(root / "batch"), str(root / "batch.md")),
            )
            batch_id = connection.execute("SELECT id FROM batches").fetchone()[0]
            for number, status in ((1, "approved"), (2, "draft")):
                connection.execute(
                    "INSERT INTO questions(batch_id,question_no,task_id,folder_name,folder_path,title,prompt,"
                    "prompt_sha256,task_type,difficulty,languages,repo_url,initial_snapshot,local_initial_sha,"
                    "reproducibility,mechanical_qc,qc_decision,qc_prompt_sha256,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'title','prompt','hash','0-1 代码生成','中等','Python',"
                    "'https://github.com/org/repo','https://github.com/org/repo/commit/sha','sha',"
                    "'无外部依赖','pass','pass','hash',?,'now','now')",
                    (batch_id, number, f"task-{number}", f"q{number}", str(root / f"q{number}"), status),
                )
            connection.commit()
        assert authored_batch_result(database, "batch", 2) == (False, 2, 1)
        with connect(database) as connection:
            assert connection.execute("SELECT status FROM batches").fetchone()[0] == "failed"
            connection.execute("UPDATE questions SET status='approved'")
            connection.commit()
        assert authored_batch_result(database, "batch", 2) == (True, 2, 2)
        with connect(database) as connection:
            assert connection.execute("SELECT status FROM batches").fetchone()[0] == "ready"


def test_author_failure_is_retryable_and_releases_claimed_topics() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "production.sqlite3"
        with connect(database) as connection:
            for index in range(1, 3):
                connection.execute(
                    "INSERT INTO news_topics(source_url,article_url,title,topic_hash,status,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'new','now','now')",
                    ("https://news.example", f"https://news.example/{index}", f"topic {index}", f"hash-{index}"),
                )
            connection.commit()
        args = Namespace(
            db=database, codex="codex", log_dir=root / "runs" / "daemon",
            agent_timeout=60,
        )
        with mock.patch.dict(os.environ, {"CC_AUTHOR_BATCH_SIZE": "2"}), mock.patch(
            "tools.pipeline_daemon.create_batch", side_effect=TimeoutError("author timeout")
        ):
            code, batch = create_next_batch(database, args)
        assert code == AUTHOR_FAILURE_EXIT
        assert batch.isdigit()
        with connect(database) as connection:
            statuses = connection.execute(
                "SELECT status FROM news_topics ORDER BY id"
            ).fetchall()
        assert [row["status"] for row in statuses] == ["new", "new"]
