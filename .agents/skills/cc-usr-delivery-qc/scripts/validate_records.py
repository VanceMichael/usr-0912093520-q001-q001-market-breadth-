#!/usr/bin/env python3
"""Validate, repair, and finalize SQLite delivery records for export."""

from __future__ import annotations

import argparse
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


def trajectory_integrity_issues(record: dict, source_row: sqlite3.Row) -> list[str]:
    """Reject target sessions that loaded instructions outside their workspace."""
    trajectory_name = str(record.get("trajectory_file") or "")
    if not trajectory_name.endswith(".jsonl") or "/" in trajectory_name or "\\" in trajectory_name:
        return []
    root_value = str(source_row["source_trajectory_root"] or "").strip()
    roots = [Path(root_value).resolve()] if root_value else [Path(str(source_row["batch_folder"])).resolve()]
    candidates: list[tuple[Path, list[dict]]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if path.name != trajectory_name and not path.name.endswith("_" + trajectory_name):
                continue
            if not path.is_file():
                continue
            try:
                events = _trajectory_events(path)
            except (OSError, ValueError):
                continue
            if any(
                _event_session_id(event) == str(record.get("session_id"))
                and _event_user_prompt(event).strip() == str(record.get("user_prompt", "")).strip()
                for event in events
            ):
                candidates.append((path, events))
    if not candidates:
        return []

    record_id = str(record.get("record_id") or "<unknown>")
    folder = Path(str(source_row["question_folder"])).resolve()
    alternate = str(source_row["container_cwd"] or "").strip()
    allowed = (folder,) + ((Path(alternate),) if alternate.startswith("/") else ())
    issues: list[str] = []
    seen: set[str] = set()
    for _path, events in candidates:
        for event in events:
            if _event_session_id(event) not in {"", str(record.get("session_id"))}:
                continue
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if (
                        isinstance(block, dict) and block.get("type") == "tool_use"
                        and str(block.get("name") or "").casefold() in {"skill", "slashcommand"}
                    ):
                        key = f"{record_id}: trajectory invoked a Skill tool"
                        if key not in seen:
                            seen.add(key)
                            issues.append(key)
            for text in _event_strings(event):
                match = SKILL_PATH_RE.search(text)
                if not match:
                    continue
                marker_start = match.start()
                prefix_start = marker_start
                while prefix_start > 0 and text[prefix_start - 1] not in " \t\r\n'\"`()[]{}=:":
                    prefix_start -= 1
                referenced = text[prefix_start:]
                if _path_is_within(referenced, allowed, folder):
                    continue
                key = (
                    f"{record_id}: trajectory read an external skill path "
                    f"({text[marker_start:marker_start + 120]})"
                )
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
        "SELECT r.record_id, r.turn_no, r.user_prompt, r.session_id, r.trajectory_file, "
        "r.initial_snapshot, r.reproducibility, r.harness, r.harness_version, "
        "r.task_type, r.difficulty, r.languages, q.prompt AS source_prompt, "
        "q.initial_snapshot AS source_snapshot, q.reproducibility AS source_reproducibility, "
        "q.task_type AS source_task_type, q.difficulty AS source_difficulty, "
        "q.languages AS source_languages, "
        "(SELECT x.session_id FROM runs x WHERE x.question_id=q.id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_session_id, "
        "(SELECT x.harness FROM runs x WHERE x.question_id=q.id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_harness, "
        "(SELECT x.harness_version FROM runs x WHERE x.question_id=q.id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_harness_version, "
        "(SELECT x.trajectory_root FROM runs x WHERE x.question_id=q.id "
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_trajectory_root, "
        "(SELECT x.container_cwd FROM runs x WHERE x.question_id=q.id "
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
