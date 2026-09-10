#!/usr/bin/env python3
"""Validate, repair, and finalize SQLite delivery records for export."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import ntpath
import posixpath
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect, parse_selection, question_rows  # noqa: E402
from tools.delivery_records import (  # noqa: E402
    EXPORT_KEYS,
    load_records,
    validate_records,
)


FIXABLE_FIELDS = frozenset(EXPORT_KEYS) - {"turn_no", "delivery_qc_note"}
SKILL_PATH_RE = re.compile(
    r"(?i)(?:\.agents[/\\]+skills[/\\]+|\.claude[/\\]+skills[/\\]+|"
    r"\.codex[/\\]+skills[/\\]+|[/\\]skills[/\\][^\s'\"`]+[/\\]SKILL\.md)"
)
CONFIG_PATH_RE = re.compile(
    r"(?i)(?:CLAUDE\.md|AGENTS\.md|\.claude[/\\]+settings(?:\.local)?\.json|"
    r"\.codex[/\\]+config\.toml)"
)


def _event_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _event_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _event_strings(nested)


def _event_session_id(event: dict) -> str:
    return str(event.get("sessionId") or event.get("session_id") or "")


def _event_user_prompt(event: dict) -> str:
    if event.get("type") != "user" or event.get("isMeta") is True:
        return ""
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _event_prompt_id(event: dict) -> str:
    return str(event.get("promptId") or event.get("prompt_id") or event.get("uuid") or "")


def _assistant_has_text(event: dict) -> bool:
    if event.get("type") != "assistant":
        return False
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and str(block.get("text") or "").strip()
            for block in content
        )
    return False


def _path_is_within(path_text: str, allowed: tuple[Path, ...], workspace: Path) -> bool:
    normalized = path_text.replace("\\", "/")
    if "://" in normalized:
        return False
    if re.match(r"^[A-Za-z]:/", normalized):
        normalized = ntpath.normpath(normalized).replace("\\", "/")
    elif normalized.startswith("/"):
        normalized = posixpath.normpath(normalized)
    if not normalized.startswith("/") and not re.match(r"^[A-Za-z]:/", normalized):
        try:
            candidate = (workspace / normalized).resolve()
            return any(candidate == root or root in candidate.parents for root in allowed)
        except OSError:
            return False
    for root in allowed:
        root_text = str(root).replace("\\", "/").rstrip("/")
        if (
            normalized.casefold() == root_text.casefold()
            or normalized.casefold().startswith(root_text.casefold() + "/")
        ):
            return True
    return False


def _trajectory_events(path: Path) -> list[dict]:
    events: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if isinstance(event, dict):
                events.append(event)
    return events


def _trajectory_root(source_row: sqlite3.Row) -> tuple[Path, bool]:
    configured = str(source_row["source_trajectory_root"] or "").strip()
    if configured:
        return Path(configured).resolve(), True
    batch_run_id = str(source_row["source_batch_run_id"] or "").strip()
    task_id = str(source_row["task_id"] or "").strip()
    batch_folder = Path(str(source_row["batch_folder"])).resolve()
    if batch_run_id and task_id:
        isolated = batch_folder / ".runs" / batch_run_id / task_id / "claude-home" / "projects"
        if isolated.is_dir():
            return isolated.resolve(), True
    return batch_folder, False


def _named_trajectory_candidates(root: Path, name: str, recursive: bool) -> list[Path]:
    if not root.is_dir():
        return []
    paths = root.rglob("*.jsonl") if recursive else root.glob("*.jsonl")
    return sorted(
        path.resolve() for path in paths
        if path.is_file() and (path.name == name or path.name.endswith("_" + name))
    )


def trajectory_integrity_issues(record: dict, source_row: sqlite3.Row) -> list[str]:
    """Require one exact, complete trajectory from the record's successful run."""
    trajectory_name = str(record.get("trajectory_file") or "")
    if not trajectory_name.endswith(".jsonl") or "/" in trajectory_name or "\\" in trajectory_name:
        return []
    record_id = str(record.get("record_id") or "<unknown>")
    root, recursive = _trajectory_root(source_row)
    paths = _named_trajectory_candidates(root, trajectory_name, recursive)
    if not paths:
        return [f"{record_id}: original trajectory not found in authoritative root: {root}"]
    if len(paths) > 1:
        return [
            f"{record_id}: trajectory file is ambiguous under {root}: "
            + ", ".join(str(path) for path in paths)
        ]
    path = paths[0]
    try:
        events = _trajectory_events(path)
    except (OSError, ValueError) as exc:
        return [f"{record_id}: original trajectory is unreadable or invalid: {exc}"]
    if not events:
        return [f"{record_id}: original trajectory is empty: {path}"]

    session_id = str(record.get("session_id") or "")
    turn_id = str(record.get("turn_id") or "")
    user_prompt = str(record.get("user_prompt") or "")
    session_events = [event for event in events if _event_session_id(event) in {"", session_id}]
    issues: list[str] = []
    foreign_sessions = sorted({
        _event_session_id(event) for event in events
        if _event_session_id(event) not in {"", session_id}
    })
    if foreign_sessions:
        issues.append(
            f"{record_id}: trajectory contains events from other SessionIDs: "
            + ", ".join(foreign_sessions)
        )
    real_turns = [
        (index, event) for index, event in enumerate(events)
        if _event_session_id(event) in {"", session_id}
        and _event_user_prompt(event) and _event_prompt_id(event)
    ]
    turn_no = record.get("turn_no")
    if not isinstance(turn_no, int) or isinstance(turn_no, bool) or not 1 <= turn_no <= len(real_turns):
        issues.append(
            f"{record_id}: turn_no {turn_no} does not identify a trajectory user turn"
        )
        return issues
    matched_index, matched_event = real_turns[turn_no - 1]
    raw_prompt = _event_user_prompt(matched_event)
    raw_prompt_id = _event_prompt_id(matched_event)
    is_continuation = raw_prompt.strip() == "继续"
    exact_matches = [
        index for index, event in enumerate(events)
        if _event_session_id(event) == session_id
        and _event_prompt_id(event) == turn_id
        and _event_user_prompt(event) == user_prompt
    ]
    if bool(record.get("is_continuation")) != is_continuation:
        issues.append(f"{record_id}: continuation flag does not match the original trajectory")
    if str(record.get("raw_user_prompt") or "") != raw_prompt:
        issues.append(f"{record_id}: raw_user_prompt does not match the original trajectory")
    if str(record.get("raw_turn_id") or "") != raw_prompt_id:
        issues.append(f"{record_id}: raw_turn_id does not match the original trajectory")
    if is_continuation:
        if user_prompt.strip() == "继续" or raw_prompt_id == turn_id:
            issues.append(
                f"{record_id}: continuation delivery fields must reuse the interrupted task's User Prompt and PromptID"
            )
        origin_event: dict | None = None
        for _index, earlier_event in reversed(real_turns[:turn_no - 1]):
            if _event_user_prompt(earlier_event).strip() != "继续":
                origin_event = earlier_event
                break
        if (
            origin_event is None
            or _event_user_prompt(origin_event) != user_prompt
            or _event_prompt_id(origin_event) != turn_id
            or len(exact_matches) != 1
            or exact_matches[0] >= matched_index
        ):
            issues.append(
                f"{record_id}: continuation must reuse the nearest interrupted task's "
                f"User Prompt and PromptID from {path}"
            )
        consecutive_count = 1
        for _index, earlier_event in reversed(real_turns[:turn_no - 1]):
            if _event_user_prompt(earlier_event).strip() != "继续":
                break
            consecutive_count += 1
        if int(record.get("continuation_count") or 0) != consecutive_count:
            issues.append(
                f"{record_id}: continuation_count must be {consecutive_count} for this trajectory"
            )
        other_issues = str(record.get("other_issues") or "")
        count_names = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
        if consecutive_count > 1 and not re.search(
            rf"(?:{consecutive_count}|{count_names.get(consecutive_count, '')})\s*次\s*继续",
            other_issues,
        ):
            issues.append(
                f"{record_id}: other_issues must state that continuation occurred {consecutive_count} times"
            )
    elif (
        len(exact_matches) != 1
        or exact_matches[0] != matched_index
        or raw_prompt_id != turn_id
        or raw_prompt != user_prompt
    ):
        issues.append(
            f"{record_id}: trajectory turn must exactly match SessionID + PromptID + User Prompt"
        )

    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    call_positions: dict[str, list[int]] = {}
    result_positions: dict[str, list[int]] = {}
    for event_index, event in enumerate(session_events):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                identifier = str(block.get("id") or "")
                if identifier:
                    calls[identifier] += 1
                    call_positions.setdefault(identifier, []).append(event_index)
                else:
                    issues.append(f"{record_id}: trajectory contains a tool call without an id")
            elif block.get("type") == "tool_result":
                identifier = str(block.get("tool_use_id") or "")
                if identifier:
                    results[identifier] += 1
                    result_positions.setdefault(identifier, []).append(event_index)
                else:
                    issues.append(f"{record_id}: trajectory contains a tool result without tool_use_id")
    missing_results = sorted((calls - results).elements())
    orphan_results = sorted((results - calls).elements())
    if missing_results:
        issues.append(f"{record_id}: trajectory has tool calls without matching results: " + ", ".join(missing_results[:10]))
    if orphan_results:
        issues.append(f"{record_id}: trajectory has tool results without matching calls: " + ", ".join(orphan_results[:10]))
    duplicate_calls = sorted(identifier for identifier, count in calls.items() if count > 1)
    duplicate_results = sorted(identifier for identifier, count in results.items() if count > 1)
    if duplicate_calls or duplicate_results:
        issues.append(
            f"{record_id}: trajectory reuses tool identifiers; calls={duplicate_calls[:10]}, results={duplicate_results[:10]}"
        )
    out_of_order = sorted(
        identifier for identifier in calls.keys() & results.keys()
        if min(result_positions[identifier]) <= min(call_positions[identifier])
    )
    if out_of_order:
        issues.append(f"{record_id}: trajectory has tool results before their calls: " + ", ".join(out_of_order[:10]))

    next_turns = [(index, event) for index, event in real_turns if index > matched_index]
    turn_end = next_turns[0][0] if next_turns else len(events)
    turn_assistant_events = [
        event for event in events[matched_index + 1:turn_end]
        if event.get("type") == "assistant"
    ]
    followed_by_continuation = bool(
        next_turns and _event_user_prompt(next_turns[0][1]).strip() == "继续"
    )
    if not followed_by_continuation and (
        not turn_assistant_events or not _assistant_has_text(turn_assistant_events[-1])
    ):
        issues.append(f"{record_id}: trajectory turn has no final assistant text response")

    folder = Path(str(source_row["question_folder"])).resolve()
    alternate = str(source_row["container_cwd"] or "").strip()
    allowed = (folder,) + ((Path(alternate).resolve(),) if alternate.startswith("/") else ())
    seen: set[str] = set()
    for event in session_events:
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if str(block.get("name") or "").casefold() in {"skill", "slashcommand"}:
                key = f"{record_id}: trajectory invoked a Skill tool"
                if key not in seen:
                    seen.add(key)
                    issues.append(key)
            for text in _event_strings(block.get("input")):
                match = SKILL_PATH_RE.search(text) or CONFIG_PATH_RE.search(text)
                if not match:
                    continue
                marker_start = match.start()
                prefix_start = marker_start
                while prefix_start > 0 and text[prefix_start - 1] not in " \t\r\n'\"`()[]{}=:":
                    prefix_start -= 1
                referenced = text[prefix_start:]
                if _path_is_within(referenced, allowed, folder):
                    continue
                kind = "skill" if SKILL_PATH_RE.search(text) else "instruction/config"
                key = f"{record_id}: trajectory read an external {kind} path ({text[marker_start:marker_start + 120]})"
                if key not in seen:
                    seen.add(key)
                    issues.append(key)
    return issues


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_report(path: Path | None, report: dict) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def build_report(
    connection: sqlite3.Connection,
    batch: str,
    records: list[dict],
    selected_numbers: set[int] | None = None,
) -> dict:
    errors, warnings = validate_records(records)
    missing_query = (
        "SELECT q.task_id FROM questions q JOIN batches b ON b.id=q.batch_id "
        "WHERE b.name=? AND EXISTS(SELECT 1 FROM runs x WHERE x.question_id=q.id) "
        "AND NOT EXISTS(SELECT 1 FROM records r WHERE r.question_id=q.id)"
    )
    missing_parameters: list[object] = [batch]
    if selected_numbers:
        missing_query += " AND q.question_no IN (" + ",".join("?" for _ in selected_numbers) + ")"
        missing_parameters.extend(sorted(selected_numbers))
    missing_questions = connection.execute(
        missing_query + " ORDER BY q.question_no", missing_parameters,
    ).fetchall()
    errors.extend(
        f"{row['task_id']}: launched question has no delivery record"
        for row in missing_questions
    )
    source_query = (
        "SELECT r.record_id, r.turn_no, r.user_prompt, r.session_id, r.turn_id, r.trajectory_file, "
        "r.raw_user_prompt, r.raw_turn_id, r.is_continuation, r.continuation_count, r.other_issues, "
        "r.initial_snapshot, r.reproducibility, r.harness, r.harness_version, "
        "r.task_type, r.difficulty, r.languages, q.prompt AS source_prompt, "
        "q.initial_snapshot AS source_snapshot, q.reproducibility AS source_reproducibility, "
        "q.task_type AS source_task_type, q.difficulty AS source_difficulty, "
        "q.languages AS source_languages, q.task_id AS task_id, "
        "(SELECT x.id FROM runs x WHERE x.question_id=q.id AND x.status='succeeded' "
        " AND x.session_id=r.session_id ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_run_id, "
        "(SELECT x.session_id FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_session_id, "
        "(SELECT x.batch_run_id FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_batch_run_id, "
        "(SELECT x.harness FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_harness, "
        "(SELECT x.harness_version FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_harness_version, "
        "(SELECT x.trajectory_root FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_trajectory_root, "
        "(SELECT x.container_cwd FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS container_cwd, "
        "q.folder_path AS question_folder, b.folder_path AS batch_folder "
        "FROM records r JOIN questions q ON q.id=r.question_id "
        "JOIN batches b ON b.id=q.batch_id WHERE b.name=?"
    )
    source_parameters: list[object] = [batch]
    if selected_numbers:
        source_query += " AND q.question_no IN (" + ",".join("?" for _ in selected_numbers) + ")"
        source_parameters.extend(sorted(selected_numbers))
    source_rows = connection.execute(source_query, source_parameters).fetchall()
    for row in source_rows:
        if row["source_run_id"] is None:
            errors.append(
                f"{row['record_id']}: session_id is not linked to a succeeded registered run"
            )
            continue
        common_fields = {
            "initial_snapshot": "source_snapshot",
            "reproducibility": "source_reproducibility",
            "harness": "source_harness",
            "harness_version": "source_harness_version",
        }
        for field, source_field in common_fields.items():
            if row[field] != row[source_field]:
                errors.append(
                    f"{row['record_id']}: {field} does not match question/run metadata"
                )
        if row["source_session_id"] and row["session_id"] != row["source_session_id"]:
            errors.append(
                f"{row['record_id']}: session_id does not match the registered run"
            )
        if row["turn_no"] == 1:
            first_turn_fields = {
                "user_prompt": "source_prompt",
                "task_type": "source_task_type",
                "difficulty": "source_difficulty",
                "languages": "source_languages",
            }
            for field, source_field in first_turn_fields.items():
                if row[field] != row[source_field]:
                    errors.append(
                        f"{row['record_id']}: first-turn {field} does not match the question"
                    )
        errors.extend(trajectory_integrity_issues(dict(row), row))
    return {
        "checked_records": len(records),
        "passed": not errors and not warnings,
        "errors": errors,
        "warnings": warnings,
        "scope": "Required export fields, identifiers, turn order, values, descriptions, sessions, and deadlines.",
    }


def load_fixes(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read fixes JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("fixes JSON must contain a records array")
    if not payload["records"]:
        raise ValueError("fixes JSON records array is empty")
    return payload["records"]


def apply_fixes(
    connection: sqlite3.Connection,
    batch: str,
    fixes_path: Path,
    selected_numbers: set[int] | None = None,
) -> tuple[int, dict]:
    before_records = load_records(connection, batch)
    if selected_numbers:
        before_records = [
            record for record in before_records
            if int(record["question_no"]) in selected_numbers
        ]
    if not before_records:
        raise ValueError(f"batch has no delivery records: {batch}")
    before_report = build_report(connection, batch, before_records, selected_numbers)
    by_id = {record["record_id"]: record for record in before_records}
    timestamp = now()
    changed_records = 0
    seen: set[str] = set()

    for item in load_fixes(fixes_path):
        if not isinstance(item, dict):
            raise ValueError("each fixes entry must be an object")
        record_id = item.get("record_id")
        reason = item.get("reason")
        changes = item.get("changes")
        if not isinstance(record_id, str) or record_id not in by_id:
            raise ValueError(f"unknown record_id in fixes: {record_id}")
        if record_id in seen:
            raise ValueError(f"duplicate record_id in fixes: {record_id}")
        seen.add(record_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{record_id}: every correction requires a concrete reason")
        if not isinstance(changes, dict) or not changes:
            raise ValueError(f"{record_id}: changes must be a non-empty object")
        invalid = sorted(set(changes) - FIXABLE_FIELDS)
        if invalid:
            raise ValueError(
                f"{record_id}: fields cannot be changed by delivery QC: {', '.join(invalid)}"
            )
        current = by_id[record_id]
        effective = {
            key: value for key, value in changes.items() if current.get(key) != value
        }
        if not effective:
            continue
        try:
            history = json.loads(str(current.get("delivery_qc_changes") or "[]"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{record_id}: invalid delivery_qc_changes audit log") from exc
        if not isinstance(history, list):
            raise ValueError(f"{record_id}: delivery_qc_changes must contain an array")
        history.append({
            "checked_at": timestamp,
            "reason": reason.strip(),
            "changes": {
                key: {"before": current.get(key), "after": value}
                for key, value in effective.items()
            },
        })
        assignments = [f"{key}=?" for key in effective]
        assignments.extend([
            "delivery_qc_passed=0",
            "delivery_qc_note=''",
            "delivery_qc_checked_at=''",
            "delivery_qc_changes=?",
        ])
        connection.execute(
            f"UPDATE records SET {', '.join(assignments)} WHERE record_id=?",
            [*effective.values(), json.dumps(history, ensure_ascii=False), record_id],
        )
        changed_records += 1

    after_records = load_records(connection, batch)
    if selected_numbers:
        after_records = [
            record for record in after_records
            if int(record["question_no"]) in selected_numbers
        ]
    after_report = build_report(connection, batch, after_records, selected_numbers)
    new_errors = sorted(set(after_report["errors"]) - set(before_report["errors"]))
    new_warnings = sorted(set(after_report["warnings"]) - set(before_report["warnings"]))
    if new_errors or new_warnings:
        connection.rollback()
        raise ValueError(
            "corrections introduced new validation issues: "
            + "; ".join(new_errors + new_warnings)
        )
    connection.commit()
    return changed_records, after_report


def finalize(
    connection: sqlite3.Connection,
    batch: str,
    selected_numbers: set[int] | None = None,
) -> tuple[int, dict]:
    records = load_records(connection, batch)
    if selected_numbers:
        records = [
            record for record in records
            if int(record["question_no"]) in selected_numbers
        ]
    if not records:
        raise ValueError(f"batch has no delivery records: {batch}")
    report = build_report(connection, batch, records, selected_numbers)
    if not report["passed"]:
        return 0, report
    timestamp = now()
    question_marks = ", ".join("?" for _ in records)
    connection.execute(
        f"UPDATE records SET delivery_qc_passed=1, delivery_qc_note='质检通过', "
        f"delivery_qc_checked_at=? WHERE record_id IN ({question_marks})",
        [timestamp, *(record["record_id"] for record in records)],
    )
    connection.commit()
    return len(records), report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--select", help="question numbers, for example 1,3-5")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--fixes", type=Path)
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.fixes and args.finalize:
        parser.error("--fixes and --finalize cannot be used together")

    connection: sqlite3.Connection | None = None
    try:
        connection = connect(args.db.resolve())
        selected_numbers: set[int] | None = None
        if args.select:
            selected_numbers = {
                int(row["question_no"])
                for row in parse_selection(args.select, question_rows(connection, args.batch))
            }
        if args.fixes:
            changed, report = apply_fixes(
                connection, args.batch, args.fixes.resolve(), selected_numbers
            )
            write_report(args.report.resolve() if args.report else None, report)
            print(f"已修正 {changed} 条交付记录；请继续处理剩余问题并重新质检。")
            return 0 if report["passed"] else 1
        if args.finalize:
            passed_count, report = finalize(connection, args.batch, selected_numbers)
            write_report(args.report.resolve() if args.report else None, report)
            if not report["passed"]:
                print("仍有不符合规范的字段，不能标记通过。", file=sys.stderr)
                return 1
            print(f"批次 {args.batch}：质检通过（{passed_count} 条记录）")
            return 0

        records = load_records(connection, args.batch)
        if selected_numbers:
            records = [
                record for record in records
                if int(record["question_no"]) in selected_numbers
            ]
        if not records:
            raise ValueError(f"batch has no delivery records: {args.batch}")
        report = build_report(connection, args.batch, records, selected_numbers)
        write_report(args.report.resolve() if args.report else None, report)
        return 0 if report["passed"] else 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        if connection is not None:
            connection.rollback()
        print(f"Delivery QC failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
