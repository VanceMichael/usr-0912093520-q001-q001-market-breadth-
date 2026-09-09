#!/usr/bin/env python3
"""Persistent state and event storage for the autonomous scheduler."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    desired_state TEXT NOT NULL DEFAULT 'running',
    actual_state TEXT NOT NULL DEFAULT 'stopped',
    phase TEXT NOT NULL DEFAULT 'idle',
    batch_name TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    heartbeat_at TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    cycle_count INTEGER NOT NULL DEFAULT 0,
    restart_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scheduler_cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    batch_name TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scheduler_cycles_started
    ON scheduler_cycles(started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS scheduler_events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    event_type TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    batch_name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_scheduler_events_created
    ON scheduler_events(id DESC);

CREATE TABLE IF NOT EXISTS scheduler_controls (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scheduler_controls_requested
    ON scheduler_controls(id DESC);
"""

VALID_ACTIONS = {"start", "pause", "drain", "stop", "restart", "resume", "retry"}
ACTION_DESIRED_STATE = {
    "start": "running",
    "resume": "running",
    "retry": "running",
    "pause": "paused",
    "drain": "draining",
    "stop": "stopped",
    "restart": "restarting",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SchedulerStore:
    def __init__(self, database: Path) -> None:
        self.database = Path(database).absolute()
        self.ensure()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def ensure(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(SCHEDULER_SCHEMA)
            timestamp = now()
            connection.execute(
                "INSERT OR IGNORE INTO scheduler_state(id,updated_at) VALUES(1,?)",
                (timestamp,),
            )
            connection.commit()

    def state(self) -> dict:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
        return dict(row) if row else {}

    def update(self, **values: object) -> dict:
        allowed = {
            "desired_state", "actual_state", "phase", "batch_name", "detail", "pid",
            "heartbeat_at", "started_at", "last_error", "cycle_count", "restart_count",
            "consecutive_failures",
        }
        fields = [(key, value) for key, value in values.items() if key in allowed]
        fields.append(("updated_at", now()))
        assignments = ",".join(f"{key}=?" for key, _value in fields)
        with closing(self.connect()) as connection:
            connection.execute(
                f"UPDATE scheduler_state SET {assignments} WHERE id=1",
                [value for _key, value in fields],
            )
            connection.commit()
        return self.state()

    def event(
        self,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        phase: str = "",
        batch: str = "",
        details: dict | None = None,
    ) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO scheduler_events(created_at,level,event_type,phase,batch_name,message,details_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (now(), level, event_type, phase, batch, message, json.dumps(details or {}, ensure_ascii=False)),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def startup(self) -> dict:
        state = self.state()
        desired = str(state.get("desired_state") or "running")
        if desired in {"restarting", "draining"}:
            desired = "running"
        actual = "running" if desired == "running" else "paused" if desired == "paused" else "stopped"
        started_at = now()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE scheduler_cycles SET status='interrupted',finished_at=?,error=? WHERE status='running'",
                (started_at, "调度器进程重启，上一周期被中断"),
            )
            connection.commit()
        updated = self.update(
            desired_state=desired,
            actual_state=actual,
            phase="idle",
            detail="调度器已启动",
            pid=os.getpid(),
            heartbeat_at=started_at,
            started_at=started_at,
            restart_count=int(state.get("restart_count") or 0) + 1,
        )
        self.event("scheduler_started", "调度器进程已启动", details={"pid": os.getpid()})
        return updated

    def heartbeat(self, *, phase: str | None = None, batch: str | None = None, detail: str | None = None) -> dict:
        values: dict[str, object] = {"heartbeat_at": now(), "pid": os.getpid()}
        if phase is not None:
            values["phase"] = phase
        if batch is not None:
            values["batch_name"] = batch
        if detail is not None:
            values["detail"] = detail
        updated = self.update(**values)
        if batch:
            with closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE scheduler_cycles SET batch_name=? WHERE id=("
                    "SELECT id FROM scheduler_cycles WHERE status='running' ORDER BY id DESC LIMIT 1)",
                    (batch,),
                )
                connection.commit()
        return updated

    def request_control(self, action: str) -> dict:
        action = str(action).strip().lower()
        if action not in VALID_ACTIONS:
            raise ValueError("不支持的调度器控制动作")
        desired = ACTION_DESIRED_STATE[action]
        timestamp = now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO scheduler_controls(action,requested_at) VALUES(?,?)",
                (action, timestamp),
            )
            connection.execute(
                "UPDATE scheduler_state SET desired_state=?,updated_at=?,"
                "last_error=CASE WHEN ? IN ('retry','start','resume') THEN '' ELSE last_error END,"
                "consecutive_failures=CASE WHEN ?='retry' THEN 0 ELSE consecutive_failures END WHERE id=1",
                (desired, timestamp, action, action),
            )
            connection.commit()
            control_id = int(cursor.lastrowid)
        self.event("control_requested", f"收到控制指令：{action}", details={"control_id": control_id})
        return {"ok": True, "action": action, "control_id": control_id, "desired_state": desired}

    def apply_controls(self, desired_state: str, message: str = "") -> None:
        timestamp = now()
        with closing(self.connect()) as connection:
            latest = connection.execute(
                "SELECT id FROM scheduler_controls WHERE status='pending' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest:
                connection.execute(
                    "UPDATE scheduler_controls SET status='superseded',applied_at=?,message=? "
                    "WHERE status='pending' AND id<?",
                    (timestamp, "已被更新的控制指令覆盖", latest["id"]),
                )
                connection.execute(
                    "UPDATE scheduler_controls SET status='applied',applied_at=?,message=? WHERE id=?",
                    (timestamp, message, latest["id"]),
                )
            connection.execute(
                "UPDATE scheduler_state SET desired_state=?,updated_at=? WHERE id=1",
                (desired_state, timestamp),
            )
            connection.commit()

    def begin_cycle(self) -> int:
        timestamp = now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO scheduler_cycles(started_at) VALUES(?)", (timestamp,)
            )
            connection.execute(
                "UPDATE scheduler_state SET actual_state='running',phase='news',batch_name='',"
                "detail='正在抓取新闻主题',heartbeat_at=?,updated_at=? WHERE id=1",
                (timestamp, timestamp),
            )
            connection.commit()
            cycle_id = int(cursor.lastrowid)
        self.event("cycle_started", f"生产周期 #{cycle_id} 已开始", phase="news")
        return cycle_id

    def finish_cycle(self, cycle_id: int, *, status: str, batch: str = "", error: str = "") -> None:
        timestamp = now()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE scheduler_cycles SET finished_at=?,status=?,batch_name=?,error=? WHERE id=?",
                (timestamp, status, batch, error, cycle_id),
            )
            if status == "completed":
                connection.execute(
                    "UPDATE scheduler_state SET cycle_count=cycle_count+1,consecutive_failures=0,"
                    "last_error='',phase='idle',batch_name='',detail='等待下一轮',heartbeat_at=?,updated_at=? WHERE id=1",
                    (timestamp, timestamp),
                )
            elif status == "failed":
                connection.execute(
                    "UPDATE scheduler_state SET consecutive_failures=consecutive_failures+1,last_error=?,"
                    "actual_state='error',phase='error',detail='本轮执行失败',heartbeat_at=?,updated_at=? WHERE id=1",
                    (error, timestamp, timestamp),
                )
            else:
                connection.execute(
                    "UPDATE scheduler_state SET phase='idle',batch_name='',detail=?,heartbeat_at=?,updated_at=? WHERE id=1",
                    ("本轮已中断", timestamp, timestamp),
                )
            connection.commit()
        level = "error" if status == "failed" else "info"
        self.event(f"cycle_{status}", f"生产周期 #{cycle_id} {status}", level=level, batch=batch, details={"error": error})

    def events(self, *, after_id: int = 0, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with closing(self.connect()) as connection:
            if after_id:
                rows = connection.execute(
                    "SELECT * FROM scheduler_events WHERE id>? ORDER BY id LIMIT ?",
                    (after_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM scheduler_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()[::-1]
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result

    def cycles(self, limit: int = 20) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM scheduler_cycles ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]
