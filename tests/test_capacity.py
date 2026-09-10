from unittest import mock

from tools.capacity import (
    MachineCapacity,
    adaptive_model_limit,
    concurrency_recommendation,
    detect_capacity,
)


GIB = 1024 ** 3


def machine(cpu: int, memory_gb: int) -> MachineCapacity:
    return MachineCapacity(
        platform="Linux",
        host_cpu=cpu,
        host_memory_bytes=memory_gb * GIB,
        docker_cpu=cpu,
        docker_memory_bytes=memory_gb * GIB,
        effective_cpu=cpu,
        effective_memory_bytes=memory_gb * GIB,
        docker_available=True,
    )


def test_eight_core_profile_uses_eight_stable_and_ten_maximum_workers() -> None:
    result = concurrency_recommendation(machine(8, 32))
    assert result["recommended"]["model_concurrency"] == 8
    assert result["recommended"]["codex_concurrency"] == 4
    assert result["maximum"]["model_concurrency"] == 10
    assert result["maximum"]["codex_concurrency"] == 5


def test_sixteen_core_profile_uses_twelve_stable_and_fourteen_maximum_workers() -> None:
    result = concurrency_recommendation(machine(16, 32))
    assert result["recommended"] == {
        "qc_concurrency": 2,
        "model_concurrency": 12,
        "codex_concurrency": 6,
        "batch_size": 20,
        "ready_target": 40,
    }
    assert result["maximum"]["model_concurrency"] == 14
    assert result["maximum"]["codex_concurrency"] == 6


def test_detect_capacity_prefers_docker_desktop_limits() -> None:
    with mock.patch("tools.capacity.os.cpu_count", return_value=10), mock.patch(
        "tools.capacity.host_memory_bytes", return_value=64 * GIB
    ), mock.patch("tools.capacity.docker_capacity", return_value=(6, 16 * GIB, True)):
        result = detect_capacity()
    assert result.host_cpu == 10
    assert result.effective_cpu == 6
    assert result.effective_memory_bytes == 16 * GIB


def test_adaptive_limit_stops_admission_under_memory_pressure() -> None:
    with mock.patch("tools.capacity.effective_available_memory_bytes", return_value=3 * GIB):
        limit, reason = adaptive_model_limit(14, 8, capacity=machine(16, 32))
    assert limit == 8
    assert "暂停" in reason


def test_adaptive_limit_never_terminates_running_workers() -> None:
    with mock.patch("tools.capacity.effective_available_memory_bytes", return_value=2 * GIB):
        limit, _reason = adaptive_model_limit(6, 9, capacity=machine(8, 32))
    assert limit == 9
