#!/usr/bin/env python3
"""Small, restartable Docker-backed Claude worker scheduler.

This is intentionally limited to the model-run stage. The delivery stages are
kept as explicit commands until their agent credentials are configured on the
server; a completed worker is never mistaken for a delivered record.
"""

from __future__ import annotations

import argparse
import concurrent.futures
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

from tools.batch_pipeline import connect, prompt_hash, question_rows


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ready(row: sqlite3.Row) -> bool:
    return bool(
        row["mechanical_qc"] == "pass"
        and row["qc_decision"] == "pass"
        and row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        and row["status"] == "approved"
    )


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
    trajectory_root = task_root / "claude"
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
    home_root = task_root / "home"
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
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
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
            futures = [pool.submit(run_one, args.db.resolve(), row, args.env_file.resolve(), args.data_root.resolve(), args.image, args.cpus, args.memory, args.timeout) for row in rows]
            for future in concurrent.futures.as_completed(futures):
                task_id, code, message = future.result()
                print(f"{task_id}: {message}", flush=True)
                if code != 0:
                    print(f"{task_id}: failed", flush=True)
        if not args.loop:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
