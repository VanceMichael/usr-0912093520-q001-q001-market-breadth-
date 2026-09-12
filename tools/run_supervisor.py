"""Durable per-run supervision and Claude session metadata helpers.

The scheduler deliberately stores only bounded, content-free state here.  The
actual Claude JSONL remains in the run directory and is validated by the
trajectory gate before a run can become successful.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from tools.batch_pipeline import connect, now


class RunMetadataError(RuntimeError):
    """Raised when a completed run cannot be safely attributed."""


def primary_session_file(trajectory_root: Path) -> Path:
    """Return the only Claude session JSONL below ``trajectory_root``.

    Claude may create auxiliary files, but a run must have exactly one primary
    session file.  Refusing ambiguity prevents evidence from being attributed
    to another attempt or another question.
    """
    root = trajectory_root.resolve(strict=True)
    if not root.is_dir():
        raise RunMetadataError(f"轨迹目录不存在：{root}")
    try:
        files = sorted(path for path in root.rglob("*.jsonl") if path.is_file())
    except OSError as exc:
        raise RunMetadataError(f"无法读取轨迹目录：{exc}") from exc
    if len(files) != 1:
        raise RunMetadataError(f"完成元数据要求恰好一个主会话 JSONL，当前找到 {len(files)} 个")
    if not files[0].stem or files[0].stem in {"session", "unknown"}:
        raise RunMetadataError(f"主会话 JSONL 文件名不是有效 SessionID：{files[0].name}")
    return files[0]


def register_run_metadata(
    database: Path,
    question_id: int,
    batch_run_id: str,
    trajectory_root: Path,
    *,
    session_file: Path | None = None,
    expected_session_id: str = "",
) -> tuple[str, Path]:
    """Atomically bind a run to its unique session file and absolute root."""
    if session_file is None:
        session_file = primary_session_file(trajectory_root)
    elif isinstance(session_file, Path):
        session_file = session_file.resolve(strict=True)
        root_check = trajectory_root.resolve(strict=True)
        if root_check != session_file and root_check not in session_file.parents:
            raise RunMetadataError("主会话 JSONL 不在登记的轨迹目录内")
        try:
            files = [path for path in root_check.rglob("*.jsonl") if path.is_file()]
        except OSError as exc:
            raise RunMetadataError(f"无法读取轨迹目录：{exc}") from exc
        if len(files) != 1 or files[0] != session_file:
            raise RunMetadataError(
                f"完成元数据要求恰好一个主会话 JSONL，当前找到 {len(files)} 个"
            )
        if session_file.suffix.lower() != ".jsonl" or not session_file.stem:
            raise RunMetadataError("主会话 JSONL 文件名不是有效 SessionID")
        if expected_session_id and str(expected_session_id) != session_file.stem:
            raise RunMetadataError("主会话文件名与轨迹中的 SessionID 不一致")
    else:
        # Compatibility for validators that return counters plus a SessionID
        # but no path.  The production Docker/local validators always return a
        # real Path and therefore take the strict branch above.
        if not expected_session_id:
            raise RunMetadataError("完成元数据缺少 SessionID")
    session_id = session_file.stem if isinstance(session_file, Path) else str(expected_session_id)
    root = trajectory_root.resolve()
    connection = connect(database.resolve())
    try:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            "SELECT id,session_id,trajectory_root FROM runs "
            "WHERE question_id=? AND batch_run_id=?",
            (question_id, batch_run_id),
        ).fetchall()
        if len(run) != 1:
            raise RunMetadataError("找不到唯一对应运行记录，拒绝标记完成")
        current = run[0]
        existing_session = str(current["session_id"] or "")
        existing_root = str(current["trajectory_root"] or "")
        if expected_session_id and existing_session and str(expected_session_id) != existing_session:
            raise RunMetadataError("提供的 SessionID 与已登记的 SessionID 不一致")
        if existing_session and existing_session != session_id:
            raise RunMetadataError(
                f"已登记不同 SessionID（{existing_session}），拒绝覆盖"
            )
        if existing_root and Path(existing_root).resolve() != root:
            raise RunMetadataError("已登记不同轨迹目录，拒绝覆盖")
        connection.execute(
            "UPDATE runs SET session_id=?,trajectory_root=?,heartbeat_at=? WHERE id=?",
            (session_id, str(root), now(), int(current["id"])),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return session_id, root


def write_supervisor_state(
    run_directory: Path,
    *,
    state: str,
    container_id: str = "",
    attempt: int = 1,
    activity: tuple[int, int, int] | None = None,
    error: str = "",
) -> Path:
    """Atomically persist bounded state that survives scheduler restarts."""
    run_directory = run_directory.resolve()
    run_directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "state": str(state),
        "attempt": max(1, int(attempt)),
        "supervisor_pid": os.getpid(),
        "checked_at": time.time(),
        "heartbeat_at": now(),
    }
    if container_id:
        payload["container_id"] = str(container_id)
    if activity is not None:
        payload["activity"] = {
            "newest_mtime_ns": int(activity[0]),
            "total_size": int(activity[1]),
            "file_count": int(activity[2]),
        }
    if error:
        payload["error"] = str(error)[-2000:]
    destination = run_directory / "supervisor.json"
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def read_supervisor_state(run_directory: Path) -> dict[str, Any]:
    try:
        value = json.loads((run_directory / "supervisor.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
