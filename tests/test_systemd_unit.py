from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "ccusr-pipeline.service"


def test_pipeline_service_keeps_vps_concurrency_limits() -> None:
    content = UNIT.read_text(encoding="utf-8")

    assert "User=ubuntu" in content
    assert "WorkingDirectory=/home/ubuntu/UserSatisfactionRating-codex" in content
    assert "pipeline_daemon.py --loop --concurrency 2" in content
    assert "--worker-image ccusr-claude-worker:local" in content
    assert "Restart=on-failure" in content
