#!/usr/bin/env python3
"""Run structural QC and record explicit human approval in SQLite."""

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
from tools.delivery_records import load_records, validate_records  # noqa: E402


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_record_ids(raw: str) -> list[str]:
    values = list(dict.fromkeys(value.strip() for value in raw.split(",") if value.strip()))
    if not values:
        raise ValueError("record selection is empty")
    return values


def write_report(path: Path | None, report: dict) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def approve_records(
    connection: sqlite3.Connection,
    batch: str,
    record_ids: list[str],
    reviewer: str,
    confirmation: str,
) -> int:
    if confirmation != "YES":
        raise ValueError("human reviewer must pass --confirm-human-review YES")
    if not reviewer.strip():
        raise ValueError("reviewer is required")
    all_records = load_records(connection, batch)
    if not all_records:
        raise ValueError(f"batch has no delivery records: {batch}")
    errors, warnings = validate_records(all_records)
    if errors or warnings:
        write_report(None, {
            "checked_records": len(all_records),
            "passed": False,
            "errors": errors,
            "warnings": warnings,
            "scope": "Structural checks only. Human qualitative QC remains mandatory.",
        })
        return 1
    by_id = {record["record_id"]: record for record in all_records}
    missing = [record_id for record_id in record_ids if record_id not in by_id]
    if missing:
        raise ValueError("record IDs not found in batch: " + ", ".join(missing))
    timestamp = now()
    placeholders = ", ".join("?" for _ in record_ids)
    connection.execute(
        f"UPDATE records SET human_qc_approved=1, human_qc_reviewer=?, "
        f"human_qc_approved_at=? WHERE record_id IN ({placeholders})",
        [reviewer.strip(), timestamp, *record_ids],
    )
    connection.commit()
    print(f"Human QC approved {len(record_ids)} record(s): {', '.join(record_ids)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-human-qc", action="store_true")
    parser.add_argument("--approve")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--confirm-human-review", default="")
    args = parser.parse_args()
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(args.db.resolve())
        if args.approve:
            if not args.batch:
                raise ValueError("--batch is required for approval")
            return approve_records(
                connection,
                args.batch,
                parse_record_ids(args.approve),
                args.reviewer,
                args.confirm_human_review,
            )
        records = load_records(connection, args.batch)
        if not records:
            raise ValueError("no delivery records found")
        errors, warnings = validate_records(records, args.require_human_qc)
        report = {
            "checked_records": len(records),
            "passed": not errors and not warnings,
            "errors": errors,
            "warnings": warnings,
            "scope": "Structural checks only. Human qualitative QC remains mandatory.",
        }
        write_report(args.report.resolve() if args.report else None, report)
        return 1 if errors or warnings else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Delivery QC failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    sys.exit(main())
