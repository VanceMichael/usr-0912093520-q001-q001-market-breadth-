import tempfile
from pathlib import Path

from tools.batch_pipeline import connect
from tools.scheduler_state import SchedulerStore


def make_store(root: Path) -> SchedulerStore:
    database = root / "production.sqlite3"
    connection = connect(database)
    connection.close()
    return SchedulerStore(database)


def test_scheduler_control_and_cycle_state_are_persistent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = make_store(Path(raw))
        started = store.startup()
        assert started["actual_state"] == "running"
        assert started["pid"]

        cycle_id = store.begin_cycle()
        store.heartbeat(phase="author", batch="news0909000000", detail="正在出题")
        store.finish_cycle(cycle_id, status="completed", batch="news0909000000")

        state = store.state()
        assert state["cycle_count"] == 1
        assert state["consecutive_failures"] == 0
        assert store.cycles()[0]["batch_name"] == "news0909000000"
        assert any(event["event_type"] == "cycle_completed" for event in store.events())

        result = store.request_control("drain")
        assert result["desired_state"] == "draining"
        assert SchedulerStore(store.database).state()["desired_state"] == "draining"


def test_scheduler_failure_and_retry_clear_circuit_state() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = make_store(Path(raw))
        cycle_id = store.begin_cycle()
        store.finish_cycle(cycle_id, status="failed", error="network unavailable")
        assert store.state()["consecutive_failures"] == 1
        assert store.state()["last_error"] == "network unavailable"

        store.request_control("retry")
        state = store.state()
        assert state["desired_state"] == "running"
        assert state["consecutive_failures"] == 0
        assert state["last_error"] == ""


def test_scheduler_startup_marks_previous_running_cycles_interrupted() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = make_store(Path(raw))
        cycle_id = store.begin_cycle()
        assert store.cycles()[0]["status"] == "running"
        restarted = SchedulerStore(store.database)
        restarted.startup()
        cycle = next(item for item in restarted.cycles() if item["id"] == cycle_id)
        assert cycle["status"] == "interrupted"
        assert cycle["finished_at"]


def test_scheduler_rejects_unknown_control_action() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = make_store(Path(raw))
        try:
            store.request_control("destroy")
        except ValueError as exc:
            assert "不支持" in str(exc)
        else:
            raise AssertionError("unknown action should fail")
