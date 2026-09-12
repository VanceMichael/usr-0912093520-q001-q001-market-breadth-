#!/usr/bin/env python3
"""Run restartable Claude workers and stream successful tasks into delivery."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import random
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import SNAPSHOT_RE, connect, prompt_hash, question_rows
from tools.capacity import adaptive_model_limit, detect_capacity
from tools.text_encoding import read_portable_text
from tools.trajectory_gate import TrajectoryGateError, validate_effective_trajectory
from tools.run_supervisor import RunMetadataError, register_run_metadata, write_supervisor_state
from tools.solo2_service import submit_records


DEFAULT_HEARTBEAT_SECONDS = 5
DEFAULT_START_TIMEOUT = 300
DEFAULT_STALLED_TIMEOUT = 900
DEFAULT_GATEWAY_MAX_ATTEMPTS = 3
DEFAULT_GATEWAY_BACKOFF_BASE = 30
DEFAULT_GATEWAY_BACKOFF_MAX = 300
DEFAULT_GATEWAY_CIRCUIT_THRESHOLD = 2
DEFAULT_GATEWAY_CIRCUIT_WINDOW = 120
DEFAULT_GATEWAY_CIRCUIT_COOLDOWN = 180

TRANSIENT_GATEWAY_RE = re.compile(
    r"(?i)(?:\b(?:429|500|502|503|504)\b|gateway[ -]?(?:time-?out|error)|"
    r"server error mid-response|ECONNRESET|ETIMEDOUT|ENOTFOUND|"
    r"connection (?:reset|timed out)|temporarily unavailable|rate limit)"
)
PERMANENT_AUTH_RE = re.compile(
    r"(?i)(?:\b(?:401|403)\b|invalid (?:api key|auth(?:entication)? token)|"
    r"unauthorized|authentication failed|permission denied.*(?:api|model))"
)
MODEL_CONFIG_RE = re.compile(
    r"(?i)(?:unsupported model|invalid model|model (?:is )?not found|"
    r"model .* does not exist|unknown model provider)"
)
UNRECOGNIZED_MODEL_WARNING = "[claude-code:unrecognized_model]"
HARNESS_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_WORKER_VERSION_LOCK = threading.Lock()
_WORKER_VERSIONS: dict[str, str] = {}


@dataclass(frozen=True)
class FailureClassification:
    kind: str
    retryable: bool


class GatewayCircuitBreaker:
    """Coordinate transient provider failures across concurrent workers."""

    def __init__(self, threshold: int, window_seconds: int, cooldown_seconds: int) -> None:
        self.threshold = max(1, threshold)
        self.window_seconds = max(1, window_seconds)
        self.cooldown_seconds = max(1, cooldown_seconds)
        self._failures: deque[float] = deque()
        self._open_until = 0.0
        self._half_open_in_flight = False
        self._condition = threading.Condition()

    def wait_until_allowed(self) -> None:
        while True:
            with self._condition:
                current = time.monotonic()
                if not self._open_until:
                    return
                if current >= self._open_until and not self._half_open_in_flight:
                    self._half_open_in_flight = True
                    return
                delay = max(0.1, self._open_until - current) if current < self._open_until else 1.0
                self._condition.wait(timeout=min(delay, 5.0))

    def record_transient_failure(self) -> None:
        with self._condition:
            current = time.monotonic()
            while self._failures and current - self._failures[0] > self.window_seconds:
                self._failures.popleft()
            self._failures.append(current)
            if self._half_open_in_flight or len(self._failures) >= self.threshold:
                self._open_until = current + self.cooldown_seconds
                self._half_open_in_flight = False
            self._condition.notify_all()

    def record_recovery(self) -> None:
        with self._condition:
            self._failures.clear()
            self._open_until = 0.0
            self._half_open_in_flight = False
            self._condition.notify_all()

    def snapshot(self) -> dict[str, float | int | bool]:
        with self._condition:
            return {
                "failure_count": len(self._failures),
                "open": self._open_until > time.monotonic(),
                "open_until": self._open_until,
                "half_open_in_flight": self._half_open_in_flight,
            }


def worker_harness_version(image: str) -> str:
    """Read and cache the Claude Code version embedded in a worker image."""
    with _WORKER_VERSION_LOCK:
        cached = _WORKER_VERSIONS.get(image)
        if cached:
            return cached
        result = subprocess.run(
            ["docker", "run", "--rm", image, "claude", "--version"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30, check=False,
        )
        match = HARNESS_VERSION_RE.search(result.stdout or "")
        if result.returncode or match is None:
            detail = (result.stdout or "").strip()[-500:]
            raise RuntimeError(f"cannot read Claude Code version from worker image: {detail}")
        version = match.group(0)
        _WORKER_VERSIONS[image] = version
        return version


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ready(row: sqlite3.Row) -> bool:
    return bool(
        row["mechanical_qc"] == "pass"
        and row["qc_decision"] == "pass"
        and row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        and row["status"] == "approved"
        and not bool(row["maintenance_mode"])
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


def activity_signature(log_path: Path, trajectory_root: Path) -> tuple[int, int, int]:
    """Return a cheap, content-free signature for worker output activity."""
    newest = 0
    total_size = 0
    file_count = 0
    paths = [log_path]
    try:
        paths.extend(path for path in trajectory_root.rglob("*") if path.is_file())
    except OSError:
        pass
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime_ns)
        total_size += stat.st_size
        file_count += 1
    return newest, total_size, file_count


def claude_task_state(trajectory_root: Path) -> str:
    """Read only structured Claude task status, never task descriptions."""
    observed = False
    unreadable = False
    try:
        paths = list((trajectory_root / "tasks").glob("*/*.json"))
    except OSError:
        return "unknown"
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            unreadable = True
            continue
        if not isinstance(payload, dict):
            unreadable = True
            continue
        status = str(payload.get("status") or payload.get("state") or "").casefold()
        if not status:
            unreadable = True
            continue
        observed = True
        if status not in {"completed", "complete", "done", "cancelled", "canceled"}:
            return "incomplete"
    if unreadable:
        return "unknown"
    return "complete" if observed else "unknown"


def stream_result_details(log_path: Path) -> tuple[str, str]:
    """Return final stream state plus provider diagnostics outside tool payloads."""
    found = "missing"
    diagnostics: list[str] = []
    try:
        with log_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stripped = line.strip()
                    if stripped:
                        diagnostics.append(stripped[-1000:])
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "result":
                    is_error = bool(event.get("is_error"))
                    found = "failure" if is_error else "success"
                    # A successful assistant answer may legitimately mention HTTP
                    # status codes (for example, documenting a 403 response). Only
                    # scan result text for provider diagnostics when the CLI marks
                    # the result as an error; otherwise it can trip auth detection.
                    keys = ("error", "result", "subtype") if is_error else ("error",)
                    for key in keys:
                        value = event.get(key)
                        if isinstance(value, str) and value.strip():
                            diagnostics.append(value[-2000:])
                        elif key == "error" and isinstance(value, (dict, list)):
                            diagnostics.append(json.dumps(value, ensure_ascii=False)[-2000:])
                elif isinstance(event.get("error"), (str, dict, list)):
                    value = event["error"]
                    diagnostics.append(
                        (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[-2000:]
                    )
    except OSError:
        return "missing", ""
    return found, "\n".join(diagnostics[-10:])


def stream_result_state(log_path: Path) -> str:
    """Keep the historical state-only interface for callers and tests."""
    return stream_result_details(log_path)[0]


def classify_failure(status: str, error: str, diagnostics: str = "") -> FailureClassification:
    text = "\n".join(value for value in (error, diagnostics) if value)
    without_catalog_warning = "\n".join(
        line for line in text.splitlines() if UNRECOGNIZED_MODEL_WARNING not in line
    )
    if status == "timeout":
        return FailureClassification("model_timeout", True)
    if "effective trajectory gate rejected run" in error:
        return FailureClassification("trajectory_gate", True)
    if "blocked_metadata" in error or "完成元数据" in error or "SessionID" in error:
        return FailureClassification("blocked_metadata", False)
    if PERMANENT_AUTH_RE.search(without_catalog_warning):
        return FailureClassification("permanent_auth", False)
    if MODEL_CONFIG_RE.search(without_catalog_warning):
        return FailureClassification("model_config", False)
    if TRANSIENT_GATEWAY_RE.search(without_catalog_warning):
        return FailureClassification("transient_gateway", True)
    if any(marker in error for marker in (
        "failed before worker launch", "cannot start docker worker", "missing worker configuration",
    )):
        return FailureClassification("environment", False)
    return FailureClassification("worker_failure", True)


def retry_delay_seconds(failure_number: int, base: int, maximum: int) -> int:
    raw = min(maximum, max(1, base) * (3 ** max(0, failure_number - 1)))
    if raw >= maximum:
        return max(1, maximum)
    return max(1, min(maximum, round(raw * random.SystemRandom().uniform(1.0, 1.5))))


def update_run_heartbeat(db: Path, question_id: int, run_id: str) -> None:
    try:
        with closing(connect(db)) as connection:
            connection.execute(
                "UPDATE runs SET heartbeat_at=? WHERE batch_run_id=? AND question_id=?",
                (now(), run_id, question_id),
            )
            connection.commit()
    except sqlite3.Error:
        # A transient SQLite writer conflict must not orphan a live container.
        pass


def stop_worker_container(container_name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )


def monitor_worker(
    process: subprocess.Popen,
    db: Path,
    question_id: int,
    run_id: str,
    container_name: str,
    log_path: Path,
    trajectory_root: Path,
    timeout: int,
    heartbeat_seconds: int,
    start_timeout: int,
    stalled_timeout: int,
    initial_activity: tuple[int, int, int],
    run_directory: Path | None = None,
) -> tuple[int, str, str]:
    """Supervise one Docker worker without depending on terminal screen state."""
    started = time.monotonic()
    last_activity_at = started
    last_activity = initial_activity
    activity_seen = False
    def publish(state: str, error: str = "") -> None:
        if run_directory is None:
            return
        try:
            write_supervisor_state(
                run_directory, state=state, container_id=container_name,
                activity=last_activity, error=error,
            )
        except OSError:
            # The database heartbeat remains authoritative if the optional
            # diagnostic file cannot be written.
            pass

    publish("running")
    while True:
        try:
            code = process.wait(timeout=max(1, heartbeat_seconds))
        except subprocess.TimeoutExpired:
            code = None
        current = time.monotonic()
        signature = activity_signature(log_path, trajectory_root)
        if signature != last_activity:
            activity_seen = True
            last_activity = signature
            last_activity_at = current
        update_run_heartbeat(db, question_id, run_id)
        publish("running")
        if code is not None:
            publish("exited" if code else "idle")
            return int(code), "", ""
        if current - started >= timeout:
            stop_worker_container(container_name)
            process.wait(timeout=30)
            publish("timeout", f"worker exceeded hard timeout of {timeout}s")
            return -9, "timeout", f"worker exceeded hard timeout of {timeout}s"
        if not activity_seen and current - started >= start_timeout:
            stop_worker_container(container_name)
            process.wait(timeout=30)
            publish("timeout", f"worker produced no output within {start_timeout}s")
            return -9, "timeout", f"worker produced no output within {start_timeout}s"
        if activity_seen and current - last_activity_at >= stalled_timeout:
            stop_worker_container(container_name)
            process.wait(timeout=30)
            publish("stalled", f"worker output stalled for {stalled_timeout}s")
            return -9, "timeout", f"worker output stalled for {stalled_timeout}s"


def run_one(
    db: Path,
    row: sqlite3.Row,
    env_file: Path,
    data_root: Path,
    image: str,
    cpus: float,
    memory: str,
    timeout: int,
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    start_timeout: int = DEFAULT_START_TIMEOUT,
    stalled_timeout: int = DEFAULT_STALLED_TIMEOUT,
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
    with closing(connect(db)) as connection:
        timestamp = now()
        connection.execute(
            "INSERT INTO runs(question_id,batch_run_id,launched_at,codex_version,"
            "relay_provider,relay_host,relay_wire_api,model,harness,harness_version,"
            "status,started_at,log_path,trajectory_root,heartbeat_at,container_cwd,operating_system) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"], run_id, timestamp, "", "", "", "", "", "Claude Code", "",
                "running", timestamp, str(log_path), str(trajectory_root), timestamp,
                "/workspace", "MacOS/Linux",
            ),
        )
        connection.execute(
            "UPDATE runs SET container_id=? WHERE batch_run_id=? AND question_id=?",
            (container_name, run_id, row["id"]),
        )
        connection.execute("UPDATE questions SET status='running',updated_at=? WHERE id=?", (timestamp, row["id"]))
        connection.commit()

    def fail_setup(message: str) -> tuple[str, int, str]:
        finished = now()
        classification = classify_failure("failed", f"failed before worker launch: {message}")
        with closing(connect(db)) as connection:
            connection.execute(
                "UPDATE runs SET status='failed',finished_at=?,exit_code=78,error_message=?,"
                "failure_kind=?,retryable=? "
                "WHERE batch_run_id=? AND question_id=?",
                (
                    finished, message, classification.kind, int(classification.retryable),
                    run_id, row["id"],
                ),
            )
            connection.execute(
                "UPDATE questions SET status='approved',updated_at=? WHERE id=? "
                "AND maintenance_mode=0",
                (finished, row["id"]),
            )
            connection.commit()
        worker_env.unlink(missing_ok=True)
        return task_id, 78, f"failed before worker launch: {message}"

    def env_line(name: str, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} contains a newline")
        return f"{name}={value}\n"

    try:
        env_values: dict[str, str] = {}
        for line in read_portable_text(env_file).splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env_values[key.strip()] = value.strip().strip("'\"")
        required = ("CC_SWITCH_BASE_URL", "CC_SWITCH_API_KEY", "CC_SWITCH_MODEL")
        missing = [key for key in required if not env_values.get(key)]
        if missing:
            raise ValueError("missing worker configuration: " + ", ".join(missing))
        harness_version = worker_harness_version(image)
        with closing(connect(db)) as connection:
            connection.execute(
                "UPDATE runs SET model=?,harness_version=?,operating_system=? "
                "WHERE batch_run_id=? AND question_id=?",
                (
                    env_values["CC_SWITCH_MODEL"], harness_version, "MacOS/Linux",
                    run_id, row["id"],
                ),
            )
            connection.commit()
        worker_env.write_text(
            env_line("ANTHROPIC_BASE_URL", env_values["CC_SWITCH_BASE_URL"])
            + env_line("ANTHROPIC_AUTH_TOKEN", env_values["CC_SWITCH_API_KEY"])
            + env_line("ANTHROPIC_MODEL", env_values["CC_SWITCH_MODEL"])
            + "CI=1\nCLAUDE_CONFIG_DIR=/state/claude\nHOME=/state/home\n",
            encoding="utf-8",
        )
        worker_env.chmod(0o600)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return fail_setup(str(exc))
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
        "claude", "--print", "--safe-mode", "--disable-slash-commands",
        "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions", "--permission-prompts", "none",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        str(row["prompt"]),
    ]
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            initial_activity = activity_signature(log_path, trajectory_root)
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT,
            )
            code, status, error = monitor_worker(
                process, db, int(row["id"]), run_id, container_name,
                log_path, trajectory_root, timeout, heartbeat_seconds,
                start_timeout, stalled_timeout, initial_activity, attempt_root,
            )
        if not status:
            result_state, stream_diagnostics = stream_result_details(log_path)
            task_state = claude_task_state(trajectory_root)
            if code != 0:
                status, error = "failed", f"docker worker exited with code {code}"
            elif result_state != "success":
                code, status = 1, "failed"
                error = f"Claude stream ended without a successful result event ({result_state})"
            elif task_state == "incomplete":
                code, status = 1, "failed"
                error = "Claude exited while structured tasks were still incomplete"
            else:
                try:
                    evidence = validate_effective_trajectory(
                        trajectory_root,
                        str(row["prompt"]),
                        Path(str(row["folder_path"])).resolve(strict=True),
                        str(row["local_initial_sha"] or "").strip(),
                    )
                except (OSError, TrajectoryGateError) as exc:
                    code, status = 1, "failed"
                    error = f"effective trajectory gate rejected run: {exc}"
                else:
                    try:
                        evidence_path = getattr(evidence, "path", None)
                        register_run_metadata(
                            db, int(row["id"]), run_id, trajectory_root,
                            session_file=evidence_path if isinstance(evidence_path, Path) else evidence_path,
                            expected_session_id=str(evidence.session_id or ""),
                        )
                    except (OSError, RunMetadataError) as exc:
                        code, status = 1, "failed"
                        error = f"blocked_metadata: {exc}"
                        try:
                            write_supervisor_state(
                                attempt_root, state="blocked_metadata",
                                container_id=container_name, error=error,
                            )
                        except OSError:
                            pass
                    else:
                        status, error = "succeeded", ""
                        try:
                            write_supervisor_state(
                                attempt_root, state="completed",
                                container_id=container_name,
                                activity=activity_signature(log_path, trajectory_root),
                            )
                        except OSError:
                            pass
    except subprocess.TimeoutExpired:
        code, status, error = -9, "timeout", "worker did not stop after container termination"
    except OSError as exc:
        code, status, error = 127, "failed", f"cannot start docker worker: {exc}"
    finally:
        worker_env.unlink(missing_ok=True)
    finished = now()
    classification = (
        FailureClassification("", False)
        if status == "succeeded"
        else classify_failure(status, error, locals().get("stream_diagnostics", ""))
    )
    with closing(connect(db)) as connection:
        question_status = (
            "completed" if status == "succeeded"
            else "blocked" if classification.kind == "blocked_metadata"
            else "approved"
        )
        connection.execute(
            "UPDATE runs SET status=?,finished_at=?,exit_code=?,error_message=?,heartbeat_at=?,"
            "failure_kind=?,retryable=? "
            "WHERE batch_run_id=? AND question_id=?",
            (
                status, finished, code, error, finished, classification.kind,
                int(classification.retryable), run_id, row["id"],
            ),
        )
        connection.execute(
            "UPDATE questions SET status=?,updated_at=? WHERE id=? AND maintenance_mode=0",
            (question_status, finished, row["id"]),
        )
        connection.commit()
    return task_id, code, f"{status} in {time.monotonic()-started:.1f}s ({log_path})"


def failed_attempts(db: Path, question_id: int) -> int:
    """Count model/content attempts; transient gateway failures have a separate budget."""
    with closing(connect(db)) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM runs WHERE question_id=? AND status IN ('failed','timeout') "
            "AND COALESCE(failure_kind,'')!='transient_gateway'",
            (question_id,),
        ).fetchone()[0])


def gateway_failed_attempts(db: Path, question_id: int) -> int:
    with closing(connect(db)) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM runs WHERE question_id=? AND status='failed' "
            "AND failure_kind='transient_gateway'",
            (question_id,),
        ).fetchone()[0])


def latest_failure(db: Path, question_id: int, fallback: str) -> FailureClassification:
    with closing(connect(db)) as connection:
        row = connection.execute(
            "SELECT status,error_message,failure_kind,retryable FROM runs "
            "WHERE question_id=? AND status IN ('failed','timeout') ORDER BY id DESC LIMIT 1",
            (question_id,),
        ).fetchone()
    if row is None:
        return classify_failure("failed", fallback)
    kind = str(row["failure_kind"] or "")
    if kind:
        return FailureClassification(kind, bool(row["retryable"]))
    return classify_failure(str(row["status"]), str(row["error_message"] or fallback))


def record_retry_delay(db: Path, question_id: int, seconds: int) -> None:
    with closing(connect(db)) as connection:
        connection.execute(
            "UPDATE runs SET retry_delay_seconds=? WHERE id=("
            "SELECT id FROM runs WHERE question_id=? ORDER BY id DESC LIMIT 1)",
            (seconds, question_id),
        )
        connection.commit()


def has_unsuccessful_attempt(db: Path, question_id: int) -> bool:
    with closing(connect(db)) as connection:
        return connection.execute(
            "SELECT 1 FROM runs WHERE question_id=? "
            "AND status IN ('failed','timeout','interrupted') LIMIT 1",
            (question_id,),
        ).fetchone() is not None


def block_question(db: Path, question_id: int) -> None:
    with closing(connect(db)) as connection:
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
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    start_timeout: int = DEFAULT_START_TIMEOUT,
    stalled_timeout: int = DEFAULT_STALLED_TIMEOUT,
    gateway_max_attempts: int = DEFAULT_GATEWAY_MAX_ATTEMPTS,
    gateway_backoff_base: int = DEFAULT_GATEWAY_BACKOFF_BASE,
    gateway_backoff_max: int = DEFAULT_GATEWAY_BACKOFF_MAX,
    circuit_breaker: GatewayCircuitBreaker | None = None,
) -> tuple[str, int, str]:
    question_id = int(row["id"])
    task_id = str(row["task_id"])
    last_message = ""
    model_attempts = failed_attempts(db, question_id)
    gateway_attempts = gateway_failed_attempts(db, question_id)
    restore_before_run = has_unsuccessful_attempt(db, question_id)
    while model_attempts < max_attempts and gateway_attempts < gateway_max_attempts:
        if circuit_breaker is not None:
            circuit_breaker.wait_until_allowed()
        if restore_before_run:
            try:
                restore_question_workspace(row)
            except Exception as exc:
                block_question(db, question_id)
                return task_id, 1, f"blocked: retry baseline restore failed: {exc}"
        try:
            raised = False
            task_id, code, last_message = run_one(
                db, row, env_file, data_root, image, cpus, memory, timeout,
                heartbeat_seconds, start_timeout, stalled_timeout,
            )
        except Exception as exc:
            raised = True
            code = 1
            last_message = str(exc)
        if code == 0:
            if circuit_breaker is not None:
                circuit_breaker.record_recovery()
            return task_id, code, last_message
        classification = (
            classify_failure("failed", last_message)
            if raised else latest_failure(db, question_id, last_message)
        )
        if classification.kind == "transient_gateway":
            gateway_attempts = max(gateway_attempts + 1, gateway_failed_attempts(db, question_id))
            if circuit_breaker is not None:
                circuit_breaker.record_transient_failure()
            if gateway_attempts < gateway_max_attempts:
                delay = retry_delay_seconds(
                    gateway_attempts, gateway_backoff_base, gateway_backoff_max,
                )
                record_retry_delay(db, question_id, delay)
                time.sleep(delay)
        else:
            if classification.kind == "blocked_metadata":
                block_question(db, question_id)
                return task_id, 1, f"blocked_metadata: {last_message}"
            model_attempts = max(model_attempts + 1, failed_attempts(db, question_id))
            if circuit_breaker is not None:
                circuit_breaker.record_recovery()
            if classification.kind in {"permanent_auth", "model_config", "environment"}:
                model_attempts = max_attempts
        restore_before_run = True
    block_question(db, question_id)
    if gateway_attempts >= gateway_max_attempts:
        return task_id, 1, f"blocked after {gateway_max_attempts} transient gateway attempts: {last_message}"
    return task_id, 1, f"blocked after {max_attempts} model attempts: {last_message}"


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
    with closing(connect(db)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN delivery_qc_passed=1 AND delivery_qc_note='质检通过' THEN 1 ELSE 0 END) AS passed "
            "FROM records WHERE question_id=?",
            (question_id,),
        ).fetchone()
    return int(row["total"] or 0), int(row["passed"] or 0)


def delivery_needed(db: Path, row: sqlite3.Row) -> bool:
    with closing(connect(db)) as connection:
        succeeded = connection.execute(
            "SELECT 1 FROM runs WHERE question_id=? AND status='succeeded' LIMIT 1",
            (row["id"],),
        ).fetchone()
    total, passed = record_counts(db, int(row["id"]))
    return succeeded is not None and (total == 0 or passed != total)


def release_question_lease(db: Path, question_id: int, kind: str, owner: str) -> None:
    if kind not in {"model", "delivery"}:
        raise ValueError("unknown lease kind")
    with closing(connect(db)) as connection:
        connection.execute(
            f"UPDATE questions SET {kind}_lease_owner='',{kind}_lease_expires_at='' "
            f"WHERE id=? AND {kind}_lease_owner=?",
            (question_id, owner),
        )
        connection.commit()


def clear_question_leases(db: Path) -> None:
    with closing(connect(db)) as connection:
        connection.execute(
            "UPDATE questions SET model_lease_owner='',model_lease_expires_at='',"
            "delivery_lease_owner='',delivery_lease_expires_at=''"
        )
        connection.commit()


def _claim_rows(
    db: Path,
    owner: str,
    kind: str,
    limit: int,
    lease_seconds: int,
    max_attempts: int = 2,
    gateway_max_attempts: int = DEFAULT_GATEWAY_MAX_ATTEMPTS,
) -> list[sqlite3.Row]:
    if limit <= 0:
        return []
    if kind not in {"model", "delivery"}:
        raise ValueError("unknown lease kind")
    current = now()
    expires = (datetime.now().astimezone() + timedelta(seconds=max(60, lease_seconds))).isoformat(
        timespec="seconds"
    )
    with closing(connect(db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if kind == "model":
            rows = connection.execute(
                "SELECT q.* FROM questions q JOIN batches b ON b.id=q.batch_id "
                "WHERE b.status NOT IN ('completed','partial','failed') AND q.maintenance_mode=0 "
                "AND q.status='approved' AND q.mechanical_qc='pass' AND q.qc_decision='pass' "
                "AND q.qc_prompt_sha256=q.prompt_sha256 "
                "AND (q.model_lease_owner='' OR julianday(q.model_lease_expires_at)<julianday(?)) "
                "AND NOT EXISTS(SELECT 1 FROM runs s WHERE s.question_id=q.id AND s.status='succeeded') "
                "AND (SELECT COUNT(*) FROM runs f WHERE f.question_id=q.id "
                "AND f.status IN ('failed','timeout') AND COALESCE(f.failure_kind,'')!='transient_gateway')<? "
                "AND (SELECT COUNT(*) FROM runs g WHERE g.question_id=q.id "
                "AND g.status='failed' AND g.failure_kind='transient_gateway')<? "
                "ORDER BY b.created_at,q.question_no LIMIT ?",
                (current, max_attempts, gateway_max_attempts, limit * 4),
            ).fetchall()
            rows = [row for row in rows if ready(row)][:limit]
        else:
            candidates = connection.execute(
                "SELECT q.* FROM questions q JOIN batches b ON b.id=q.batch_id "
                "WHERE b.status NOT IN ('completed','partial','failed') AND q.maintenance_mode=0 "
                "AND q.status='completed' "
                "AND (q.delivery_lease_owner='' OR julianday(q.delivery_lease_expires_at)<julianday(?)) "
                "AND EXISTS(SELECT 1 FROM runs s WHERE s.question_id=q.id AND s.status='succeeded') "
                "AND ((SELECT COUNT(*) FROM records r WHERE r.question_id=q.id)=0 "
                "OR (SELECT COUNT(*) FROM records r WHERE r.question_id=q.id "
                "AND r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过') "
                "!=(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id)) "
                "ORDER BY b.created_at,q.question_no LIMIT ?",
                (current, limit * 8),
            ).fetchall()
            rows = list(candidates[:limit])
        claimed: list[int] = []
        for row in rows:
            cursor = connection.execute(
                f"UPDATE questions SET {kind}_lease_owner=?,{kind}_lease_expires_at=? "
                f"WHERE id=? AND ({kind}_lease_owner='' "
                f"OR julianday({kind}_lease_expires_at)<julianday(?))",
                (owner, expires, row["id"], current),
            )
            if cursor.rowcount:
                claimed.append(int(row["id"]))
        connection.commit()
        if not claimed:
            return []
        placeholders = ",".join("?" for _value in claimed)
        return connection.execute(
            f"SELECT q.* FROM questions q JOIN batches b ON b.id=q.batch_id "
            f"WHERE q.id IN ({placeholders}) ORDER BY b.created_at,q.question_no",
            claimed,
        ).fetchall()


def active_batch_names(db: Path) -> list[str]:
    with closing(connect(db)) as connection:
        return [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM batches WHERE status NOT IN ('completed','partial','failed') "
                "ORDER BY created_at,id"
            )
        ]


def solo2_pending_available(db: Path, max_attempts: int) -> bool:
    with closing(connect(db)) as connection:
        return connection.execute(
            "SELECT 1 FROM records r LEFT JOIN solo2_submissions s ON s.record_id=r.record_id "
            "WHERE r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过' "
            "AND r.evidence_gate_passed=1 AND r.history_gate_passed=1 "
            "AND r.human_qc_approved=1 "
            "AND r.review_method='human' "
            "AND COALESCE(s.status,'') NOT IN ('succeeded','auth_blocked','schema_blocked') "
            "AND COALESCE(s.status,'')!='remote_pending_fix' "
            "AND (COALESCE(s.status,'')!='submitting' OR "
            "julianday(COALESCE(s.lease_expires_at,''))<julianday('now')) "
            "AND COALESCE(s.attempt_count,0)<? "
            "AND (s.status IS NULL OR s.status!='retry_wait' OR "
            "julianday(s.updated_at)<=julianday('now')-(30 * (1 << MIN(COALESCE(s.attempt_count,1)-1,5)))/86400.0) "
            "LIMIT 1",
            (max_attempts,),
        ).fetchone() is not None


def batch_ready_to_finalize(db: Path, batch: str) -> bool:
    with closing(connect(db)) as connection:
        rows = connection.execute(
            "SELECT q.status,q.maintenance_mode,COUNT(r.id) AS records,"
            "SUM(CASE WHEN r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过' "
            "AND r.evidence_gate_passed=1 AND r.history_gate_passed=1 "
            "AND r.human_qc_approved=1 THEN 1 ELSE 0 END) passed "
            "FROM questions q JOIN batches b ON b.id=q.batch_id "
            "LEFT JOIN records r ON r.question_id=q.id WHERE b.name=? GROUP BY q.id",
            (batch,),
        ).fetchall()
    return bool(rows) and all(
        not bool(row["maintenance_mode"]) and (row["status"] == "blocked"
        or (
            row["status"] == "completed"
            and int(row["records"] or 0) > 0
            and int(row["records"] or 0) == int(row["passed"] or 0)
        ))
        for row in rows
    )


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
            "按项目规范为全部有效轮次生成完整交付记录并写入 SQLite。功能成功必须由目标轮次内的真实测试输出、"
            "服务交互或其他运行证据支撑，静态阅读、文件存在和最终回复不能单独证明成功；测试失败仍要保留记录并如实降分。"
            "五维描述必须各自为单段、至少 45 个汉字且不超过 420 个字符。不得修改题目代码、轨迹或仓库，"
            "不得处理其他题目；完成生产后停止，保持质检与最终复核字段未通过，不得执行交付质检或导出。"
        )
        code = run_codex(codex, prompt, log_root / "delivery-producer.log", timeout)
        total, _passed = record_counts(db, int(row["id"]))
        if code or total == 0:
            return task_id, code or 1, "delivery producer did not create records"

    total, passed = record_counts(db, int(row["id"]))
    if passed != total:
        prompt = (
            f"使用 $cc-usr-delivery-qc 只质检批次 {batch} 第 {number} 题的全部交付记录。"
            f"原始 Claude JSONL 根目录为 {data_root.resolve()}。以 SQLite 已有生产记录为质检对象，不得重新生产。"
            "成功结论必须有目标轮次原始 JSONL 中的真实运行证据；测试失败要保留记录、说明影响并降低对应分数。"
            "五维描述必须各自为单段、至少 45 个汉字且不超过 420 个字符。依据可核验证据修正不合规字段，"
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
    with closing(connect(db)) as connection:
        batch_row = connection.execute(
            "SELECT id,folder_path,status FROM batches WHERE name=?", (batch,),
        ).fetchone()
        if batch_row is None:
            return 1, "batch does not exist"
        states = connection.execute(
            "SELECT q.id,q.question_no,q.status,q.maintenance_mode,"
            "EXISTS(SELECT 1 FROM runs x WHERE x.question_id=q.id AND x.status='succeeded') AS model_succeeded,"
            "COUNT(r.id) AS record_count,"
            "SUM(CASE WHEN r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过' "
            "AND r.evidence_gate_passed=1 AND r.history_gate_passed=1 "
            "AND r.human_qc_approved=1 THEN 1 ELSE 0 END) AS passed_count "
            "FROM questions q LEFT JOIN records r ON r.question_id=q.id "
            "WHERE q.batch_id=? GROUP BY q.id ORDER BY q.question_no",
            (batch_row["id"],),
        ).fetchall()
    if not states:
        with closing(connect(db)) as connection:
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
        not bool(row["maintenance_mode"]) and (
            int(row["question_no"]) in eligible or row["status"] == "blocked"
        )
        for row in states
    )
    if not terminal:
        return 0, "batch still has pending questions"
    if not eligible:
        with closing(connect(db)) as connection:
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
        f"使用 $cc-usr-excel-exporter 只导出批次 {batch} 第 {selection} 题中已经通过事实证据、历史去重、交付质检和最终五维复核的完整记录，"
        f"显式使用 --select {selection} 和 --claude-root {data_root.resolve()}，逐字节复制原始 JSONL，"
        "不得修改评分、记录或目标模型产物。完成后报告输出路径并停止。"
    )
    code = run_codex(codex, prompt, data_root / batch / "export.log", timeout)
    new_trajectories = set(folder.glob("轨迹_*.jsonl")) - trajectories_before
    if code or not set(folder.glob("CC_Codex*.xlsx")) - before or len(new_trajectories) < len(eligible):
        return code or 1, "export did not create a complete delivery package"
    status = "completed" if len(eligible) == len(states) else "partial"
    with closing(connect(db)) as connection:
        connection.execute(
            "UPDATE batches SET status=?,updated_at=? WHERE name=?", (status, now(), batch),
        )
        connection.commit()
    return 0, f"batch {status}: exported questions {selection}"


def run_delivery_pipeline(args: argparse.Namespace) -> int:
    database = args.db.resolve()
    gateway_max_attempts = getattr(args, "gateway_max_attempts", DEFAULT_GATEWAY_MAX_ATTEMPTS)
    gateway_backoff_base = getattr(args, "gateway_backoff_base", DEFAULT_GATEWAY_BACKOFF_BASE)
    gateway_backoff_max = getattr(args, "gateway_backoff_max", DEFAULT_GATEWAY_BACKOFF_MAX)
    gateway_circuit_threshold = getattr(
        args, "gateway_circuit_threshold", DEFAULT_GATEWAY_CIRCUIT_THRESHOLD,
    )
    gateway_circuit_window = getattr(
        args, "gateway_circuit_window", DEFAULT_GATEWAY_CIRCUIT_WINDOW,
    )
    gateway_circuit_cooldown = getattr(
        args, "gateway_circuit_cooldown", DEFAULT_GATEWAY_CIRCUIT_COOLDOWN,
    )
    with closing(connect(database)) as connection:
        rows = question_rows(connection, args.batch)
        candidates = []
        for row in rows:
            succeeded = connection.execute(
                "SELECT 1 FROM runs WHERE question_id=? AND status='succeeded' LIMIT 1",
                (row["id"],),
            ).fetchone() is not None
            attempts = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE question_id=? AND status IN ('failed','timeout') "
                "AND COALESCE(failure_kind,'')!='transient_gateway'",
                (row["id"],),
            ).fetchone()[0]
            gateway_attempts = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE question_id=? AND status='failed' "
                "AND failure_kind='transient_gateway'",
                (row["id"],),
            ).fetchone()[0]
            if succeeded:
                connection.execute(
                    "UPDATE questions SET status='completed',updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
            elif (
                ready(row) and attempts < args.max_attempts
                and gateway_attempts < gateway_max_attempts
            ):
                candidates.append(row)
            elif row["status"] != "blocked":
                connection.execute(
                    "UPDATE questions SET status='blocked',updated_at=? WHERE id=?", (now(), row["id"]),
                )
        connection.commit()
        rows = question_rows(connection, args.batch)

    errors: list[str] = []
    scheduled_delivery: set[int] = set()
    circuit_breaker = GatewayCircuitBreaker(
        gateway_circuit_threshold, gateway_circuit_window, gateway_circuit_cooldown,
    )
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
                getattr(args, "heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS),
                getattr(args, "start_timeout", DEFAULT_START_TIMEOUT),
                getattr(args, "stalled_timeout", DEFAULT_STALLED_TIMEOUT),
                gateway_max_attempts, gateway_backoff_base,
                gateway_backoff_max, circuit_breaker,
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
    with closing(connect(database)) as connection:
        status = connection.execute(
            "SELECT status FROM batches WHERE name=?", (args.batch,),
        ).fetchone()
    terminal = status is not None and status["status"] in {"completed", "partial", "failed"}
    return 0 if terminal else (1 if errors else 0)


def run_global_delivery_pipeline(args: argparse.Namespace) -> int:
    """Continuously drain model and delivery work across every active batch."""
    database = args.db.resolve()
    owner = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
    sentinel = args.producer_sentinel.resolve() if args.producer_sentinel else None
    model_lease_seconds = (
        args.timeout * (args.max_attempts + args.gateway_max_attempts)
        + args.gateway_backoff_max * max(0, args.gateway_max_attempts - 1)
        + 600
    )
    delivery_lease_seconds = args.agent_timeout * 2 + 600
    circuit_breaker = GatewayCircuitBreaker(
        args.gateway_circuit_threshold,
        args.gateway_circuit_window,
        args.gateway_circuit_cooldown,
    )
    scheduled_exports: set[str] = set()
    export_attempts: dict[str, int] = {}
    delivery_attempts: dict[int, int] = {}
    fatal_configuration = False
    idle_since = time.monotonic()
    adaptive_reason = ""
    memory_match = re.fullmatch(r"([0-9]+)([mg])", args.memory.strip().lower())
    worker_memory_gb = (
        int(memory_match.group(1)) / 1024
        if memory_match and memory_match.group(2) == "m"
        else float(memory_match.group(1)) if memory_match else 2.0
    )
    machine_capacity = detect_capacity()
    env_values: dict[str, str] = {}
    try:
        for raw in read_portable_text(args.env_file.resolve()).splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env_values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    solo2_enabled = env_values.get("CC_SOLO2_AUTO_SUBMIT", "false").lower() in {
        "1", "true", "yes", "on",
    }
    solo2_origin = env_values.get("CC_SOLO2_ORIGIN", "").strip() or "https://solo2.jzxhnh.com"
    try:
        solo2_concurrency = max(1, min(8, int(env_values.get("CC_SOLO2_CONCURRENCY", "1"))))
        solo2_max_attempts = max(1, min(10, int(env_values.get("CC_SOLO2_MAX_ATTEMPTS", "3"))))
    except ValueError:
        solo2_concurrency, solo2_max_attempts = 1, 3
    solo2_cookie = args.env_file.resolve().parent / ".local-auth" / "solo2.cookies"
    solo2_retry_at = 0.0

    def leased_model(row: sqlite3.Row) -> tuple[str, int, str]:
        try:
            return run_with_retries(
                database, row, args.env_file.resolve(), args.data_root.resolve(),
                args.image, args.cpus, args.memory, args.timeout, args.max_attempts,
                args.heartbeat_seconds, args.start_timeout, args.stalled_timeout,
                args.gateway_max_attempts, args.gateway_backoff_base,
                args.gateway_backoff_max, circuit_breaker,
            )
        finally:
            release_question_lease(database, int(row["id"]), "model", owner)

    def leased_delivery(row: sqlite3.Row) -> tuple[str, int, str]:
        question_id = int(row["id"])
        try:
            result = deliver_one(
                database, row, args.codex, args.data_root.resolve(), args.agent_timeout,
            )
            if result[1]:
                delivery_attempts[question_id] = delivery_attempts.get(question_id, 0) + 1
                if delivery_attempts[question_id] >= args.delivery_max_attempts:
                    block_question(database, question_id)
            else:
                delivery_attempts.pop(question_id, None)
            return result
        finally:
            release_question_lease(database, question_id, "delivery", owner)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency, thread_name_prefix="global-model"
    ) as model_pool, concurrent.futures.ThreadPoolExecutor(
        max_workers=args.codex_concurrency, thread_name_prefix="global-delivery"
    ) as delivery_pool, concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="global-export"
    ) as export_pool, concurrent.futures.ThreadPoolExecutor(
        max_workers=solo2_concurrency, thread_name_prefix="global-solo2"
    ) as solo2_pool:
        model_futures: dict[concurrent.futures.Future, sqlite3.Row] = {}
        delivery_futures: dict[concurrent.futures.Future, sqlite3.Row] = {}
        export_futures: dict[concurrent.futures.Future, str] = {}
        solo2_futures: set[concurrent.futures.Future] = set()

        while not fatal_configuration:
            producer_done = sentinel is not None and sentinel.exists()
            admission_limit, reason = adaptive_model_limit(
                args.concurrency, len(model_futures), worker_memory_gb,
                machine_capacity,
            )
            if reason != adaptive_reason:
                adaptive_reason = reason
                print(
                    f"adaptive model limit={admission_limit}/{args.concurrency}: {reason}",
                    flush=True,
                )
            model_slots = admission_limit - len(model_futures)
            for row in _claim_rows(
                database, owner, "model", model_slots, model_lease_seconds,
                args.max_attempts, args.gateway_max_attempts,
            ):
                model_futures[model_pool.submit(leased_model, row)] = row
                print(f"{row['task_id']}: leased by global model pool", flush=True)

            delivery_slots = args.codex_concurrency - len(delivery_futures)
            for row in _claim_rows(
                database, owner, "delivery", delivery_slots, delivery_lease_seconds,
            ):
                delivery_futures[delivery_pool.submit(leased_delivery, row)] = row
                print(f"{row['task_id']}: leased by global delivery pool", flush=True)

            for batch in active_batch_names(database):
                if (
                    batch not in scheduled_exports
                    and batch_ready_to_finalize(database, batch)
                    and not export_futures
                ):
                    scheduled_exports.add(batch)
                    export_futures[export_pool.submit(
                        finalize_batch, database, batch, args.codex,
                        args.data_root.resolve(), args.agent_timeout,
                    )] = batch
                    break

            if solo2_enabled and time.monotonic() >= solo2_retry_at:
                solo2_slots = solo2_concurrency - len(solo2_futures)
                for _slot in range(solo2_slots):
                    if not solo2_pending_available(database, solo2_max_attempts):
                        break
                    solo2_futures.add(solo2_pool.submit(
                        submit_records, database, solo2_cookie, solo2_origin,
                        limit=1, max_attempts=solo2_max_attempts, manual=False,
                    ))

            futures = (
                set(model_futures) | set(delivery_futures) | set(export_futures)
                | solo2_futures
            )
            if not futures:
                if producer_done or time.monotonic() - idle_since >= args.idle_timeout:
                    break
                time.sleep(min(5, max(1, args.poll_seconds)))
                continue

            idle_since = time.monotonic()
            done, _pending = concurrent.futures.wait(
                futures, timeout=max(1, args.poll_seconds),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                if future in model_futures:
                    row = model_futures.pop(future)
                    try:
                        task_id, code, message = future.result()
                    except Exception as exc:
                        task_id, code, message = str(row["task_id"]), 1, str(exc)
                    print(f"{task_id}: {message}", flush=True)
                    if code:
                        classification = latest_failure(database, int(row["id"]), message)
                        if classification.kind in {"permanent_auth", "model_config", "environment"}:
                            fatal_configuration = True
                            print(
                                f"global model pool stopped after non-retryable {classification.kind} failure",
                                file=sys.stderr, flush=True,
                            )
                elif future in delivery_futures:
                    row = delivery_futures.pop(future)
                    try:
                        task_id, _code, message = future.result()
                    except Exception as exc:
                        task_id, message = str(row["task_id"]), str(exc)
                    print(f"{task_id}: {message}", flush=True)
                elif future in export_futures:
                    batch = export_futures.pop(future)
                    try:
                        code, message = future.result()
                    except Exception as exc:
                        code, message = 1, str(exc)
                    print(f"{batch}: {message}", flush=True)
                    if code:
                        export_attempts[batch] = export_attempts.get(batch, 0) + 1
                        if export_attempts[batch] < 2:
                            scheduled_exports.discard(batch)
                else:
                    solo2_futures.discard(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        solo2_retry_at = time.monotonic() + 60
                        print(
                            f"SOLO2 auto-submit paused for 60s: {exc}",
                            file=sys.stderr, flush=True,
                        )
                        continue
                    print(
                        f"SOLO2 auto-submit: submitted={result['submitted']} "
                        f"failed={result['failed']}", flush=True,
                    )
                    statuses = {
                        str(item.get("status") or "") for item in result.get("results", [])
                        if not item.get("ok")
                    }
                    if statuses & {"auth_blocked", "schema_blocked"}:
                        solo2_retry_at = time.monotonic() + 300

    return 1 if fatal_configuration else 0


def main() -> int:
    def env_seconds(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, str(default))))
        except ValueError:
            return default

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", default="")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--data-root", type=Path, default=Path("runs"))
    parser.add_argument("--image", default="ccusr-claude-worker:local")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--cpus", type=float, default=1.0)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument(
        "--heartbeat-seconds", type=int,
        default=env_seconds("CC_CLAUDE_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS),
    )
    parser.add_argument(
        "--start-timeout", type=int,
        default=env_seconds("CC_CLAUDE_START_TIMEOUT", DEFAULT_START_TIMEOUT),
    )
    parser.add_argument(
        "--stalled-timeout", type=int,
        default=env_seconds("CC_CLAUDE_STALLED_TIMEOUT", DEFAULT_STALLED_TIMEOUT),
    )
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument(
        "--gateway-max-attempts", type=int,
        default=env_seconds("CC_GATEWAY_MAX_ATTEMPTS", DEFAULT_GATEWAY_MAX_ATTEMPTS),
    )
    parser.add_argument(
        "--gateway-backoff-base", type=int,
        default=env_seconds("CC_GATEWAY_BACKOFF_BASE", DEFAULT_GATEWAY_BACKOFF_BASE),
    )
    parser.add_argument(
        "--gateway-backoff-max", type=int,
        default=env_seconds("CC_GATEWAY_BACKOFF_MAX", DEFAULT_GATEWAY_BACKOFF_MAX),
    )
    parser.add_argument(
        "--gateway-circuit-threshold", type=int,
        default=env_seconds("CC_GATEWAY_CIRCUIT_THRESHOLD", DEFAULT_GATEWAY_CIRCUIT_THRESHOLD),
    )
    parser.add_argument(
        "--gateway-circuit-window", type=int,
        default=env_seconds("CC_GATEWAY_CIRCUIT_WINDOW", DEFAULT_GATEWAY_CIRCUIT_WINDOW),
    )
    parser.add_argument(
        "--gateway-circuit-cooldown", type=int,
        default=env_seconds("CC_GATEWAY_CIRCUIT_COOLDOWN", DEFAULT_GATEWAY_CIRCUIT_COOLDOWN),
    )
    parser.add_argument("--deliver", action="store_true")
    parser.add_argument("--all-active", action="store_true")
    parser.add_argument("--producer-sentinel", type=Path)
    parser.add_argument("--idle-timeout", type=int, default=30)
    parser.add_argument("--delivery-max-attempts", type=int, default=2)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--codex-concurrency", type=int, default=1)
    parser.add_argument("--agent-timeout", type=int, default=3600)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=15)
    args = parser.parse_args()
    if not args.all_active and not args.batch:
        parser.error("--batch is required unless --all-active is used")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.codex_concurrency < 1:
        parser.error("--codex-concurrency must be positive")
    if args.concurrency > 32 or args.codex_concurrency > 32:
        parser.error("concurrency must not exceed 32")
    if min(
        args.heartbeat_seconds, args.start_timeout, args.stalled_timeout, args.timeout,
        args.gateway_max_attempts, args.gateway_backoff_base, args.gateway_backoff_max,
        args.gateway_circuit_threshold, args.gateway_circuit_window,
        args.gateway_circuit_cooldown,
    ) < 1:
        parser.error("worker monitoring timeouts must be positive")
    if args.deliver:
        if args.all_active:
            return run_global_delivery_pipeline(args)
        return run_delivery_pipeline(args)
    while True:
        circuit_breaker = GatewayCircuitBreaker(
            args.gateway_circuit_threshold,
            args.gateway_circuit_window,
            args.gateway_circuit_cooldown,
        )
        with connect(args.db.resolve()) as connection:
            candidates = [row for row in question_rows(connection, args.batch) if ready(row)]
            rows = []
            for row in candidates:
                attempts = failed_attempts(args.db.resolve(), int(row["id"]))
                gateway_attempts = gateway_failed_attempts(args.db.resolve(), int(row["id"]))
                if attempts < args.max_attempts and gateway_attempts < args.gateway_max_attempts:
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
                args.timeout, args.max_attempts, args.heartbeat_seconds,
                args.start_timeout, args.stalled_timeout,
                args.gateway_max_attempts, args.gateway_backoff_base,
                args.gateway_backoff_max, circuit_breaker,
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
