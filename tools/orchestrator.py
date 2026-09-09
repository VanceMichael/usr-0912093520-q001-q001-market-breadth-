#!/usr/bin/env python3
"""Run restartable Claude workers and stream successful tasks into delivery."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import SNAPSHOT_RE, connect, prompt_hash, question_rows


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ready(row: sqlite3.Row) -> bool:
    return bool(
        row["mechanical_qc"] == "pass"
        and row["qc_decision"] == "pass"
        and row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        and row["status"] == "approved"
    )


def restore_question_workspace(row: sqlite3.Row) -> None:
    """Restore a failed question to its registered clean baseline before retry."""
    folder = Path(str(row["folder_path"])).resolve(strict=True)
    baseline = str(row["local_initial_sha"] or "").strip()
    snapshot = str(row["initial_snapshot"] or "").strip()
    if (
        not re.fullmatch(r"[0-9a-fA-F]{40}", baseline)
        or not SNAPSHOT_RE.fullmatch(snapshot)
        or snapshot.rsplit("/", 1)[-1].lower() != baseline.lower()
    ):
        raise RuntimeError(f"{row['task_id']} 缺少一致的初始快照，拒绝在脏工作区重试")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(folder), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    root = git("rev-parse", "--show-toplevel")
    exists = git("cat-file", "-e", f"{baseline}^{{commit}}")
    if root.returncode or Path(root.stdout.strip()).resolve() != folder or exists.returncode:
        raise RuntimeError(f"{row['task_id']} 无法验证登记的初始快照 {baseline}")
    reset = git("reset", "--hard", baseline)
    clean = git("clean", "-ffdx")
    status = git("status", "--porcelain", "--untracked-files=all")
    head = git("rev-parse", "HEAD")
    if (
        reset.returncode or clean.returncode or status.returncode or status.stdout.strip()
        or head.returncode or head.stdout.strip().lower() != baseline.lower()
    ):
        raise RuntimeError(f"{row['task_id']} 恢复后工作区不是干净初始快照")


def run_one(
    db: Path,
    row: sqlite3.Row,
    env_file: Path,
    data_root: Path,
    image: str,
    cpus: float,
    memory: str,
    timeout: int,
) -> tuple[str, int, str]:
    task_id = str(row["task_id"])
    batch = Path(str(row["folder_path"])).parent.name
    run_id = f"docker-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    task_root = data_root / batch / task_id
    task_root.mkdir(parents=True, exist_ok=True)
    log_path = task_root / f"{run_id}.log"
    attempt_root = task_root / run_id
    trajectory_root = attempt_root / "claude"
    trajectory_root.mkdir(parents=True, exist_ok=True)
    worker_env = task_root / f".{run_id}.env"
    container_name = f"ccusr-{task_id}-{secrets.token_hex(2)}"
    with connect(db) as connection:
        timestamp = now()
        connection.execute(
            "INSERT INTO runs(question_id,batch_run_id,launched_at,codex_version,"
            "relay_provider,relay_host,relay_wire_api,model,harness,harness_version,"
            "status,started_at,log_path,trajectory_root,heartbeat_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], run_id, timestamp, "", "", "", "", "", "Claude Code", "", "running", timestamp, str(log_path), str(trajectory_root), timestamp),
        )
        connection.execute(
            "UPDATE runs SET container_id=? WHERE batch_run_id=? AND question_id=?",
            (container_name, run_id, row["id"]),
        )
        connection.execute("UPDATE questions SET status='running',updated_at=? WHERE id=?", (timestamp, row["id"]))
        connection.commit()

    env_values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_values[key.strip()] = value.strip().strip("'\"")
    required = ("CC_SWITCH_BASE_URL", "CC_SWITCH_API_KEY", "CC_SWITCH_MODEL")
    missing = [key for key in required if not env_values.get(key)]
    if missing:
        message = "missing worker configuration: " + ", ".join(missing)
        with connect(db) as connection:
            connection.execute(
                "UPDATE runs SET status='failed',finished_at=?,exit_code=78,error_message=? "
                "WHERE batch_run_id=? AND question_id=?",
                (now(), message, run_id, row["id"]),
            )
            connection.execute("UPDATE questions SET status='approved',updated_at=? WHERE id=?", (now(), row["id"]))
            connection.commit()
        raise ValueError(message)

    def env_line(name: str, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} contains a newline")
        return f"{name}={value}\n"

    worker_env.write_text(
        env_line("ANTHROPIC_BASE_URL", env_values["CC_SWITCH_BASE_URL"])
        + env_line("ANTHROPIC_AUTH_TOKEN", env_values["CC_SWITCH_API_KEY"])
        + env_line("ANTHROPIC_MODEL", env_values["CC_SWITCH_MODEL"])
        + "CI=1\nCLAUDE_CONFIG_DIR=/state/claude\nHOME=/state/home\n",
        encoding="utf-8",
    )
    worker_env.chmod(0o600)
    home_root = attempt_root / "home"
    home_root.mkdir(exist_ok=True)
    command = [
        "docker", "run", "--rm", "--name", container_name,
        "--cpus", str(cpus), "--memory", memory, "--pids-limit", "512",
        "--env-file", str(worker_env.resolve()),
        "-v", f"{Path(row['folder_path']).resolve()}:/workspace",
        "-v", f"{trajectory_root.resolve()}:/state/claude",
        "-v", f"{home_root.resolve()}:/state/home",
        "-w", "/workspace", image,
        "claude", "--print", "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions", "--permission-prompts", "none",
        str(row["prompt"]),
    ]
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        code = result.returncode
        status = "succeeded" if code == 0 else "failed"
        error = "" if code == 0 else f"docker worker exited with code {code}"
    except subprocess.TimeoutExpired:
        code, status, error = -9, "timeout", f"worker exceeded {timeout}s"
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        code, status, error = 127, "failed", f"cannot start docker worker: {exc}"
    finally:
        worker_env.unlink(missing_ok=True)
    finished = now()
    with connect(db) as connection:
        connection.execute(
            "UPDATE runs SET status=?,finished_at=?,exit_code=?,error_message=?,heartbeat_at=? "
            "WHERE batch_run_id=? AND question_id=?",
            (status, finished, code, error, finished, run_id, row["id"]),
        )
        connection.execute(
            "UPDATE questions SET status=?,updated_at=? WHERE id=?",
            ("completed" if status == "succeeded" else "approved", finished, row["id"]),
        )
        connection.commit()
    return task_id, code, f"{status} in {time.monotonic()-started:.1f}s ({log_path})"


def failed_attempts(db: Path, question_id: int) -> int:
    with connect(db) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM runs WHERE question_id=? AND status IN ('failed','timeout')",
            (question_id,),
        ).fetchone()[0])


def has_unsuccessful_attempt(db: Path, question_id: int) -> bool:
    with connect(db) as connection:
        return connection.execute(
            "SELECT 1 FROM runs WHERE question_id=? "
            "AND status IN ('failed','timeout','interrupted') LIMIT 1",
            (question_id,),
        ).fetchone() is not None


def block_question(db: Path, question_id: int) -> None:
    with connect(db) as connection:
        connection.execute(
            "UPDATE questions SET status='blocked',updated_at=? WHERE id=?",
            (now(), question_id),
        )
        connection.commit()


def run_with_retries(
    db: Path,
    row: sqlite3.Row,
    env_file: Path,
    data_root: Path,
    image: str,
    cpus: float,
    memory: str,
    timeout: int,
    max_attempts: int,
) -> tuple[str, int, str]:
    question_id = int(row["id"])
    task_id = str(row["task_id"])
    last_message = ""
    attempts = failed_attempts(db, question_id)
    restore_before_run = has_unsuccessful_attempt(db, question_id)
    while attempts < max_attempts:
        if restore_before_run:
            try:
                restore_question_workspace(row)
            except Exception as exc:
                block_question(db, question_id)
                return task_id, 1, f"blocked: retry baseline restore failed: {exc}"
        try:
            task_id, code, last_message = run_one(
                db, row, env_file, data_root, image, cpus, memory, timeout,
            )
        except Exception as exc:
            code = 1
            last_message = str(exc)
        if code == 0:
            return task_id, code, last_message
        attempts = max(attempts + 1, failed_attempts(db, question_id))
        restore_before_run = True
    block_question(db, question_id)
    return task_id, 1, f"blocked after {max_attempts} attempts: {last_message}"


def codex_command(codex: str, prompt: str) -> list[str]:
    return [
        codex, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check", "-C", str(PROJECT_ROOT), prompt,
    ]


def run_codex(codex: str, prompt: str, log_path: Path, timeout: int) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now()}] Codex task started\n")
        log.flush()
        try:
            result = subprocess.run(
                codex_command(codex, prompt), cwd=PROJECT_ROOT,
                stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                check=False, env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            log.write(f"[{now()}] Codex task timed out after {timeout}s\n")
            return 124
        log.write(f"[{now()}] Codex task exit={result.returncode}\n")
        return int(result.returncode)


def record_counts(db: Path, question_id: int) -> tuple[int, int]:
    with connect(db) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN delivery_qc_passed=1 AND delivery_qc_note='质检通过' THEN 1 ELSE 0 END) AS passed "
            "FROM records WHERE question_id=?",
            (question_id,),
        ).fetchone()
    return int(row["total"] or 0), int(row["passed"] or 0)


def delivery_needed(db: Path, row: sqlite3.Row) -> bool:
    with connect(db) as connection:
        succeeded = connection.execute(
            "SELECT 1 FROM runs WHERE question_id=? AND status='succeeded' LIMIT 1",
            (row["id"],),
        ).fetchone()
    total, passed = record_counts(db, int(row["id"]))
    return succeeded is not None and (total == 0 or passed != total)


def deliver_one(
    db: Path, row: sqlite3.Row, codex: str, data_root: Path, timeout: int,
) -> tuple[str, int, str]:
    task_id = str(row["task_id"])
    number = int(row["question_no"])
    batch = Path(str(row["folder_path"])).parent.name
    log_root = data_root / batch / task_id
    total, _passed = record_counts(db, int(row["id"]))
    if total == 0:
        prompt = (
            f"使用 $cc-usr-delivery-producer 只处理批次 {batch} 第 {number} 题。"
            f"原始 Claude JSONL 根目录为 {data_root.resolve()}。读取真实轨迹、SQLite、初始快照、Git diff 和实际产物，"
            "按项目规范为全部有效轮次生成完整交付记录并写入 SQLite；不得修改题目代码、轨迹或仓库，"
            "不得处理其他题目，不得执行交付质检或导出。完成后停止。"
        )
        code = run_codex(codex, prompt, log_root / "delivery-producer.log", timeout)
        total, _passed = record_counts(db, int(row["id"]))
        if code or total == 0:
            return task_id, code or 1, "delivery producer did not create records"

    total, passed = record_counts(db, int(row["id"]))
    if passed != total:
        prompt = (
            f"使用 $cc-usr-delivery-qc 只质检批次 {batch} 第 {number} 题的全部交付记录。"
            f"原始 Claude JSONL 根目录为 {data_root.resolve()}。依据可核验证据修正不合规字段，"
            f"显式使用 --select {number} 重新验证并执行 --finalize；不得修改目标模型代码，不得处理其他题目，"
            "不要导出 Excel。完成后停止。"
        )
        code = run_codex(codex, prompt, log_root / "delivery-qc.log", timeout)
        total, passed = record_counts(db, int(row["id"]))
        if code or total == 0 or passed != total:
            return task_id, code or 1, f"delivery QC incomplete ({passed}/{total})"
    return task_id, 0, f"delivery passed ({passed}/{total})"


def finalize_batch(
    db: Path, batch: str, codex: str, data_root: Path, timeout: int,
) -> tuple[int, str]:
    with connect(db) as connection:
        batch_row = connection.execute(
            "SELECT id,folder_path,status FROM batches WHERE name=?", (batch,),
        ).fetchone()
        if batch_row is None:
            return 1, "batch does not exist"
        states = connection.execute(
            "SELECT q.id,q.question_no,q.status,"
            "EXISTS(SELECT 1 FROM runs x WHERE x.question_id=q.id AND x.status='succeeded') AS model_succeeded,"
            "COUNT(r.id) AS record_count,"
            "SUM(CASE WHEN r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过' THEN 1 ELSE 0 END) AS passed_count "
            "FROM questions q LEFT JOIN records r ON r.question_id=q.id "
            "WHERE q.batch_id=? GROUP BY q.id ORDER BY q.question_no",
            (batch_row["id"],),
        ).fetchall()
    if not states:
        with connect(db) as connection:
            connection.execute(
                "UPDATE batches SET status='failed',updated_at=? WHERE name=?", (now(), batch),
            )
            connection.commit()
        return 0, "batch failed because it has no questions"
    eligible = [
        int(row["question_no"]) for row in states
        if row["status"] == "completed" and bool(row["model_succeeded"])
        and int(row["record_count"] or 0) > 0
        and int(row["record_count"] or 0) == int(row["passed_count"] or 0)
    ]
    terminal = all(
        int(row["question_no"]) in eligible or row["status"] == "blocked"
        for row in states
    )
    if not terminal:
        return 0, "batch still has pending questions"
    if not eligible:
        with connect(db) as connection:
            connection.execute(
                "UPDATE batches SET status='failed',updated_at=? WHERE name=?", (now(), batch),
            )
            connection.commit()
        return 0, "batch failed with no deliverable questions"

    selection = ",".join(str(number) for number in eligible)
    folder = Path(str(batch_row["folder_path"]))
    before = set(folder.glob("CC_Codex*.xlsx"))
    trajectories_before = set(folder.glob("轨迹_*.jsonl"))
    prompt = (
        f"使用 $cc-usr-excel-exporter 只导出批次 {batch} 第 {selection} 题中已经通过交付质检的完整记录，"
        f"显式使用 --select {selection} 和 --claude-root {data_root.resolve()}，逐字节复制原始 JSONL，"
        "不得修改评分、记录或目标模型产物。完成后报告输出路径并停止。"
    )
    code = run_codex(codex, prompt, data_root / batch / "export.log", timeout)
    new_trajectories = set(folder.glob("轨迹_*.jsonl")) - trajectories_before
    if code or not set(folder.glob("CC_Codex*.xlsx")) - before or len(new_trajectories) < len(eligible):
        return code or 1, "export did not create a complete delivery package"
    status = "completed" if len(eligible) == len(states) else "partial"
    with connect(db) as connection:
        connection.execute(
            "UPDATE batches SET status=?,updated_at=? WHERE name=?", (status, now(), batch),
        )
        connection.commit()
    return 0, f"batch {status}: exported questions {selection}"


def run_delivery_pipeline(args: argparse.Namespace) -> int:
    database = args.db.resolve()
    with connect(database) as connection:
        rows = question_rows(connection, args.batch)
        candidates = []
        for row in rows:
            succeeded = connection.execute(
                "SELECT 1 FROM runs WHERE question_id=? AND status='succeeded' LIMIT 1",
                (row["id"],),
            ).fetchone() is not None
            attempts = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE question_id=? AND status IN ('failed','timeout')",
                (row["id"],),
            ).fetchone()[0]
            if succeeded:
                connection.execute(
                    "UPDATE questions SET status='completed',updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
            elif ready(row) and attempts < args.max_attempts:
                candidates.append(row)
            elif row["status"] != "blocked":
                connection.execute(
                    "UPDATE questions SET status='blocked',updated_at=? WHERE id=?", (now(), row["id"]),
                )
        connection.commit()
        rows = question_rows(connection, args.batch)

    errors: list[str] = []
    scheduled_delivery: set[int] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as model_pool, concurrent.futures.ThreadPoolExecutor(
        max_workers=args.codex_concurrency,
    ) as delivery_pool:
        delivery_futures: dict[concurrent.futures.Future, sqlite3.Row] = {}

        def schedule_delivery(row: sqlite3.Row) -> None:
            question_id = int(row["id"])
            if question_id in scheduled_delivery or not delivery_needed(database, row):
                return
            scheduled_delivery.add(question_id)
            future = delivery_pool.submit(
                deliver_one, database, row, args.codex, args.data_root.resolve(), args.agent_timeout,
            )
            delivery_futures[future] = row

        for row in rows:
            schedule_delivery(row)
        model_futures = {
            model_pool.submit(
                run_with_retries, database, row, args.env_file.resolve(), args.data_root.resolve(),
                args.image, args.cpus, args.memory, args.timeout, args.max_attempts,
            ): row
            for row in candidates
        }
        for future in concurrent.futures.as_completed(model_futures):
            row = model_futures[future]
            try:
                task_id, code, message = future.result()
            except Exception as exc:
                task_id, code, message = str(row["task_id"]), 1, str(exc)
            print(f"{task_id}: {message}", flush=True)
            if code:
                errors.append(f"{task_id}: {message}")
            else:
                schedule_delivery(row)

        for future in concurrent.futures.as_completed(delivery_futures):
            row = delivery_futures[future]
            try:
                task_id, code, message = future.result()
            except Exception as exc:
                task_id, code, message = str(row["task_id"]), 1, str(exc)
            print(f"{task_id}: {message}", flush=True)
            if code:
                errors.append(f"{task_id}: {message}")

    code, message = finalize_batch(
        database, args.batch, args.codex, args.data_root.resolve(), args.agent_timeout,
    )
    print(message, flush=True)
    if code:
        errors.append(message)
    with connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM batches WHERE name=?", (args.batch,),
        ).fetchone()
    terminal = status is not None and status["status"] in {"completed", "partial", "failed"}
    return 0 if terminal else (1 if errors else 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--data-root", type=Path, default=Path("runs"))
    parser.add_argument("--image", default="ccusr-claude-worker:local")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--cpus", type=float, default=1.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--deliver", action="store_true")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--codex-concurrency", type=int, default=1)
    parser.add_argument("--agent-timeout", type=int, default=3600)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.codex_concurrency < 1:
        parser.error("--codex-concurrency must be positive")
    if args.deliver:
        return run_delivery_pipeline(args)
    while True:
        with connect(args.db.resolve()) as connection:
            candidates = [row for row in question_rows(connection, args.batch) if ready(row)]
            rows = []
            for row in candidates:
                attempts = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE question_id=? AND status IN ('failed','timeout')",
                    (row["id"],),
                ).fetchone()[0]
                if attempts < args.max_attempts:
                    rows.append(row)
                else:
                    connection.execute(
                        "UPDATE questions SET status='blocked',updated_at=? WHERE id=?",
                        (now(), row["id"]),
                    )
            connection.commit()
            rows = rows[: args.concurrency]
        if not rows:
            print("No runnable READY questions available.", flush=True)
            if not args.loop:
                return 0
            time.sleep(args.poll_seconds)
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(
                run_with_retries, args.db.resolve(), row, args.env_file.resolve(),
                args.data_root.resolve(), args.image, args.cpus, args.memory,
                args.timeout, args.max_attempts,
            ) for row in rows]
            for future in concurrent.futures.as_completed(futures):
                task_id, code, message = future.result()
                print(f"{task_id}: {message}", flush=True)
                if code != 0:
                    print(f"{task_id}: failed", flush=True)
        if not args.loop:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
