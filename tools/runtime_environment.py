"""Runtime probes and conservative repairs shared by the local console and pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def process_alive(pid: object) -> bool:
    """Return whether a PID represents a live, non-zombie process."""
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    permission_denied = False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        permission_denied = True
    except OSError:
        return False
    if os.name == "nt":
        return True
    try:
        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(value)], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    state = result.stdout.strip()
    if result.returncode != 0 or not state:
        return permission_denied
    return not state.startswith("Z")


def docker_info(docker: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker, "info"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=timeout,
    )


def repair_docker_engine(
    docker: str, *, timeout: int = 120, poll_interval: float = 2.0,
) -> tuple[bool, str]:
    """Start Docker Desktop where supported and wait for its engine."""
    try:
        if sys.platform == "win32":
            candidates: list[Path] = []
            program_files = os.environ.get("ProgramFiles", "").strip()
            if program_files:
                candidates.append(Path(program_files) / "Docker" / "Docker" / "Docker Desktop.exe")
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                candidates.append(Path(local_app_data) / "Docker" / "Docker Desktop.exe")
            desktop = next((path for path in candidates if path.is_file()), None)
            if desktop is None:
                return False, "未找到 Docker Desktop，无法自动启动 Docker 引擎"
            subprocess.Popen(
                [str(desktop)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif sys.platform == "darwin":
            opener = shutil.which("open") or "/usr/bin/open"
            started = subprocess.run(
                [opener, "-gja", "Docker"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=30,
            )
            if started.returncode:
                return False, "无法启动 Docker Desktop：" + started.stdout.strip()[-300:]
        else:
            return False, "当前系统无法自动启动 Docker 引擎，请先启动 Docker"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"启动 Docker Desktop 失败：{exc}"

    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            result = docker_info(docker)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
        else:
            if result.returncode == 0:
                return True, "已启动 Docker Desktop，Docker 引擎可以连接"
            last_error = result.stdout.strip()[-300:]
        time.sleep(poll_interval)
    return False, "Docker Desktop 已启动，但等待引擎就绪超时" + (f"：{last_error}" if last_error else "")
