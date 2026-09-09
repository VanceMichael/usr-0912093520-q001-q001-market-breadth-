import subprocess
from unittest import mock

from tools import runtime_environment


def test_repair_docker_engine_starts_macos_app_and_waits_until_ready():
    started = subprocess.CompletedProcess([], 0, "")
    unavailable = subprocess.CompletedProcess([], 1, "not ready")
    ready = subprocess.CompletedProcess([], 0, "ready")
    with mock.patch.object(runtime_environment.sys, "platform", "darwin"), mock.patch.object(
        runtime_environment.shutil, "which", return_value="/usr/bin/open"
    ), mock.patch.object(
        runtime_environment.subprocess, "run", return_value=started
    ) as run, mock.patch.object(
        runtime_environment, "docker_info", side_effect=[unavailable, ready]
    ), mock.patch.object(runtime_environment.time, "sleep"):
        ok, detail = runtime_environment.repair_docker_engine("docker", timeout=10)

    assert ok is True
    assert "可以连接" in detail
    run.assert_called_once_with(
        ["/usr/bin/open", "-gja", "Docker"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=30,
    )


def test_repair_docker_engine_does_not_change_linux_services():
    with mock.patch.object(runtime_environment.sys, "platform", "linux"):
        ok, detail = runtime_environment.repair_docker_engine("docker")

    assert ok is False
    assert "请先启动 Docker" in detail
