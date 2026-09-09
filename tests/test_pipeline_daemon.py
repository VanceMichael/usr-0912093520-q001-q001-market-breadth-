import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from tools.pipeline_daemon import (
    active_batches,
    cleanup_logs,
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
    topic = {
        "title": "城市公共数据服务升级",
        "summary": "围绕可靠的数据交换与异步处理能力建设",
        "article_url": "https://news.example.com/article/1",
        "source_url": "https://news.example.com/",
        "published_at": "2026-09-09T08:00:00+08:00",
    }
    with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
        os.environ,
        {"CC_AUTHOR_BATCH_SIZE": "10", "CC_AUTHOR_DIFFICULTY_WEIGHTS": '{"中等":100}'},
        clear=False,
    ), mock.patch("tools.pipeline_daemon.run_command", return_value=0) as run:
        result = create_batch(
            Path(raw) / "production.sqlite3",
            "codex",
            topic,
            "news-20260909-001",
            Path(raw) / "author.log",
            600,
        )

    assert result == 0
    prompt = run.call_args.args[0][-1]
    assert "生成 10 道" in prompt
    assert "只允许纯后端项目" in prompt
    assert "Go、Python、Node.js（JavaScript 或 TypeScript）、Java" in prompt
    for excluded in ("Kotlin", "C#/.NET", "Rust", "PHP"):
        assert excluded not in prompt
    assert "不得要求或创建任何前端页面" in prompt
    assert "不得生成全栈题" in prompt
    assert "不依赖浏览器操作" in prompt


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
