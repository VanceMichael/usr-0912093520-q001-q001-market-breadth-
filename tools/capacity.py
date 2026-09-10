#!/usr/bin/env python3
"""Cross-platform scheduler capacity detection and concurrency profiles."""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import subprocess
from dataclasses import asdict, dataclass


MAX_CONCURRENCY = 32
WORKER_MEMORY_GB = 2.0


@dataclass(frozen=True)
class MachineCapacity:
    platform: str
    host_cpu: int
    host_memory_bytes: int
    docker_cpu: int
    docker_memory_bytes: int
    effective_cpu: int
    effective_memory_bytes: int
    docker_available: bool


def _windows_memory_bytes() -> int:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.total_physical)


def host_memory_bytes() -> int:
    system = platform.system()
    if system == "Windows":
        try:
            return _windows_memory_bytes()
        except (AttributeError, OSError):
            return 0
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
            if result.returncode == 0:
                return int(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def available_memory_bytes() -> int:
    system = platform.system()
    if system == "Windows":
        try:
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        except (AttributeError, OSError):
            return 0
    if system == "Linux":
        try:
            for line in open("/proc/meminfo", encoding="ascii"):
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return 0
    if system == "Darwin":
        try:
            page = subprocess.run(
                ["sysctl", "-n", "hw.pagesize"], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            vm = subprocess.run(
                ["vm_stat"], text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            page_size = int(page.stdout.strip())
            values: dict[str, int] = {}
            for line in vm.stdout.splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().rstrip("."))
            pages = sum(values.get(key, 0) for key in (
                "Pages free", "Pages inactive", "Pages speculative", "Pages purgeable",
            ))
            return pages * page_size
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0
    return 0


def _size_bytes(value: str) -> int:
    match = re.fullmatch(
        r"\s*([0-9.]+)\s*([kmgt]?i?b)\s*", value, re.IGNORECASE
    )
    if not match:
        return 0
    scale = {
        "b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2,
        "gb": 1000 ** 3, "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4,
    }
    return int(float(match.group(1)) * scale[match.group(2).lower()])


def effective_available_memory_bytes(machine: MachineCapacity) -> int:
    if machine.docker_available and machine.platform in {"Darwin", "Windows"}:
        try:
            result = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=15, check=False,
            )
            used = 0
            for line in result.stdout.splitlines():
                payload = json.loads(line)
                usage = str(payload.get("MemUsage") or "").split("/", 1)[0]
                used += _size_bytes(usage)
            if result.returncode == 0:
                return max(0, machine.effective_memory_bytes - used)
        except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
            pass
    return available_memory_bytes()


def adaptive_model_limit(
    configured_max: int,
    running: int,
    worker_memory_gb: float = WORKER_MEMORY_GB,
    capacity: MachineCapacity | None = None,
) -> tuple[int, str]:
    """Return a cross-platform admission limit; running workers are never killed."""
    machine = capacity or detect_capacity()
    maximum = max(1, min(MAX_CONCURRENCY, configured_max))
    available = effective_available_memory_bytes(machine)
    total = machine.effective_memory_bytes
    if available <= 0 or total <= 0:
        return maximum, "资源采样不可用，保持配置上限"
    free_ratio = available / total
    reserve = max(3.0, min(6.0, total / 1024 ** 3 * 0.15))
    memory_slots = max(0, math.floor((available / 1024 ** 3 - reserve) / worker_memory_gb))
    limit = min(maximum, running + memory_slots)
    if free_ratio < 0.12:
        limit = running
        reason = "可用内存低于 12%，暂停领取新模型任务"
    elif free_ratio < 0.20:
        limit = min(limit, max(running, math.ceil(maximum * 0.75)))
        reason = "可用内存低于 20%，降低模型任务放行量"
    else:
        reason = "CPU 与内存允许按配置上限补充模型任务"
    try:
        load_one = os.getloadavg()[0]
    except (AttributeError, OSError):
        load_one = 0.0
    if free_ratio >= 0.12 and load_one > machine.effective_cpu * 1.5:
        limit = min(limit, max(running, math.ceil(maximum * 0.75)))
        reason = "一分钟系统负载过高，降低模型任务放行量"
    return max(running, limit), reason


def docker_capacity() -> tuple[int, int, bool]:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
        cpu = int(payload.get("NCPU") or 0)
        memory = int(payload.get("MemTotal") or 0)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return 0, 0, False
    return cpu, memory, bool(cpu and memory)


def detect_capacity() -> MachineCapacity:
    host_cpu = max(1, int(os.cpu_count() or 1))
    host_memory = max(0, host_memory_bytes())
    docker_cpu, docker_memory, docker_available = docker_capacity()
    effective_cpu = docker_cpu if docker_available else host_cpu
    effective_memory = docker_memory if docker_available else host_memory
    if effective_memory <= 0:
        effective_memory = 8 * 1024 ** 3
    return MachineCapacity(
        platform=platform.system() or "Unknown",
        host_cpu=host_cpu,
        host_memory_bytes=host_memory,
        docker_cpu=docker_cpu,
        docker_memory_bytes=docker_memory,
        effective_cpu=max(1, effective_cpu),
        effective_memory_bytes=effective_memory,
        docker_available=docker_available,
    )


def _profile(model_concurrency: int) -> dict[str, int]:
    model = max(1, min(MAX_CONCURRENCY, model_concurrency))
    codex = max(1, min(MAX_CONCURRENCY, math.ceil(model * 0.42)))
    batch_size = min(20, max(10, model * 2 if model < 10 else 20))
    return {
        "qc_concurrency": 2,
        "model_concurrency": model,
        "codex_concurrency": codex,
        "batch_size": batch_size,
        "ready_target": max(40, batch_size * 2),
    }


def concurrency_recommendation(
    capacity: MachineCapacity | None = None,
) -> dict[str, object]:
    machine = capacity or detect_capacity()
    cpu = machine.effective_cpu
    memory_gb = machine.effective_memory_bytes / 1024 ** 3

    stable_reserve = max(4.0, min(8.0, memory_gb * 0.25))
    maximum_reserve = max(3.0, min(4.0, memory_gb * 0.125))
    stable_by_memory = max(1, math.floor((memory_gb - stable_reserve) / WORKER_MEMORY_GB))
    maximum_by_memory = max(1, math.floor((memory_gb - maximum_reserve) / WORKER_MEMORY_GB))
    stable_by_cpu = cpu if cpu <= 8 else math.floor(cpu * 0.75)
    maximum_by_cpu = math.ceil(cpu * 1.25) if cpu <= 8 else math.floor(cpu * 0.875)
    stable_model = min(MAX_CONCURRENCY, stable_by_memory, max(1, stable_by_cpu))
    maximum_model = min(
        MAX_CONCURRENCY, maximum_by_memory,
        max(stable_model, maximum_by_cpu),
    )

    warnings: list[str] = []
    if not machine.docker_available:
        warnings.append("未读取到 Docker 容量，当前按宿主机资源估算；启动调度器前需确认 Docker 可用。")
    elif machine.docker_cpu < machine.host_cpu or (
        machine.host_memory_bytes and machine.docker_memory_bytes < machine.host_memory_bytes
    ):
        warnings.append("Docker 可用资源低于宿主机，推荐值已按 Docker 实际配额计算。")
    if memory_gb / max(1, cpu) < 2:
        warnings.append("有效内存低于每 CPU 2GB，模型并发主要受内存限制。")

    return {
        "machine": asdict(machine),
        "recommended": _profile(stable_model),
        "maximum": _profile(maximum_model),
        "worker_limits": {"cpus": 1.0, "memory": "2g"},
        "warnings": warnings,
    }
