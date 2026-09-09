import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from tools.pipeline_daemon import (
    cleanup_logs,
    create_batch,
    difficulty_distribution,
    difficulty_plan,
    load_runtime_env,
)


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
    assert "Go、Python、Node.js（JavaScript 或 TypeScript）、Java、Kotlin、C#/.NET、Rust、PHP" in prompt
    assert "不得要求或创建任何前端页面" in prompt
    assert "不得生成全栈题" in prompt
    assert "不依赖浏览器操作" in prompt
