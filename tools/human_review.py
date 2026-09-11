#!/usr/bin/env python3
"""Human or Codex review state and hard delivery approval gates."""

from __future__ import annotations

import json
import re
from contextlib import closing
from pathlib import Path

from tools.batch_pipeline import connect, now
from tools.delivery_quality import (
    DIMENSIONS,
    evidence_fingerprint,
    history_matches,
    json_array,
    prose_english_issues,
    verify_evidence_sources,
)
from tools.delivery_records import as_record, validate_one


REVIEWERS = ("zhanglei", "renhuangding", "gaoyong")
CODEX_REVIEWER = "Codex 自动逐维复核"


def _record_context(connection, record_id: str):
    return connection.execute(
        "SELECT r.*,q.task_id,q.question_no,q.title,q.folder_path AS question_folder,"
        "b.name AS batch_name,"
        "(SELECT x.trajectory_root FROM runs x WHERE x.question_id=q.id "
        " AND x.status='succeeded' AND x.session_id=r.session_id "
        " ORDER BY x.launched_at DESC,x.id DESC LIMIT 1) AS trajectory_root "
        "FROM records r JOIN questions q ON q.id=r.question_id "
        "JOIN batches b ON b.id=q.batch_id WHERE r.record_id=?",
        (record_id,),
    ).fetchone()


def review_queue(database: Path, batch: str | None = None) -> dict:
    with closing(connect(database)) as connection:
        parameters: list[object] = []
        where = ""
        if batch:
            where = "WHERE b.name=?"
            parameters.append(batch)
        rows = connection.execute(
            "SELECT r.*,q.task_id,q.question_no,q.title,b.name AS batch_name,"
            "s.status AS solo2_status,s.attempt_count AS solo2_attempt_count,"
            "s.last_error AS solo2_last_error,s.remote_submission_id AS solo2_remote_id,"
            "s.updated_at AS solo2_updated_at "
            "FROM records r JOIN questions q ON q.id=r.question_id "
            "JOIN batches b ON b.id=q.batch_id "
            f"LEFT JOIN solo2_submissions s ON s.record_id=r.record_id {where} "
            "ORDER BY r.human_qc_approved ASC,b.created_at,q.question_no,r.turn_no",
            parameters,
        ).fetchall()
        reviews = connection.execute(
            "SELECT record_id,dimension,approved,reviewer,reviewed_at "
            "FROM record_dimension_reviews"
        ).fetchall()
        reviewed = {
            (str(row["record_id"]), str(row["dimension"])): dict(row)
            for row in reviews
        }
        codex_events = {}
        for event in connection.execute(
            "SELECT record_id,action,details,created_at FROM record_review_events "
            "WHERE action IN ('codex_review_started','codex_review_completed','codex_review_failed') "
            "ORDER BY id DESC"
        ).fetchall():
            codex_events.setdefault(str(event["record_id"]), dict(event))
        regeneration_events = {}
        for event in connection.execute(
            "SELECT record_id,action,details,created_at FROM record_review_events "
            "WHERE action IN ("
            "'delivery_regeneration_started','delivery_regeneration_completed',"
            "'delivery_regeneration_qc_completed','delivery_regeneration_failed') "
            "ORDER BY id DESC"
        ).fetchall():
            regeneration_events.setdefault(str(event["record_id"]), dict(event))
        items: list[dict] = []
        for row in rows:
            record = as_record(row)
            evidence, _ = json_array(record.get("evidence_ledger"), "evidence_ledger")
            coverage, _ = json_array(record.get("requirement_coverage"), "requirement_coverage")
            record["evidence_ledger"] = evidence
            record["requirement_coverage"] = coverage
            record["dimension_reviews"] = {
                dimension: reviewed.get((record["record_id"], dimension))
                for dimension in DIMENSIONS
            }
            codex_event = codex_events.get(record["record_id"])
            if codex_event:
                details = json.loads(str(codex_event.get("details") or "{}"))
                record["codex_review"] = {
                    "status": str(codex_event["action"]).removeprefix("codex_review_"),
                    "report": str(details.get("report") or ""),
                    "decision": str(details.get("decision") or ""),
                    "dimensions": details.get("dimensions") or {},
                    "updated_at": str(codex_event["created_at"]),
                }
            else:
                record["codex_review"] = None
            regeneration_event = regeneration_events.get(record["record_id"])
            if regeneration_event:
                try:
                    regeneration_details = json.loads(
                        str(regeneration_event.get("details") or "{}")
                    )
                except json.JSONDecodeError:
                    regeneration_details = {}
                regeneration_action = str(regeneration_event["action"])
                regeneration_status = {
                    "delivery_regeneration_started": "started",
                    "delivery_regeneration_completed": "quality_checking",
                    "delivery_regeneration_qc_completed": "completed",
                    "delivery_regeneration_failed": "failed",
                }[regeneration_action]
                record["delivery_regeneration"] = {
                    "status": regeneration_status,
                    "report": str(
                        regeneration_details.get("error")
                        or regeneration_details.get("message")
                        or ""
                    ),
                    "updated_at": str(regeneration_event["created_at"]),
                }
            else:
                record["delivery_regeneration"] = None
            record["history_matches"] = history_matches(
                connection, record, exclude_record_id=record["record_id"]
            )
            record["ready_for_review"] = bool(
                record["delivery_qc_passed"]
                and record["evidence_gate_passed"]
                and record["history_gate_passed"]
                and not record["history_matches"]
            )
            record["solo2_status"] = str(record.get("solo2_status") or "")
            record["solo2_attempt_count"] = int(record.get("solo2_attempt_count") or 0)
            record["solo2_last_error"] = str(record.get("solo2_last_error") or "")
            record["solo2_remote_id"] = str(record.get("solo2_remote_id") or "")
            record["solo2_updated_at"] = str(record.get("solo2_updated_at") or "")
            record["can_solo2_submit"] = bool(
                record["delivery_qc_passed"]
                and record.get("delivery_qc_note") == "质检通过"
                and record["evidence_gate_passed"]
                and record["history_gate_passed"]
                and not record["history_matches"]
                and record["human_qc_approved"]
                and record.get("review_method") in {"human", "codex"}
                and record["solo2_status"] not in {"succeeded", "submitting"}
            )
            items.append(record)
    return {
        "reviewers": list(REVIEWERS),
        "records": items,
        "summary": {
            "total": len(items),
            "waiting": sum(
                not item["human_qc_approved"] and item["solo2_status"] != "succeeded"
                for item in items
            ),
            "approved": sum(item["human_qc_approved"] for item in items),
            "delivered": sum(item["solo2_status"] == "succeeded" for item in items),
            "human_approved": sum(
                item["human_qc_approved"] and item.get("review_method") == "human"
                for item in items
            ),
            "codex_approved": sum(
                item["human_qc_approved"] and item.get("review_method") == "codex"
                for item in items
            ),
            "blocked": sum(
                not item["human_qc_approved"]
                and item["solo2_status"] != "succeeded"
                and not item["ready_for_review"]
                for item in items
            ),
        },
    }


def _review_gate_errors(connection, row) -> list[str]:
    record = as_record(row)
    record_id = str(record["record_id"])
    errors, warnings = validate_one(record, require_delivery_qc=True)
    trajectory_root = str(row["trajectory_root"] or "").strip()
    if not trajectory_root:
        errors.append(f"{record_id}: 缺少权威轨迹目录")
    else:
        errors.extend(verify_evidence_sources(
            record,
            question_folder=Path(str(row["question_folder"])),
            trajectory_root=Path(trajectory_root),
        ))
    duplicates = history_matches(connection, record, exclude_record_id=record_id)
    if duplicates:
        first = duplicates[0]
        errors.append(
            f"{record_id}: {first['dimension']} 描述与 "
            f"{first['source_node']}的 {first['record_id']} 过于相似"
        )
    return errors + warnings


def build_codex_review_dossier(database: Path, record_id: str) -> dict:
    """Build a secret-free, evidence-focused dossier for an isolated Codex review."""
    with closing(connect(database)) as connection:
        row = _record_context(connection, record_id)
        if row is None:
            raise ValueError("交付记录不存在")
        if bool(row["human_qc_approved"]):
            connection.rollback()
            raise ValueError("记录已经完成最终复核；如需重审，请先退回记录")
        errors = _review_gate_errors(connection, row)
        if errors:
            raise ValueError("Codex 复核前门禁未通过：" + "；".join(errors))
        record = as_record(row)
        evidence, evidence_errors = json_array(record.get("evidence_ledger"), "evidence_ledger")
        coverage, coverage_errors = json_array(record.get("requirement_coverage"), "requirement_coverage")
        if evidence_errors or coverage_errors:
            raise ValueError("；".join(evidence_errors + coverage_errors))
        return {
            "record_id": record_id,
            "batch": str(row["batch_name"]),
            "question_no": int(row["question_no"]),
            "turn_no": int(record["turn_no"]),
            "title": str(row["title"]),
            "user_prompt": str(record["user_prompt"]),
            "scores": {
                dimension: {
                    "score": int(record[f"{dimension}_score"]),
                    "description": str(record[f"{dimension}_description"]),
                    "evidence": [item for item in evidence if item.get("dimension") == dimension],
                }
                for dimension in DIMENSIONS
            },
            "other_issues": str(record.get("other_issues") or ""),
            "requirement_coverage": coverage,
            "history_matches": history_matches(
                connection, record, exclude_record_id=record_id
            ),
            "mechanical_gates": {
                "delivery_qc_passed": bool(record["delivery_qc_passed"]),
                "evidence_gate_passed": bool(record["evidence_gate_passed"]),
                "history_gate_passed": bool(record["history_gate_passed"]),
            },
        }


def build_codex_delivery_regeneration_dossier(database: Path, record_id: str) -> dict:
    """Build the evidence-only input for regenerating one delivery record.

    This deliberately includes the existing evidence and coverage tables instead
    of granting the regeneration task access to the question workspace.  Codex
    can rewrite unsupported prose, but it cannot invent a new source or modify
    the Claude result.
    """
    with closing(connect(database)) as connection:
        row = _record_context(connection, record_id)
        if row is None:
            raise ValueError("交付记录不存在")
        submission = connection.execute(
            "SELECT status FROM solo2_submissions WHERE record_id=?", (record_id,)
        ).fetchone()
        if submission and str(submission["status"] or "") in {"submitting", "succeeded"}:
            raise ValueError("记录正在提交或已经提交到 SOLO2，不能重新生成")
        record = as_record(row)
        evidence, evidence_errors = json_array(record.get("evidence_ledger"), "evidence_ledger")
        coverage, coverage_errors = json_array(record.get("requirement_coverage"), "requirement_coverage")
        if evidence_errors or coverage_errors:
            raise ValueError("；".join(evidence_errors + coverage_errors))
        events = connection.execute(
            "SELECT action,note,details,created_at FROM record_review_events "
            "WHERE record_id=? ORDER BY id DESC LIMIT 10", (record_id,)
        ).fetchall()
        return {
            "record_id": record_id,
            "batch": str(row["batch_name"]),
            "question_no": int(row["question_no"]),
            "turn_no": int(record["turn_no"]),
            "title": str(row["title"]),
            "user_prompt": str(record["user_prompt"]),
            "question_folder": str(row["question_folder"]),
            "trajectory_root": str(row["trajectory_root"] or ""),
            "current": {
                "delivery_score": int(record["delivery_score"]),
                "delivery_description": str(record["delivery_description"]),
                "instruction_score": int(record["instruction_score"]),
                "instruction_description": str(record["instruction_description"]),
                "planning_score": int(record["planning_score"]),
                "planning_description": str(record["planning_description"]),
                "reasoning_score": int(record["reasoning_score"]),
                "reasoning_description": str(record["reasoning_description"]),
                "execution_score": int(record["execution_score"]),
                "execution_description": str(record["execution_description"]),
                "other_issues": str(record.get("other_issues") or ""),
            },
            "evidence_ledger": evidence,
            "requirement_coverage": coverage,
            "review_history": [
                {"action": str(event["action"]), "note": str(event["note"] or ""),
                 "details": str(event["details"] or "{}"), "created_at": str(event["created_at"])}
                for event in events
            ],
        }


def approve_codex_record(database: Path, record_id: str, review: dict) -> dict:
    """Accept a strict five-dimension Codex verdict as the final review gate."""
    if not isinstance(review, dict) or review.get("decision") != "approved":
        raise ValueError("Codex 结论不是通过")
    dimensions = review.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("Codex 复核缺少五维结论")
    for dimension in DIMENSIONS:
        item = dimensions.get(dimension)
        if not isinstance(item, dict) or item.get("approved") is not True:
            raise ValueError(f"Codex 未通过维度：{dimension}")
        reason = str(item.get("reason") or "").strip()
        if len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", reason)) < 8:
            raise ValueError(f"Codex 对 {dimension} 的复核依据不完整")
        if prose_english_issues(reason):
            raise ValueError(f"Codex 对 {dimension} 的复核依据英文过多")
    summary = str(review.get("summary") or "").strip()
    if len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", summary)) < 8:
        raise ValueError("Codex 复核总结不完整")
    if prose_english_issues(summary):
        raise ValueError("Codex 复核总结英文过多")
    issues = review.get("issues")
    if issues not in (None, []) and (not isinstance(issues, list) or issues):
        raise ValueError("Codex 报告仍包含阻断问题")

    with closing(connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _record_context(connection, record_id)
        if row is None:
            raise ValueError("交付记录不存在")
        if bool(row["human_qc_approved"]):
            connection.rollback()
            raise ValueError("记录已经完成最终复核；如需重审，请先退回记录")
        errors = _review_gate_errors(connection, row)
        if errors:
            connection.rollback()
            raise ValueError("Codex 放行前门禁未通过：" + "；".join(errors))
        timestamp = now()
        connection.executemany(
            "INSERT INTO record_dimension_reviews(record_id,dimension,approved,reviewer,reviewed_at) "
            "VALUES(?,?,1,?,?) ON CONFLICT(record_id,dimension) DO UPDATE SET "
            "approved=1,reviewer=excluded.reviewer,reviewed_at=excluded.reviewed_at",
            [(record_id, dimension, CODEX_REVIEWER, timestamp) for dimension in DIMENSIONS],
        )
        connection.execute(
            "UPDATE records SET human_qc_approved=1,human_qc_reviewer=?,"
            "human_qc_approved_at=?,human_qc_note=?,review_method='codex',"
            "evidence_gate_passed=1,evidence_checked_at=?,history_gate_passed=1,history_checked_at=? "
            "WHERE record_id=?",
            (CODEX_REVIEWER, timestamp, summary, timestamp, timestamp, record_id),
        )
        connection.execute(
            "INSERT INTO record_review_events(record_id,action,reviewer,note,details,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                record_id, "codex_approved", CODEX_REVIEWER, summary,
                json.dumps(review, ensure_ascii=False), timestamp,
            ),
        )
        connection.commit()
    return {
        "ok": True, "record_id": record_id, "reviewer": CODEX_REVIEWER,
        "review_method": "codex", "approved_at": timestamp,
    }


def approve_record(
    database: Path,
    record_id: str,
    reviewer: str,
    confirmations: dict,
    note: str = "",
) -> dict:
    if reviewer not in REVIEWERS:
        raise ValueError("审核人必须从固定人员中选择")
    if not isinstance(confirmations, dict) or any(
        confirmations.get(dimension) is not True for dimension in DIMENSIONS
    ):
        raise ValueError("五个维度都必须分别确认")
    with closing(connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _record_context(connection, record_id)
        if row is None:
            raise ValueError("交付记录不存在")
        if bool(row["human_qc_approved"]):
            connection.rollback()
            raise ValueError("记录已经完成最终复核；如需重审，请先退回记录")
        errors = _review_gate_errors(connection, row)
        if errors:
            connection.rollback()
            raise ValueError("人工确认前门禁未通过：" + "；".join(errors))
        timestamp = now()
        connection.executemany(
            "INSERT INTO record_dimension_reviews(record_id,dimension,approved,reviewer,reviewed_at) "
            "VALUES(?,?,1,?,?) ON CONFLICT(record_id,dimension) DO UPDATE SET "
            "approved=1,reviewer=excluded.reviewer,reviewed_at=excluded.reviewed_at",
            [(record_id, dimension, reviewer, timestamp) for dimension in DIMENSIONS],
        )
        connection.execute(
            "UPDATE records SET human_qc_approved=1,human_qc_reviewer=?,"
            "human_qc_approved_at=?,human_qc_note=?,review_method='human',evidence_gate_passed=1,"
            "evidence_checked_at=?,history_gate_passed=1,history_checked_at=? "
            "WHERE record_id=?",
            (reviewer, timestamp, note.strip(), timestamp, timestamp, record_id),
        )
        connection.execute(
            "INSERT INTO record_review_events(record_id,action,reviewer,note,details,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                record_id, "approved", reviewer, note.strip(),
                json.dumps({"dimensions": list(DIMENSIONS)}, ensure_ascii=False), timestamp,
            ),
        )
        connection.commit()
    return {"ok": True, "record_id": record_id, "reviewer": reviewer, "approved_at": timestamp}


def reject_record(database: Path, record_id: str, reviewer: str, note: str) -> dict:
    if reviewer not in REVIEWERS:
        raise ValueError("审核人必须从固定人员中选择")
    if len(note.strip()) < 4:
        raise ValueError("退回时请说明具体原因")
    with closing(connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if _record_context(connection, record_id) is None:
            raise ValueError("交付记录不存在")
        submission = connection.execute(
            "SELECT status FROM solo2_submissions WHERE record_id=?", (record_id,)
        ).fetchone()
        if submission and submission["status"] in {"submitting", "succeeded"}:
            connection.rollback()
            raise ValueError("记录正在提交或已经提交到 SOLO2，不能退回重写")
        timestamp = now()
        connection.execute(
            "DELETE FROM record_dimension_reviews WHERE record_id=?", (record_id,)
        )
        connection.execute(
            "UPDATE records SET human_qc_approved=0,human_qc_reviewer=?,"
            "human_qc_approved_at='',human_qc_note=?,review_method='' WHERE record_id=?",
            (reviewer, note.strip(), record_id),
        )
        connection.execute(
            "INSERT INTO record_review_events(record_id,action,reviewer,note,details,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (record_id, "rejected", reviewer, note.strip(), "{}", timestamp),
        )
        connection.commit()
    return {"ok": True, "record_id": record_id, "reviewer": reviewer, "rejected_at": timestamp}


def approved_history(database: Path) -> dict:
    with closing(connect(database)) as connection:
        rows = connection.execute(
            "SELECT record_id,human_qc_approved_at," + ",".join(
                f"{dimension}_description" for dimension in DIMENSIONS
            ) + " FROM records WHERE human_qc_approved=1 ORDER BY human_qc_approved_at"
        ).fetchall()
    descriptions = []
    for row in rows:
        for dimension in DIMENSIONS:
            value = str(row[f"{dimension}_description"])
            descriptions.append({
                "record_id": str(row["record_id"]),
                "dimension": dimension,
                "description": value,
                "fingerprint": evidence_fingerprint(value),
                "approved_at": str(row["human_qc_approved_at"]),
            })
    return {"descriptions": descriptions}


def import_history(database: Path, source_node: str, descriptions: object) -> dict:
    if not source_node.strip() or not isinstance(descriptions, list):
        raise ValueError("历史来源和描述列表不能为空")
    timestamp = now()
    accepted = []
    for item in descriptions:
        if not isinstance(item, dict) or item.get("dimension") not in DIMENSIONS:
            raise ValueError("远程历史记录格式无效")
        description = str(item.get("description") or "").strip()
        record_id = str(item.get("record_id") or "").strip()
        approved_at = str(item.get("approved_at") or "").strip()
        if not description or not record_id or not approved_at:
            raise ValueError("远程历史记录缺少必填字段")
        fingerprint = evidence_fingerprint(description)
        if item.get("fingerprint") != fingerprint:
            raise ValueError("远程历史记录校验值不一致")
        accepted.append((
            source_node.strip(), record_id, str(item["dimension"]), description,
            fingerprint, approved_at, timestamp,
        ))
    with closing(connect(database)) as connection:
        connection.executemany(
            "INSERT INTO description_history(source_node,source_record_id,dimension,description,"
            "fingerprint,approved_at,synced_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source_node,source_record_id,dimension) DO UPDATE SET "
            "description=excluded.description,fingerprint=excluded.fingerprint,"
            "approved_at=excluded.approved_at,synced_at=excluded.synced_at",
            accepted,
        )
        connection.commit()
    return {"ok": True, "imported": len(accepted)}
