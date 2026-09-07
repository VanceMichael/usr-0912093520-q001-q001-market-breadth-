#!/usr/bin/env python3
"""Shared structural contract for SQLite-backed delivery records."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


TASK_TYPES = {
    "0-1 代码生成", "Feature 迭代", "Bug 修复", "代码理解",
    "代码重构", "工程化", "代码测试",
}
DIFFICULTIES = {"简单", "中等", "困难", "地狱"}
REPRODUCIBILITY = {
    "无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境",
}
HARNESSES = {"Claude Code", "Codex CLI"}
OPERATING_SYSTEMS = {"MacOS/Linux", "Windows"}
SCORE_PREFIXES = ("delivery", "instruction", "planning", "reasoning", "execution")
SNAPSHOT_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/commit/[0-9a-fA-F]{40}$"
)

EXPORT_HEADERS = [
    "User Prompt", "SessionID", "TurnID/PromptID", "初始环境快照",
    "环境可复现等级", "Harness", "Harness 版本", "操作系统", "任务类型",
    "任务难度", "语言/框架", "交付完整性", "交付完整性 - 描述",
    "指令遵循", "指令遵循 - 描述", "任务规划", "任务规划 - 描述",
    "推理能力", "推理能力 - 描述", "执行能力", "执行能力 - 描述",
    "其他问题", "提交人", "提交时间", "父记录",
]
EXPORT_KEYS = [
    "user_prompt", "session_id", "turn_id", "initial_snapshot",
    "reproducibility", "harness", "harness_version", "operating_system",
    "task_type", "difficulty", "languages", "delivery_score",
    "delivery_description", "instruction_score", "instruction_description",
    "planning_score", "planning_description", "reasoning_score",
    "reasoning_description", "execution_score", "execution_description",
    "other_issues", "submitter", "submitted_at", "parent_record",
]
SCORE_KEYS = {f"{prefix}_score" for prefix in SCORE_PREFIXES}


def as_record(row: sqlite3.Row | dict) -> dict:
    record = dict(row)
    record["human_authored"] = bool(record.get("human_authored"))
    record["human_qc_approved"] = bool(record.get("human_qc_approved"))
    return record


def load_records(
    connection: sqlite3.Connection,
    batch: str | None = None,
    *,
    only_human_approved: bool = False,
) -> list[dict]:
    clauses: list[str] = []
    parameters: list[object] = []
    if batch:
        clauses.append("b.name = ?")
        parameters.append(batch)
    if only_human_approved:
        clauses.append("r.human_qc_approved = 1")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        "SELECT r.*, b.name AS batch_name, q.question_no, q.task_id "
        "FROM records r JOIN questions q ON q.id=r.question_id "
        "JOIN batches b ON b.id=q.batch_id" + where +
        " ORDER BY b.created_at, q.question_no, r.turn_no",
        parameters,
    ).fetchall()
    return [as_record(row) for row in rows]


def _parse_timestamp(
    value: object, field: str, record_id: str, errors: list[str]
) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{record_id}: {field} is required")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"{record_id}: {field} must be ISO 8601")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{record_id}: {field} must include timezone offset")
        return None
    return parsed


def validate_one(record: dict, require_human_qc: bool = False) -> tuple[list[str], list[str]]:
    record_id = str(record.get("record_id") or "<unknown>")
    errors: list[str] = []
    warnings: list[str] = []
    required_text = (
        "record_id", "user_prompt", "session_id", "turn_id", "initial_snapshot",
        "harness_version", "languages", "submitter",
    )
    for key in required_text:
        if not isinstance(record.get(key), str) or not record[key].strip():
            errors.append(f"{record_id}: {key} must be non-empty text")
    for key in ("other_issues", "parent_record"):
        if not isinstance(record.get(key), str):
            errors.append(f"{record_id}: {key} must be text")
    if not SNAPSHOT_RE.fullmatch(str(record.get("initial_snapshot", ""))):
        errors.append(f"{record_id}: invalid initial_snapshot")
    for key, allowed in (
        ("reproducibility", REPRODUCIBILITY), ("harness", HARNESSES),
        ("operating_system", OPERATING_SYSTEMS), ("task_type", TASK_TYPES),
        ("difficulty", DIFFICULTIES),
    ):
        if record.get(key) not in allowed:
            errors.append(f"{record_id}: invalid {key}")
    turn_no = record.get("turn_no")
    if isinstance(turn_no, bool) or not isinstance(turn_no, int) or not 1 <= turn_no <= 10:
        errors.append(f"{record_id}: turn_no must be integer 1-10")
    for prefix in SCORE_PREFIXES:
        score = record.get(f"{prefix}_score")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            errors.append(f"{record_id}: {prefix}_score must be integer 1-5")
        description = record.get(f"{prefix}_description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{record_id}: {prefix}_description is required")
    if record.get("human_authored") is not True:
        errors.append(f"{record_id}: human_authored must be true")
    if not isinstance(record.get("human_qc_approved"), bool):
        errors.append(f"{record_id}: human_qc_approved must be boolean")
    elif require_human_qc and record["human_qc_approved"] is not True:
        errors.append(f"{record_id}: human qualitative QC is not approved")

    completed = _parse_timestamp(
        record.get("turn_completed_at"), "turn_completed_at", record_id, errors
    )
    submitted = _parse_timestamp(
        record.get("submitted_at"), "submitted_at", record_id, errors
    )
    if completed and submitted:
        local = ZoneInfo("Asia/Shanghai")
        completed_local = completed.astimezone(local)
        submitted_local = submitted.astimezone(local)
        if completed_local.time() < time(20, 0):
            deadline = datetime.combine(completed_local.date(), time.max, local)
        else:
            deadline = datetime.combine(
                completed_local.date() + timedelta(days=1), time(14, 0), local
            )
        if submitted_local > deadline:
            errors.append(
                f"{record_id}: submitted after project deadline {deadline.isoformat()}"
            )
        if submitted_local < completed_local:
            errors.append(f"{record_id}: submitted_at precedes turn_completed_at")
    if record.get("difficulty") == "简单" and not record.get("parent_record", "").strip():
        errors.append(f"{record_id}: a first-turn record cannot be 简单")
    if len(str(record.get("user_prompt", ""))) > 32767:
        warnings.append(f"{record_id}: User Prompt exceeds Excel's per-cell text limit")
    return errors, warnings


def validate_records(
    records: list[dict], require_human_qc: bool = False
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for record in records:
        item_errors, item_warnings = validate_one(record, require_human_qc)
        errors.extend(item_errors)
        warnings.extend(item_warnings)

    ids = [str(record.get("record_id", "")) for record in records]
    pairs = [
        (str(record.get("session_id", "")), str(record.get("turn_id", "")))
        for record in records
    ]
    for duplicate, count in Counter(ids).items():
        if duplicate and count > 1:
            errors.append(f"duplicate record_id: {duplicate}")
    for duplicate, count in Counter(pairs).items():
        if all(duplicate) and count > 1:
            errors.append(
                f"duplicate SessionID + TurnID: {duplicate[0]} / {duplicate[1]}"
            )

    by_id = {str(record.get("record_id")): record for record in records}
    sessions: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        sessions[str(record.get("session_id", ""))].append(record)
    stable_fields = ("initial_snapshot", "harness", "harness_version", "operating_system")
    for session_id, items in sessions.items():
        if not session_id:
            continue
        if len(items) > 10:
            errors.append(f"{session_id}: {len(items)} submitted turns exceed the limit of 10")
        for field in stable_fields:
            values = {str(item.get(field, "")) for item in items}
            if len(values) > 1:
                errors.append(f"{session_id}: inconsistent {field} across turns")
        roots = [item for item in items if not str(item.get("parent_record", "")).strip()]
        if len(roots) != 1:
            errors.append(f"{session_id}: expected exactly one root record, found {len(roots)}")
        for item in items:
            parent_id = str(item.get("parent_record", "")).strip()
            if not parent_id:
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                errors.append(f"{item.get('record_id')}: parent_record {parent_id} not found")
            elif parent.get("session_id") != session_id:
                errors.append(
                    f"{item.get('record_id')}: parent_record belongs to another session"
                )
    return errors, warnings
