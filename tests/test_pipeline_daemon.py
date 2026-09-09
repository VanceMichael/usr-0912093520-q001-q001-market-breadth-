import os
import tempfile
from pathlib import Path
from unittest import mock

from tools.pipeline_daemon import difficulty_distribution, difficulty_plan, load_runtime_env


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

