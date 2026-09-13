#!/usr/bin/env python3
"""Approve and submit at most one review-ready delivery record to SOLO2."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.batch_pipeline import connect
from tools.human_review import DIMENSIONS, approve_record, review_queue
from tools.solo2_service import submit_records


def _next_record(database: Path, max_attempts: int) -> tuple[str | None, bool]:
    """Return one eligible record and whether it still needs confirmation."""
    with connect(database) as connection:
        connection.execute(
            "UPDATE solo2_submissions SET status='retry_wait',lease_owner='',lease_expires_at='' "
            "WHERE status='submitting' AND julianday(lease_expires_at)<julianday('now')"
        )
        connection.commit()

    approved: list[dict] = []
    page = 1
    while True:
        payload = review_queue(
            database,
            view="all_pending",
            status="approved",
            page=page,
            page_size=100,
        )
        approved.extend(payload["records"])
        if page >= int(payload["meta"]["total_pages"]):
            break
        page += 1
    candidates = [
        record for record in approved
        if record["can_solo2_submit"]
        and record["solo2_status"] not in {"auth_blocked", "schema_blocked"}
        and int(record["solo2_attempt_count"]) < max_attempts
    ]
    if candidates:
        # New submissions take precedence over transient retries.
        candidates.sort(key=lambda record: bool(record["solo2_status"]))
        return str(candidates[0]["record_id"]), False

    ready = review_queue(
        database,
        view="all_pending",
        status="ready",
        page=1,
        page_size=1,
    )["records"]
    if ready:
        return str(ready[0]["record_id"]), True
    return None, False


def approve_and_submit(
    database: Path,
    cookie_file: Path,
    origin: str,
    *,
    reviewer: str,
    max_attempts: int,
    timeout: float,
) -> dict[str, object]:
    approved: list[str] = []
    approval_failures: list[dict[str, str]] = []
    confirmations = {dimension: True for dimension in DIMENSIONS}
    record_id, needs_approval = _next_record(database, max_attempts)

    if record_id and needs_approval:
        try:
            approve_record(
                database,
                record_id,
                reviewer,
                confirmations,
                note="定时一键确认五维通过",
            )
            approved.append(record_id)
        except ValueError as exc:
            approval_failures.append({"record_id": record_id, "error": str(exc)})

    # Include records approved in an earlier run whose submission previously failed.
    submission = (
        submit_records(
            database,
            cookie_file,
            origin,
            record_ids={record_id},
            limit=1,
            max_attempts=max_attempts,
            timeout=timeout,
            manual=False,
        )
        if record_id and not approval_failures
        else {"submitted": 0, "skipped": 0, "failed": 0, "results": []}
    )
    with connect(database) as connection:
        remaining = int(connection.execute(
            "SELECT COUNT(*) FROM records r "
            "LEFT JOIN solo2_submissions s ON s.record_id=r.record_id "
            "WHERE r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过' "
            "AND r.evidence_gate_passed=1 AND r.history_gate_passed=1 "
            "AND COALESCE(s.status,'') NOT IN ('succeeded','remote_pending_fix')"
        ).fetchone()[0])
    return {
        "ok": not approval_failures and int(submission["failed"]) == 0,
        "selected_record_id": record_id,
        "approved": len(approved),
        "approved_record_ids": approved,
        "approval_failures": approval_failures,
        "submission": submission,
        "remaining_review_or_submit": remaining,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--cookie-file", type=Path, default=Path(".local-auth/solo2.cookies"))
    parser.add_argument("--origin", default="https://solo2.jzxhnh.com")
    parser.add_argument("--reviewer", default="gaoyong")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.timeout > 600:
        raise SystemExit("--timeout must be between 0 and 600 seconds")
    result = approve_and_submit(
        args.db.resolve(),
        args.cookie_file.resolve(),
        args.origin,
        reviewer=args.reviewer,
        max_attempts=args.max_attempts,
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
