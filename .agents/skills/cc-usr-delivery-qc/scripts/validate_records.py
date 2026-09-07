#!/usr/bin/env python3
"""Validate, repair, and finalize SQLite delivery records for export."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402
from tools.delivery_records import (  # noqa: E402
    EXPORT_KEYS,
    load_records,
    validate_records,
)


FIXABLE_FIELDS = frozenset(EXPORT_KEYS) - {"delivery_qc_note"}


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
) -> dict:
    errors, warnings = validate_records(records)
    missing_questions = connection.execute(
        "SELECT q.task_id FROM questions q JOIN batches b ON b.id=q.batch_id "
        "WHERE b.name=? AND EXISTS(SELECT 1 FROM runs x WHERE x.question_id=q.id) "
        "AND NOT EXISTS(SELECT 1 FROM records r WHERE r.question_id=q.id) "
        "ORDER BY q.question_no",
        (batch,),
    ).fetchall()
    errors.extend(
        f"{row['task_id']}: launched question has no delivery record"
        for row in missing_questions
    )
    source_rows = connection.execute(
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
        " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS source_harness_version "
        "FROM records r JOIN questions q ON q.id=r.question_id "
        "JOIN batches b ON b.id=q.batch_id WHERE b.name=?",
        (batch,),
    ).fetchall()
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
    return {
        "checked_records": len(records),
        "passed": not errors and not warnings,
        "errors": errors,
        "warnings": warnings,
        "scope": "Required export fields, identifiers, values, descriptions, sessions, and deadlines.",
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
) -> tuple[int, dict]:
    before_records = load_records(connection, batch)
    if not before_records:
        raise ValueError(f"batch has no delivery records: {batch}")
    before_report = build_report(connection, batch, before_records)
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
    after_report = build_report(connection, batch, after_records)
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


def finalize(connection: sqlite3.Connection, batch: str) -> tuple[int, dict]:
    records = load_records(connection, batch)
    if not records:
        raise ValueError(f"batch has no delivery records: {batch}")
    report = build_report(connection, batch, records)
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
    parser.add_argument("--report", type=Path)
    parser.add_argument("--fixes", type=Path)
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.fixes and args.finalize:
        parser.error("--fixes and --finalize cannot be used together")

    connection: sqlite3.Connection | None = None
    try:
        connection = connect(args.db.resolve())
        if args.fixes:
            changed, report = apply_fixes(
                connection, args.batch, args.fixes.resolve()
            )
            write_report(args.report.resolve() if args.report else None, report)
            print(f"已修正 {changed} 条交付记录；请继续处理剩余问题并重新质检。")
            return 0 if report["passed"] else 1
        if args.finalize:
            passed_count, report = finalize(connection, args.batch)
            write_report(args.report.resolve() if args.report else None, report)
            if not report["passed"]:
                print("仍有不符合规范的字段，不能标记通过。", file=sys.stderr)
                return 1
            print(f"批次 {args.batch}：质检通过（{passed_count} 条记录）")
            return 0

        records = load_records(connection, args.batch)
        if not records:
            raise ValueError(f"batch has no delivery records: {args.batch}")
        report = build_report(connection, args.batch, records)
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
