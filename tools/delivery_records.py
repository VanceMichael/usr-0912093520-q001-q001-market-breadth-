#!/usr/bin/env python3
"""Shared structural contract for SQLite-backed delivery records."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    PROJECT_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows Python installations may not ship the IANA timezone database.
    PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


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
NUMERIC_HARNESS_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")

EXPORT_HEADERS = [
    "User Prompt", "SessionID", "TurnID/PromptID", "当前对话轮次排序",
    "初始环境快照", "轨迹文件", "环境可复现等级", "Harness", "Harness 版本", "操作系统",
    "任务类型", "任务难度", "语言/框架", "交付完整性", "交付完整性 - 描述",
    "指令遵循", "指令遵循 - 描述", "任务规划", "任务规划 - 描述",
    "推理能力", "推理能力 - 描述", "执行能力", "执行能力 - 描述",
    "其他问题", "提交人", "提交时间", "父记录", "审核备注",
]
EXPORT_KEYS = [
    "user_prompt", "session_id", "turn_id", "turn_no", "initial_snapshot",
    "trajectory_file", "reproducibility", "harness", "harness_version",
    "operating_system", "task_type", "difficulty", "languages", "delivery_score",
    "delivery_description", "instruction_score", "instruction_description",
    "planning_score", "planning_description", "reasoning_score",
    "reasoning_description", "execution_score", "execution_description",
    "other_issues", "submitter", "submitted_at", "parent_record", "delivery_qc_note",
]
SCORE_KEYS = {f"{prefix}_score" for prefix in SCORE_PREFIXES}
DESCRIPTION_META_PATTERNS = (
    re.compile(r"(?i)(?:^|[^A-Za-z])AI\s*(?:分析|生成|评分|撰写|认为)"),
    re.compile(r"(?:由|作为|本)\s*(?:AI|Codex)\b", re.IGNORECASE),
    re.compile(r"Codex\s*(?:分析|生成|评分|撰写|认为)", re.IGNORECASE),
    re.compile(r"(?:自动生成|基于轨迹生成|根据轨迹生成|本评分由)"),
    re.compile(r"(?:评分|打分|评测|质检|评价者|审核人员|生成过程)"),
    re.compile(r"(?:轨迹|日志)(?:显示|表明|可见)"),
    re.compile(r"(?:模型|智能体|助手)(?:的)?(?:表现|回答|输出|生成过程)"),
)
DESCRIPTION_TEMPLATE_PATTERNS = (
    re.compile(r"(?:^|[\s；;。])(?:When|What|Impact)\s*[:：]", re.IGNORECASE),
    re.compile(r"^\s*(?:过程|产物)\s*[:：]"),
    re.compile(r"^\s*对(?:过程|产物)不满意的原因\s*[:：]"),
    re.compile(r"^\s*(?:经检查|通过检查|根据轨迹|从轨迹看|结合轨迹|综合来看|总体来看|本次任务中|本轮任务中|总体而言|综上所述|值得注意的是|需要指出的是)[，,:：]?"),
    re.compile(r"[→➡]"),
    re.compile(r"【(?:第几步|哪个环节|具体行为|什么后果|根因|正确做法|哪个文件|哪个功能)】"),
)


def as_record(row: sqlite3.Row | dict) -> dict:
    record = dict(row)
    record["human_authored"] = bool(record.get("human_authored"))
    record["human_qc_approved"] = bool(record.get("human_qc_approved"))
    record["delivery_qc_passed"] = bool(record.get("delivery_qc_passed"))
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


def _description_style_errors(description: str) -> list[str]:
    errors: list[str] = []
    if len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", description)) < 12:
        errors.append("必须使用自然、完整的中文书面语")
    if any(pattern.search(description) for pattern in DESCRIPTION_META_PATTERNS):
        errors.append("不得包含评价者自述、评分质检、模型表现或生成过程措辞")
    if any(pattern.search(description) for pattern in DESCRIPTION_TEMPLATE_PATTERNS):
        errors.append("不得使用固定标签、套话开头、箭头或占位模板")
    return errors


def validate_one(
    record: dict,
    require_human_qc: bool = False,
    require_delivery_qc: bool = False,
) -> tuple[list[str], list[str]]:
    record_id = str(record.get("record_id") or "<unknown>")
    errors: list[str] = []
    warnings: list[str] = []
    required_text = (
        "record_id", "user_prompt", "session_id", "turn_id", "initial_snapshot",
        "trajectory_file", "harness_version", "languages", "submitter",
    )
    for key in required_text:
        if not isinstance(record.get(key), str) or not record[key].strip():
            errors.append(f"{record_id}: {key} must be non-empty text")
    if record.get("human_authored") is False:
        submitter = str(record.get("submitter", "")).strip().casefold()
        if submitter in {"ai", "codex", "claude", "claude code", "自动", "系统"}:
            errors.append(f"{record_id}: submitter must be a real person's name")
    for key in ("other_issues", "parent_record"):
        if not isinstance(record.get(key), str):
            errors.append(f"{record_id}: {key} must be text")
    if not SNAPSHOT_RE.fullmatch(str(record.get("initial_snapshot", ""))):
        errors.append(f"{record_id}: invalid initial_snapshot")
    trajectory_file = str(record.get("trajectory_file", ""))
    if (
        not trajectory_file.endswith(".jsonl")
        or "/" in trajectory_file
        or "\\" in trajectory_file
    ):
        errors.append(f"{record_id}: trajectory_file must be a JSONL file name")
    for key, allowed in (
        ("reproducibility", REPRODUCIBILITY), ("harness", HARNESSES),
        ("operating_system", OPERATING_SYSTEMS), ("task_type", TASK_TYPES),
        ("difficulty", DIFFICULTIES),
    ):
        if record.get(key) not in allowed:
            errors.append(f"{record_id}: invalid {key}")
    if (
        record.get("harness") == "Claude Code"
        and not NUMERIC_HARNESS_VERSION_RE.fullmatch(
            str(record.get("harness_version", "")).strip()
        )
    ):
        errors.append(f"{record_id}: Claude Code harness_version must be numeric only")
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
        else:
            for style_error in _description_style_errors(description):
                errors.append(f"{record_id}: {prefix}_description {style_error}")
    descriptions = [
        re.sub(r"\s+", "", str(record.get(f"{prefix}_description", "")))
        for prefix in SCORE_PREFIXES
    ]
    repeated = [text for text, count in Counter(descriptions).items() if text and count > 1]
    if repeated:
        errors.append(f"{record_id}: score descriptions must not repeat verbatim")
    other_issues = record.get("other_issues")
    if isinstance(other_issues, str) and other_issues.strip():
        for style_error in _description_style_errors(other_issues):
            errors.append(f"{record_id}: other_issues {style_error}")
    if not isinstance(record.get("human_authored"), bool):
        errors.append(f"{record_id}: human_authored must be boolean")
    if not isinstance(record.get("human_qc_approved"), bool):
        errors.append(f"{record_id}: human_qc_approved must be boolean")
    elif require_human_qc and record["human_qc_approved"] is not True:
        errors.append(f"{record_id}: human qualitative QC is not approved")
    if not isinstance(record.get("delivery_qc_passed"), bool):
        errors.append(f"{record_id}: delivery_qc_passed must be boolean")
    elif require_delivery_qc and record["delivery_qc_passed"] is not True:
        errors.append(f"{record_id}: delivery QC is not passed")
    if record.get("delivery_qc_passed") is True:
        if record.get("delivery_qc_note") != "质检通过":
            errors.append(f"{record_id}: delivery_qc_note must be 质检通过")
        _parse_timestamp(
            record.get("delivery_qc_checked_at"),
            "delivery_qc_checked_at",
            record_id,
            errors,
        )
    changes = record.get("delivery_qc_changes")
    if not isinstance(changes, str):
        errors.append(f"{record_id}: delivery_qc_changes must be JSON text")
    else:
        try:
            parsed_changes = json.loads(changes)
        except ValueError:
            errors.append(f"{record_id}: delivery_qc_changes must be valid JSON")
        else:
            if not isinstance(parsed_changes, list):
                errors.append(f"{record_id}: delivery_qc_changes must contain an array")

    completed = _parse_timestamp(
        record.get("turn_completed_at"), "turn_completed_at", record_id, errors
    )
    submitted = _parse_timestamp(
        record.get("submitted_at"), "submitted_at", record_id, errors
    )
    if completed and submitted:
        completed_local = completed.astimezone(PROJECT_TIMEZONE)
        submitted_local = submitted.astimezone(PROJECT_TIMEZONE)
        if completed_local.time() < time(20, 0):
            deadline = datetime.combine(
                completed_local.date(), time.max, PROJECT_TIMEZONE
            )
        else:
            deadline = datetime.combine(
                completed_local.date() + timedelta(days=1),
                time(14, 0),
                PROJECT_TIMEZONE,
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
    records: list[dict],
    require_human_qc: bool = False,
    require_delivery_qc: bool = False,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for record in records:
        item_errors, item_warnings = validate_one(
            record, require_human_qc, require_delivery_qc
        )
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
    stable_fields = (
        "initial_snapshot", "trajectory_file", "harness", "harness_version",
        "operating_system",
    )
    for session_id, items in sessions.items():
        if not session_id:
            continue
        if len(items) > 10:
            errors.append(f"{session_id}: {len(items)} submitted turns exceed the limit of 10")
        valid_turn_numbers = [
            item.get("turn_no") for item in items
            if isinstance(item.get("turn_no"), int)
            and not isinstance(item.get("turn_no"), bool)
        ]
        if len(valid_turn_numbers) == len(items):
            ordered = sorted(items, key=lambda item: item["turn_no"])
            actual = [item["turn_no"] for item in ordered]
            expected = list(range(1, len(ordered) + 1))
            if actual != expected:
                errors.append(
                    f"{session_id}: turn_no must be consecutive from 1, found {actual}"
                )
            for index, item in enumerate(ordered):
                expected_parent = (
                    "" if index == 0
                    else str(ordered[index - 1].get("record_id", ""))
                )
                if str(item.get("parent_record", "")).strip() != expected_parent:
                    errors.append(
                        f"{item.get('record_id')}: parent_record must match the immediately preceding turn"
                    )
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
