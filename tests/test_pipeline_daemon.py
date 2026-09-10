import os
import tempfile
import time
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

from tools.pipeline_daemon import (
    active_batches,
    author_prompt,
    authored_batch_result,
    cleanup_logs,
    claim_topics,
    child_process_kwargs,
    cycle,
    create_batch,
    difficulty_distribution,
    difficulty_plan,
    load_runtime_env,
    manual_jobs_active,
    pipeline_batch_timeout,
)
from tools.batch_pipeline import connect


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
    assert "技术关键词：需要 Docker" in prompt
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
