from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "ccusr-pipeline.service"
WINDOWS_INSTALLER = ROOT / "deploy" / "install_windows_scheduler.ps1"


def test_pipeline_service_keeps_vps_concurrency_limits() -> None:
    content = UNIT.read_text(encoding="utf-8")

    assert "User=ubuntu" in content
    assert "WorkingDirectory=/home/ubuntu/UserSatisfactionRating-codex" in content
    assert "pipeline_daemon.py --loop --concurrency 2" in content
    assert "--worker-image ccusr-claude-worker:local" in content
    assert "Restart=on-failure" in content


def test_windows_scheduler_installer_registers_console_and_scheduler_tasks() -> None:
    content = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    assert '"CCUSR Console"' in content
    assert '"CCUSR Scheduler"' in content
    assert "Register-ScheduledTask" in content
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in content
    assert 'Start-ScheduledTask -TaskName "CCUSR Console"' in content
    assert "pipeline_daemon.py`\" --loop" in content
    assert "--concurrency 2" in content
    assert "--codex-concurrency 1" in content
