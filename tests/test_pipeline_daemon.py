import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from tools.pipeline_daemon import cleanup_logs, difficulty_distribution, difficulty_plan, load_runtime_env


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
