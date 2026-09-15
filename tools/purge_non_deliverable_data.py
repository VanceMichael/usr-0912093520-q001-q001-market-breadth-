#!/usr/bin/env python3
"""Remove locally stored questions that are no longer deliverable.

The command is deliberately dry-run by default.  It removes only the selected
difficulty from SQLite and task-owned filesystem/Docker resources; SOLO2 is
read-only from this tool and remote submissions are never deleted.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect, now, render_batch


DEFAULT_DIFFICULTIES = ("中等", "medium")


def _json_object(raw: object) -> dict[str, object]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.resolve() != root.resolve()


def _unique_paths(values: Iterable[Path]) -> list[Path]:
    return sorted({value.resolve() for value in values}, key=lambda item: str(item))


def _task_run_root(path: Path, task_id: str, project_root: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name == task_id and _inside(candidate, project_root):
            return candidate
        if candidate == project_root.resolve():
            break
    return None


def _scheduler_stopped(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT desired_state,actual_state FROM scheduler_state WHERE id=1"
    ).fetchone()
    if row is None:
        return True
    return str(row["desired_state"]) == "stopped" and str(row["actual_state"]) == "stopped"


def build_plan(
    database: Path, project_root: Path, difficulties: tuple[str, ...]
) -> dict[str, object]:
    marks = ",".join("?" for _ in difficulties)
    with closing(connect(database)) as connection:
        if not _scheduler_stopped(connection):
            raise RuntimeError("调度器尚未完全停止，拒绝生成可执行清单")
        questions = [dict(row) for row in connection.execute(
            "SELECT q.*,b.name AS batch_name,b.folder_path AS batch_folder,"
            "b.markdown_path AS batch_markdown FROM questions q JOIN batches b ON b.id=q.batch_id "
            f"WHERE lower(trim(q.difficulty)) IN ({marks}) ORDER BY b.id,q.question_no",
            tuple(value.lower() for value in difficulties),
        )]
        question_ids = [int(row["id"]) for row in questions]
        if not question_ids:
            return {
                "database": str(database), "project_root": str(project_root),
                "difficulties": list(difficulties), "questions": [], "counts": {"questions": 0},
            }
        qmarks = ",".join("?" for _ in question_ids)
        running = connection.execute(
            f"SELECT q.task_id,r.status FROM runs r JOIN questions q ON q.id=r.question_id "
            f"WHERE r.question_id IN ({qmarks}) AND r.status='running'",
            question_ids,
        ).fetchall()
        if running:
            raise RuntimeError("仍有目标题正在运行：" + ", ".join(str(row["task_id"]) for row in running))

        records = [dict(row) for row in connection.execute(
            f"SELECT r.*,b.folder_path AS batch_folder FROM records r "
            "JOIN questions q ON q.id=r.question_id JOIN batches b ON b.id=q.batch_id "
            f"WHERE r.question_id IN ({qmarks})", question_ids,
        )]
        runs = [dict(row) for row in connection.execute(
            f"SELECT r.*,q.task_id FROM runs r JOIN questions q ON q.id=r.question_id "
            f"WHERE r.question_id IN ({qmarks})", question_ids,
        )]
        record_ids = [str(row["record_id"]) for row in records]

        mother_rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM mother_library WHERE source_question_id IN ({qmarks})", question_ids,
        )]
        mother_ids = [int(row["id"]) for row in mother_rows]
        if mother_ids:
            mmarks = ",".join("?" for _ in mother_ids)
            surviving_usages = connection.execute(
                f"SELECT m.source_task_id,q.task_id FROM mother_usages u "
                "JOIN mother_library m ON m.id=u.mother_id "
                "JOIN questions q ON q.id=u.derived_question_id "
                f"WHERE u.mother_id IN ({mmarks}) AND u.derived_question_id NOT IN ({qmarks})",
                [*mother_ids, *question_ids],
            ).fetchall()
            if surviving_usages:
                details = ", ".join(
                    f"{row['source_task_id']}->{row['task_id']}" for row in surviving_usages
                )
                raise RuntimeError("中等母题仍被保留题引用，拒绝删除：" + details)

        affected_batches = sorted({str(row["batch_name"]) for row in questions})
        news_release_ids: list[int] = []
        news_ambiguous: list[dict[str, object]] = []
        batch_results: list[dict[str, object]] = []
        for batch in affected_batches:
            all_questions = connection.execute(
                "SELECT q.id,q.question_no,q.difficulty,q.task_id FROM questions q "
                "JOIN batches b ON b.id=q.batch_id WHERE b.name=? ORDER BY q.question_no", (batch,),
            ).fetchall()
            target_by_no = {
                int(row["question_no"]) for row in all_questions
                if str(row["difficulty"]).strip().lower() in {value.lower() for value in difficulties}
            }
            topics = connection.execute(
                "SELECT id FROM news_topics WHERE used_batch=? AND status='used' ORDER BY created_at,id",
                (batch,),
            ).fetchall()
            sequential = [int(row["question_no"]) for row in all_questions] == list(
                range(1, len(all_questions) + 1)
            )
            if topics and len(topics) == len(all_questions) and sequential:
                news_release_ids.extend(
                    int(topics[index - 1]["id"]) for index in sorted(target_by_no)
                )
            elif topics:
                news_ambiguous.append({
                    "batch": batch, "topics": len(topics), "questions": len(all_questions),
                    "target_questions": len(target_by_no),
                })
            remaining = len(all_questions) - len(target_by_no)
            batch_results.append({
                "batch": batch, "before": len(all_questions),
                "removed": len(target_by_no), "remaining": remaining,
            })

        repair_ids: list[int] = []
        record_id_set = set(record_ids)
        for row in connection.execute("SELECT remote_id,local_record_id,detail_json FROM solo2_repairs"):
            detail = _json_object(row["detail_json"])
            repair_difficulty = str(detail.get("difficulty") or "").strip().lower()
            if str(row["local_record_id"] or "") in record_id_set or repair_difficulty in {
                value.lower() for value in difficulties
            }:
                repair_ids.append(int(row["remote_id"]))

        workspace_paths = _unique_paths(
            Path(str(row["folder_path"])) for row in questions
            if _inside(Path(str(row["folder_path"])), project_root)
        )
        run_paths: list[Path] = []
        log_paths: list[Path] = []
        for row in runs:
            trajectory = str(row.get("trajectory_root") or "").strip()
            if trajectory:
                root = _task_run_root(Path(trajectory), str(row["task_id"]), project_root)
                if root is not None:
                    run_paths.append(root)
                elif _inside(Path(trajectory), project_root):
                    run_paths.append(Path(trajectory))
            log = str(row.get("log_path") or "").strip()
            if log and _inside(Path(log), project_root):
                log_paths.append(Path(log))

        exported_trajectories: list[Path] = []
        for row in records:
            name = Path(str(row.get("trajectory_file") or "")).name
            batch_folder = Path(str(row["batch_folder"]))
            if name and batch_folder.is_dir() and _inside(batch_folder, project_root):
                exported_trajectories.extend(batch_folder.rglob(f"*{Path(name).stem}*.jsonl"))
        # Every affected batch is mixed difficulty.  Keep its workbook because
        # it may be the only local export for a retained/repairable hard item.
        workbooks: list[Path] = []

        counts = {
            "questions": len(questions), "records": len(records), "runs": len(runs),
            "batches": len(affected_batches), "repair_cache_rows": len(repair_ids),
            "news_released": len(news_release_ids),
            "news_ambiguous_batches": len(news_ambiguous),
            "mother_rows": len(mother_rows), "workspaces": len(workspace_paths),
            "run_paths": len(_unique_paths(run_paths)), "log_paths": len(_unique_paths(log_paths)),
            "exported_trajectories": len(_unique_paths(exported_trajectories)),
            "mixed_workbooks_removed": len(workbooks),
        }
        return {
            "database": str(database), "project_root": str(project_root),
            "difficulties": list(difficulties), "counts": counts,
            "question_ids": question_ids,
            "questions": [
                {
                    "id": row["id"], "task_id": row["task_id"], "batch": row["batch_name"],
                    "question_no": row["question_no"], "difficulty": row["difficulty"],
                    "folder_path": row["folder_path"],
                }
                for row in questions
            ],
            "record_ids": record_ids, "repair_ids": sorted(set(repair_ids)),
            "mother_ids": mother_ids, "news_release_ids": sorted(set(news_release_ids)),
            "news_ambiguous": news_ambiguous, "batches": batch_results,
            "workspace_paths": [str(path) for path in workspace_paths],
            "run_paths": [str(path) for path in _unique_paths(run_paths)],
            "log_paths": [str(path) for path in _unique_paths(log_paths)],
            "exported_trajectories": [str(path) for path in _unique_paths(exported_trajectories)],
            "workbooks": [str(path) for path in workbooks],
        }


def _delete_by_ids(
    connection: sqlite3.Connection, table: str, column: str, values: list[object]
) -> None:
    if not values:
        return
    marks = ",".join("?" for _ in values)
    connection.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", values)


def apply_database(plan: dict[str, object], database: Path) -> list[str]:
    question_ids = [int(value) for value in plan.get("question_ids", [])]
    record_ids = [str(value) for value in plan.get("record_ids", [])]
    repair_ids = [int(value) for value in plan.get("repair_ids", [])]
    mother_ids = [int(value) for value in plan.get("mother_ids", [])]
    news_ids = [int(value) for value in plan.get("news_release_ids", [])]
    timestamp = now()
    with closing(connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _delete_by_ids(connection, "solo2_repair_events", "remote_id", repair_ids)
        _delete_by_ids(connection, "solo2_repairs", "remote_id", repair_ids)
        _delete_by_ids(connection, "solo2_submissions", "question_id", question_ids)
        _delete_by_ids(connection, "description_history", "source_record_id", record_ids)
        _delete_by_ids(connection, "record_dimension_reviews", "record_id", record_ids)
        _delete_by_ids(connection, "record_review_events", "record_id", record_ids)
        _delete_by_ids(connection, "records", "question_id", question_ids)
        _delete_by_ids(connection, "runs", "question_id", question_ids)
        _delete_by_ids(connection, "pipeline_items", "question_id", question_ids)
        _delete_by_ids(connection, "question_reset_audit", "question_id", question_ids)

        if question_ids:
            marks = ",".join("?" for _ in question_ids)
            parent_rows = connection.execute(
                f"SELECT mother_id,COUNT(*) AS removed FROM mother_usages "
                f"WHERE derived_question_id IN ({marks}) GROUP BY mother_id", question_ids,
            ).fetchall()
            _delete_by_ids(connection, "mother_usages", "derived_question_id", question_ids)
            for row in parent_rows:
                connection.execute(
                    "UPDATE mother_library SET use_count=max(0,use_count-?),updated_at=? WHERE id=?",
                    (int(row["removed"]), timestamp, int(row["mother_id"])),
                )
        _delete_by_ids(connection, "mother_library", "id", mother_ids)
        _delete_by_ids(connection, "questions", "id", question_ids)
        if news_ids:
            marks = ",".join("?" for _ in news_ids)
            connection.execute(
                f"UPDATE news_topics SET status='new',used_batch='',updated_at=? WHERE id IN ({marks})",
                [timestamp, *news_ids],
            )

        remaining_batches: list[str] = []
        for item in plan.get("batches", []):
            batch = str(item["batch"])
            row = connection.execute("SELECT id FROM batches WHERE name=?", (batch,)).fetchone()
            if row is None:
                continue
            count = int(connection.execute(
                "SELECT COUNT(*) FROM questions WHERE batch_id=?", (row["id"],)
            ).fetchone()[0])
            if count:
                connection.execute(
                    "UPDATE batches SET question_count=?,updated_at=? WHERE id=?",
                    (count, timestamp, row["id"]),
                )
                remaining_batches.append(batch)
            else:
                connection.execute("DELETE FROM delivery_downloads WHERE batch_id=?", (row["id"],))
                connection.execute("DELETE FROM author_jobs WHERE batch_name=?", (batch,))
                connection.execute("DELETE FROM pipeline_jobs WHERE batch_name=?", (batch,))
                connection.execute("DELETE FROM batches WHERE id=?", (row["id"],))
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("数据库外键检查失败，事务已回滚")
        connection.commit()
    return remaining_batches


def remove_files(plan: dict[str, object], project_root: Path) -> dict[str, int]:
    removed = {"directories": 0, "files": 0}
    for raw in [*plan.get("run_paths", []), *plan.get("workspace_paths", [])]:
        path = Path(str(raw)).resolve()
        if path.is_dir() and _inside(path, project_root):
            shutil.rmtree(path)
            removed["directories"] += 1
    for raw in [
        *plan.get("log_paths", []), *plan.get("exported_trajectories", []),
        *plan.get("workbooks", []),
    ]:
        path = Path(str(raw)).resolve()
        if path.is_file() and _inside(path, project_root):
            path.unlink()
            removed["files"] += 1
    for raw in plan.get("batches", []):
        batch = str(raw["batch"])
        folder = (project_root / batch).resolve()
        if int(raw["remaining"]) == 0 and folder.is_dir() and _inside(folder, project_root):
            shutil.rmtree(folder)
            removed["directories"] += 1
    return removed


def _docker_lines(command: list[str]) -> list[str]:
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or "Docker command failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def docker_targets(plan: dict[str, object]) -> list[str]:
    tokens: list[re.Pattern[str]] = []
    for row in plan.get("questions", []):
        task_id = str(row["task_id"]).lower()
        batch = str(row["batch"]).lower()
        question_no = int(row["question_no"])
        tokens.append(re.compile(
            rf"(^|[-_/])(?:{re.escape(task_id)}|{re.escape(batch)}-q{question_no:03d})(?:$|[-_/:])"
        ))
    targets: list[str] = []
    for line in _docker_lines(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]):
        lowered = line.lower()
        if line.startswith("ccusr-claude-worker:"):
            continue
        if any(pattern.search(lowered) for pattern in tokens):
            targets.append(line)
    return sorted(set(targets))


def remove_docker(plan: dict[str, object]) -> dict[str, object]:
    targets = docker_targets(plan)
    removed_containers: list[str] = []
    removed_images: list[str] = []
    for target in targets:
        containers = _docker_lines(["docker", "ps", "-aq", "--filter", f"ancestor={target}"])
        if containers:
            running = set(_docker_lines(["docker", "ps", "-q", "--filter", f"ancestor={target}"]))
            if running:
                raise RuntimeError(f"目标镜像仍有运行中容器，拒绝删除：{target}")
            subprocess.run(["docker", "container", "rm", *containers], check=True)
            removed_containers.extend(containers)
        result = subprocess.run(
            ["docker", "image", "rm", target], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if result.returncode == 0:
            removed_images.append(target)
    return {
        "matched_images": targets, "removed_images": removed_images,
        "removed_containers": removed_containers,
    }


def compact_database(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("VACUUM 后数据库外键检查失败")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--difficulty", action="append", dest="difficulties")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--docker", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.db.resolve(strict=True)
    project_root = args.project_root.resolve(strict=True)
    difficulties = tuple(args.difficulties or DEFAULT_DIFFICULTIES)
    plan = build_plan(database, project_root, difficulties)
    plan["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if args.docker:
        plan["docker_images"] = docker_targets(plan)
    manifest = args.manifest or Path("/tmp") / (
        "ccusr-purge-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
    )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "manifest": str(manifest), **plan["counts"]}, ensure_ascii=False))
    if not args.apply or not plan.get("question_ids"):
        return 0

    remaining_batches = apply_database(plan, database)
    file_result = remove_files(plan, project_root)
    with closing(connect(database)) as connection:
        for batch in remaining_batches:
            render_batch(connection, batch)
    docker_result: dict[str, object] = {}
    if args.docker:
        docker_result = remove_docker(plan)
    compact_database(database)
    print(json.dumps({
        "ok": True, "manifest": str(manifest), "files": file_result,
        "docker": docker_result,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
