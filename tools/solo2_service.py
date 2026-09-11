#!/usr/bin/env python3
"""Idempotent SOLO2 submission service shared by the console and scheduler."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
import uuid

from tools.batch_pipeline import connect, now
from tools.delivery_records import as_record, validate_one
from tools.solo2_client import Solo2Client, Solo2Error, map_record_to_schema


ClientFactory = Callable[[Path, float, str], Solo2Client]


def _client(cookie_file: Path, timeout: float, origin: str) -> Solo2Client:
    return Solo2Client(cookie_file, timeout=timeout, origin=origin)


def _trajectory_path(connection: sqlite3.Connection, record: sqlite3.Row) -> Path:
    root_row = connection.execute(
        "SELECT trajectory_root FROM runs WHERE question_id=? AND session_id=? "
        "AND status='succeeded' ORDER BY launched_at DESC,id DESC LIMIT 1",
        (record["question_id"], record["session_id"]),
    ).fetchone()
    roots: list[tuple[Path, bool]] = []
    if root_row and str(root_row["trajectory_root"] or "").strip():
        roots.append((Path(str(root_row["trajectory_root"])).resolve(), True))
    roots.append((Path(str(record["batch_folder"])).resolve(), False))
    matches: list[Path] = []
    for root, recursive in roots:
        if not root.is_dir():
            continue
        candidates = root.rglob("*.jsonl") if recursive else root.glob("*.jsonl")
        matches.extend(
            path.resolve() for path in candidates
            if path.is_file() and (
                path.name == record["trajectory_file"]
                or path.name.endswith("_" + str(record["trajectory_file"]))
            )
        )
        if matches:
            break
    unique = sorted(set(matches))
    if len(unique) != 1:
        raise Solo2Error(
            "找不到唯一的原始轨迹文件" if not unique else "原始轨迹文件存在多个候选"
        )
    return unique[0]


def _records(
    connection: sqlite3.Connection,
    *,
    batch: str | None = None,
    numbers: set[int] | None = None,
    record_ids: set[str] | None = None,
    pending_only: bool = False,
) -> list[sqlite3.Row]:
    clauses = [
        "r.delivery_qc_passed=1", "r.delivery_qc_note='质检通过'",
        "r.evidence_gate_passed=1", "r.history_gate_passed=1",
        "r.human_qc_approved=1", "r.review_method IN ('human','codex')",
    ]
    parameters: list[object] = []
    if batch:
        clauses.append("b.name=?")
        parameters.append(batch)
    if numbers:
        marks = ",".join("?" for _ in numbers)
        clauses.append(f"q.question_no IN ({marks})")
        parameters.extend(sorted(numbers))
    if record_ids:
        marks = ",".join("?" for _ in record_ids)
        clauses.append(f"r.record_id IN ({marks})")
        parameters.extend(sorted(record_ids))
    if pending_only:
        clauses.append(
            "COALESCE(s.status,'') NOT IN ('succeeded','auth_blocked','schema_blocked') "
            "AND (COALESCE(s.status,'')!='submitting' OR "
            "julianday(COALESCE(s.lease_expires_at,''))<julianday('now'))"
        )
    query = (
        "SELECT r.*,q.question_no,q.task_id,b.name AS batch_name,b.folder_path AS batch_folder,"
        "s.status AS solo2_status,s.attempt_count,s.last_error,s.remote_submission_id "
        "FROM records r JOIN questions q ON q.id=r.question_id "
        "JOIN batches b ON b.id=q.batch_id "
        "LEFT JOIN solo2_submissions s ON s.record_id=r.record_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY b.created_at,q.question_no,r.turn_no"
    )
    return connection.execute(query, parameters).fetchall()


def submission_overview(
    database: Path, batch: str | None = None,
) -> dict[str, object]:
    with connect(database) as connection:
        rows = _records(connection, batch=batch)
        history = [dict(row) for row in connection.execute(
            "SELECT s.record_id,s.status,s.remote_submission_id,s.remote_status,"
            "s.attempt_count,s.last_error,s.submitted_at,s.updated_at,q.task_id,b.name AS batch_name "
            "FROM solo2_submissions s JOIN questions q ON q.id=s.question_id "
            "JOIN batches b ON b.id=q.batch_id "
            + ("WHERE b.name=? " if batch else "")
            + "ORDER BY s.updated_at DESC,s.id DESC LIMIT 100",
            (batch,) if batch else (),
        ).fetchall()]
    succeeded = sum(1 for row in rows if row["solo2_status"] == "succeeded")
    blocked = sum(1 for row in rows if row["solo2_status"] in {"auth_blocked", "schema_blocked"})
    return {
        "eligible": len(rows),
        "submitted": succeeded,
        "pending": len(rows) - succeeded,
        "blocked": blocked,
        "history": history,
    }


def _claim_record(
    connection: sqlite3.Connection, record: sqlite3.Row, max_attempts: int,
    *, allow_blocked: bool,
) -> bool:
    connection.execute("BEGIN IMMEDIATE")
    current = connection.execute(
        "SELECT status,attempt_count,updated_at,lease_expires_at FROM solo2_submissions WHERE record_id=?",
        (record["record_id"],),
    ).fetchone()
    active_submission = bool(
        current and current["status"] == "submitting"
        and str(current["lease_expires_at"] or "")
        and not retry_delay_elapsed(str(current["lease_expires_at"]), 0, delay=0)
    )
    blocked_statuses = {"succeeded"}
    if not allow_blocked:
        blocked_statuses.update({"auth_blocked", "schema_blocked"})
    if active_submission or (current and current["status"] in blocked_statuses):
        connection.rollback()
        return False
    attempts = int(current["attempt_count"] or 0) if current else 0
    if attempts >= max_attempts and not allow_blocked:
        connection.rollback()
        return False
    timestamp = now()
    lease_owner = uuid.uuid4().hex
    lease_expires = (datetime.now().astimezone() + timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    connection.execute(
        "INSERT INTO solo2_submissions(record_id,question_id,status,attempt_count,updated_at,"
        "lease_owner,lease_expires_at) VALUES(?,?, 'submitting',1,?,?,?) "
        "ON CONFLICT(record_id) DO UPDATE SET status='submitting',"
        "attempt_count=solo2_submissions.attempt_count+1,last_error='',updated_at=excluded.updated_at,"
        "lease_owner=excluded.lease_owner,lease_expires_at=excluded.lease_expires_at",
        (record["record_id"], record["question_id"], timestamp, lease_owner, lease_expires),
    )
    connection.commit()
    return True


def _failure_status(error: Solo2Error) -> str:
    if error.status in {401, 403}:
        return "auth_blocked"
    if error.status == 429 or (error.status is not None and error.status >= 500):
        return "retry_wait"
    if error.status is None and "无法连接" in str(error):
        return "retry_wait"
    return "schema_blocked"


def submit_records(
    database: Path,
    cookie_file: Path,
    origin: str,
    *,
    batch: str | None = None,
    numbers: set[int] | None = None,
    record_ids: set[str] | None = None,
    limit: int | None = None,
    max_attempts: int = 3,
    timeout: float = 30,
    client_factory: ClientFactory = _client,
    manual: bool = True,
) -> dict[str, object]:
    """Submit eligible records once; retries are scheduled by later calls."""
    with connect(database) as connection:
        rows = _records(
            connection, batch=batch, numbers=numbers, record_ids=record_ids,
            pending_only=not manual,
        )
    if not rows:
        return {"submitted": 0, "skipped": 0, "failed": 0, "results": []}
    client = client_factory(cookie_file, timeout, origin)
    schema: dict | None = None
    fingerprint = ""

    submitted = skipped = failed = 0
    claimed = 0
    results: list[dict[str, object]] = []
    for row in rows:
        if limit is not None and claimed >= max(1, limit):
            break
        with connect(database) as connection:
            if not _claim_record(connection, row, max_attempts, allow_blocked=manual):
                skipped += 1
                continue
        claimed += 1
        try:
            if schema is None:
                schema = client.form_schema()
                fingerprint = str(schema.get("fingerprint") or "")
                if not fingerprint:
                    raise Solo2Error("平台表单缺少 schema_fingerprint")
            record = as_record(row)
            errors, warnings = validate_one(
                record,
                require_human_qc=True,
                require_delivery_qc=True,
                require_quality_gates=True,
            )
            if errors or warnings:
                raise Solo2Error("本地交付校验未通过：" + "；".join(errors + warnings))
            with connect(database) as connection:
                trajectory = _trajectory_path(connection, row)
            payload, attachment_keys = map_record_to_schema(record, schema)
            attachment = client.upload(trajectory) if attachment_keys else None
            if attachment:
                for key in attachment_keys:
                    payload[key] = [attachment]
            remote = client.create_submission(payload, fingerprint)
            remote_id = str(remote.get("id"))
            remote_status = str(remote.get("status") or "submitted")
            timestamp = now()
            with connect(database) as connection:
                connection.execute(
                    "UPDATE solo2_submissions SET status='succeeded',remote_submission_id=?,"
                    "remote_status=?,last_error='',submitted_at=?,updated_at=?,"
                    "lease_owner='',lease_expires_at='' WHERE record_id=?",
                    (remote_id, remote_status, timestamp, timestamp, row["record_id"]),
                )
                connection.commit()
            submitted += 1
            results.append({"record_id": row["record_id"], "ok": True, "remote_id": remote_id})
        except Solo2Error as exc:
            failed += 1
            status = _failure_status(exc)
            with connect(database) as connection:
                connection.execute(
                    "UPDATE solo2_submissions SET status=?,last_error=?,updated_at=? WHERE record_id=?",
                    (status, str(exc)[:1000], now(), row["record_id"]),
                )
                connection.execute(
                    "UPDATE solo2_submissions SET lease_owner='',lease_expires_at='' WHERE record_id=?",
                    (row["record_id"],),
                )
                connection.commit()
            results.append({"record_id": row["record_id"], "ok": False, "error": str(exc), "status": status})
            if status == "auth_blocked":
                break
    return {"submitted": submitted, "skipped": skipped, "failed": failed, "results": results}


def retry_delay_elapsed(
    updated_at: str, attempt_count: int, *, delay: int | None = None,
) -> bool:
    try:
        updated = datetime.fromisoformat(updated_at)
    except (TypeError, ValueError):
        return True
    wait_seconds = (
        min(900, 30 * (2 ** max(0, attempt_count - 1))) if delay is None else delay
    )
    return datetime.now().astimezone() >= updated + timedelta(seconds=wait_seconds)
