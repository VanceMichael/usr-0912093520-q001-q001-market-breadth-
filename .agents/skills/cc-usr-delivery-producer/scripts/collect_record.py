#!/usr/bin/env python3
"""Insert one human-authored delivery record into the production database."""

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
    DIFFICULTIES,
    SCORE_PREFIXES,
    TASK_TYPES,
    validate_one,
)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ask(label: str, *, allow_blank: bool = False) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value or allow_blank:
            return value
        print("This value is required.")


def ask_choice(label: str, choices: set[str]) -> str:
    print(f"{label} choices: {', '.join(sorted(choices))}")
    while True:
        value = ask(label)
        if value in choices:
            return value
        print("Invalid choice.")


def ask_score(label: str) -> int:
    while True:
        value = ask(label)
        if value in {"1", "2", "3", "4", "5"}:
            return int(value)
        print("Score must be an integer from 1 to 5.")


def collect_human_fields(turn_no: int) -> dict:
    print("只填写人工已经确认的内容。本工具不会读取轨迹、评分或改写描述。")
    data: dict[str, object] = {
        "session_id": ask("SessionID（人工从当前 Claude Code 会话记录复制）"),
        "turn_id": ask("PromptID（人工从本轮 user 消息复制）"),
    }
    if turn_no > 1:
        data.update({
            "user_prompt": ask("本轮 User Prompt 原文"),
            "task_type": ask_choice("本轮任务类型", TASK_TYPES),
            "difficulty": ask_choice("本轮任务难度", DIFFICULTIES),
            "languages": ask("本轮语言/框架（逗号分隔）"),
        })
    labels = {
        "delivery": "交付完整性",
        "instruction": "指令遵循",
        "planning": "任务规划",
        "reasoning": "推理能力",
        "execution": "执行能力",
    }
    for prefix in SCORE_PREFIXES:
        label = labels[prefix]
        data[f"{prefix}_score"] = ask_score(label)
        data[f"{prefix}_description"] = ask(f"{label} - 描述（人工原文）")
    data.update({
        "other_issues": ask("其他问题（没有可留空）", allow_blank=True),
        "submitter": ask("提交人"),
        "turn_completed_at": ask("本轮完成时间（ISO 8601，含时区）"),
        "submitted_at": now(),
        "human_authored": ask("确认评分和描述均由本人完成，输入 YES") == "YES",
    })
    return data


def load_input(path: Path | None, turn_no: int) -> dict:
    if path is None:
        return collect_human_fields(turn_no)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read input JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("input JSON must contain one object")
    return value


def get_question(
    connection: sqlite3.Connection, batch: str, question_no: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT q.*, b.name AS batch_name FROM questions q "
        "JOIN batches b ON b.id=q.batch_id WHERE b.name=? AND q.question_no=?",
        (batch, question_no),
    ).fetchone()
    if row is None:
        raise ValueError(f"question does not exist: {batch} / {question_no}")
    if row["status"] not in {"running", "completed"}:
        raise ValueError("question has not been launched")
    return row


def latest_run(connection: sqlite3.Connection, question_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM runs WHERE question_id=? ORDER BY launched_at DESC, id DESC LIMIT 1",
        (question_id,),
    ).fetchone()
    if row is None:
        raise ValueError("question has no registered Claude Code run")
    return row


def build_record(
    connection: sqlite3.Connection,
    question: sqlite3.Row,
    run: sqlite3.Row,
    turn_no: int,
    supplied: dict,
) -> dict:
    human_required = {
        "session_id", "turn_id", "delivery_score", "delivery_description",
        "instruction_score", "instruction_description", "planning_score",
        "planning_description", "reasoning_score", "reasoning_description",
        "execution_score", "execution_description", "other_issues", "submitter",
        "turn_completed_at", "human_authored",
    }
    if turn_no > 1:
        human_required.update({"user_prompt", "task_type", "difficulty", "languages"})
    missing = sorted(key for key in human_required if key not in supplied)
    if missing:
        raise ValueError("missing human-provided fields: " + ", ".join(missing))

    previous = connection.execute(
        "SELECT record_id, session_id FROM records WHERE question_id=? AND turn_no=?",
        (question["id"], turn_no - 1),
    ).fetchone() if turn_no > 1 else None
    if turn_no > 1 and previous is None:
        raise ValueError("the previous turn record must be stored first")
    if previous is not None and supplied["session_id"] != previous["session_id"]:
        raise ValueError("later turns must use the same SessionID as the previous turn")

    record = {
        "question_id": question["id"],
        "record_id": str(supplied.get("record_id") or f"{question['task_id']}-T{turn_no:02d}"),
        "turn_no": turn_no,
        "user_prompt": question["prompt"] if turn_no == 1 else supplied["user_prompt"],
        "session_id": supplied["session_id"],
        "turn_id": supplied["turn_id"],
        "initial_snapshot": question["initial_snapshot"],
        "reproducibility": question["reproducibility"],
        "harness": run["harness"],
        "harness_version": run["harness_version"],
        "operating_system": "MacOS/Linux",
        "task_type": question["task_type"] if turn_no == 1 else supplied["task_type"],
        "difficulty": question["difficulty"] if turn_no == 1 else supplied["difficulty"],
        "languages": question["languages"] if turn_no == 1 else supplied["languages"],
        "other_issues": supplied["other_issues"],
        "submitter": supplied["submitter"],
        "submitted_at": supplied.get("submitted_at") or now(),
        "parent_record": "" if previous is None else previous["record_id"],
        "turn_completed_at": supplied["turn_completed_at"],
        "human_authored": supplied["human_authored"],
        "human_qc_approved": False,
        "human_qc_reviewer": "",
        "human_qc_approved_at": "",
        "created_at": now(),
    }
    for prefix in SCORE_PREFIXES:
        record[f"{prefix}_score"] = supplied[f"{prefix}_score"]
        record[f"{prefix}_description"] = supplied[f"{prefix}_description"]
    return record


def insert_record(connection: sqlite3.Connection, record: dict) -> None:
    columns = list(record)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO records({', '.join(columns)}) VALUES({placeholders})",
        [record[column] for column in columns],
    )
    connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--question", type=int, required=True)
    parser.add_argument("--turn", type=int, required=True, choices=range(1, 11))
    parser.add_argument("--from-json", type=Path)
    args = parser.parse_args()
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(args.db.resolve())
        question = get_question(connection, args.batch, args.question)
        run = latest_run(connection, question["id"])
        supplied = load_input(args.from_json.resolve() if args.from_json else None, args.turn)
        record = build_record(connection, question, run, args.turn, supplied)
        errors, _warnings = validate_one(record)
        if errors:
            raise ValueError("; ".join(errors))
        insert_record(connection, record)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Record not stored: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
    print(f"Stored draft record {record['record_id']} in {args.db.resolve()}")
    print("下一步：按顺序录入其余有效轮次；批次全部录入后执行交付质检。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
