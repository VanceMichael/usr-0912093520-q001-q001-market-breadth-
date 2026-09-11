#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one serialized autonomous production cycle.

The control agent authors and audits batches; Claude Code question runs remain
isolated Docker workers managed by ``tools.orchestrator``.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import json
import math
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

if os.name != "nt":
    import fcntl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402
from tools.authoring_policy import backend_only_requirement  # noqa: E402
from tools.news_topics import DEFAULT_FEEDS, configured_feeds, ingest, ingest_report  # noqa: E402
from tools.orchestrator import clear_question_leases  # noqa: E402
from tools.scheduler_state import SchedulerStore  # noqa: E402
from tools.text_encoding import read_portable_text  # noqa: E402


AUTHOR_FAILURE_EXIT = 75
SYSTEMIC_FAILURE_KINDS = {
    "database_unavailable", "permanent_auth", "model_config", "environment",
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SchedulerInterrupted(RuntimeError):
    def __init__(self, desired_state: str) -> None:
        super().__init__(desired_state)
        self.desired_state = desired_state


def child_process_kwargs() -> dict[str, object]:
    """Start children in a private process group on every supported host."""
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


@contextmanager
def daemon_lock(path: Path):
    """Hold an exclusive lock for the daemon lifetime without Unix-only imports."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError from exc
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield lock
        finally:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _terminate_process(process: subprocess.Popen, grace_seconds: int = 10) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            process.kill()
        process.wait()


def recover_interrupted_runs(database: Path) -> int:
    """Stop orphaned workers and make their questions eligible for a clean retry."""
    with closing(connect(database.resolve())) as connection:
        rows = connection.execute(
            "SELECT r.id,r.question_id,r.container_id FROM runs r WHERE r.status='running'"
        ).fetchall()
        for row in rows:
            container = str(row["container_id"] or "").strip()
            if container:
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=20, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            connection.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,error_message=?,heartbeat_at=? WHERE id=?",
                (now(), "调度器重启或立即停止，任务已回收到待运行队列", now(), row["id"]),
            )
            connection.execute(
                "UPDATE questions SET status='approved',updated_at=? WHERE id=? AND status='running'",
                (now(), row["question_id"]),
            )
        connection.execute(
            "UPDATE questions SET model_lease_owner='',model_lease_expires_at='',"
            "delivery_lease_owner='',delivery_lease_expires_at=''"
        )
        connection.commit()
    return len(rows)


def recover_interrupted_authoring(database: Path) -> tuple[int, int]:
    """Resolve topic claims left behind when an author process was interrupted."""
    released = 0
    preserved = 0
    with closing(connect(database.resolve())) as connection:
        claimed_ids = {
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM news_topics WHERE status='claimed'"
            )
        }
        if not claimed_ids:
            return 0, 0
        events = connection.execute(
            "SELECT batch_name,details_json FROM scheduler_events "
            "WHERE event_type='topics_claimed' ORDER BY id DESC"
        ).fetchall()
        mapped_ids: set[int] = set()
        timestamp = now()
        for event in events:
            try:
                details = json.loads(str(event["details_json"] or "{}"))
                topic_ids = {
                    int(value) for value in details.get("topic_ids", [])
                    if int(value) in claimed_ids
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            topic_ids -= mapped_ids
            if not topic_ids:
                continue
            mapped_ids.update(topic_ids)
            batch = str(event["batch_name"] or "")
            row = connection.execute(
                "SELECT id,status,question_count FROM batches WHERE name=?", (batch,),
            ).fetchone()
            complete = False
            if row is not None:
                expected = int(row["question_count"] or 0)
                total, ready = connection.execute(
                    "SELECT COUNT(*),SUM(CASE WHEN status='approved' AND mechanical_qc='pass' "
                    "AND qc_decision='pass' AND qc_prompt_sha256=prompt_sha256 "
                    "AND repo_url<>'' AND initial_snapshot<>'' AND local_initial_sha<>'' "
                    "THEN 1 ELSE 0 END) FROM questions WHERE batch_id=?",
                    (row["id"],),
                ).fetchone()
                complete = expected > 0 and int(total or 0) == expected and int(ready or 0) == expected
                if complete and str(row["status"]) == "draft":
                    connection.execute(
                        "UPDATE batches SET status='ready',updated_at=? WHERE id=?",
                        (timestamp, row["id"]),
                    )
                elif not complete and str(row["status"]) == "draft":
                    connection.execute(
                        "UPDATE batches SET status='failed',updated_at=? WHERE id=?",
                        (timestamp, row["id"]),
                    )
            if complete:
                connection.executemany(
                    "UPDATE news_topics SET status='used',used_batch=?,updated_at=? "
                    "WHERE id=? AND status='claimed'",
                    [(batch, timestamp, topic_id) for topic_id in topic_ids],
                )
                preserved += len(topic_ids)
            else:
                connection.executemany(
                    "UPDATE news_topics SET status='new',used_batch='',updated_at=? "
                    "WHERE id=? AND status='claimed'",
                    [(timestamp, topic_id) for topic_id in topic_ids],
                )
                released += len(topic_ids)
        orphaned = claimed_ids - mapped_ids
        if orphaned:
            connection.executemany(
                "UPDATE news_topics SET status='new',used_batch='',updated_at=? "
                "WHERE id=? AND status='claimed'",
                [(timestamp, topic_id) for topic_id in orphaned],
            )
            released += len(orphaned)
        connection.commit()
    return released, preserved


def wait_with_heartbeat(store: SchedulerStore, seconds: int) -> None:
    deadline = time.monotonic() + max(1, seconds)
    while time.monotonic() < deadline:
        store.heartbeat()
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def cleanup_logs(log_dir: Path, retention_days: int, max_log_gb: float) -> tuple[int, int]:
    if not log_dir.is_dir():
        return 0, 0
    paths = sorted(
        (path for path in log_dir.rglob("*.log") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    cutoff = datetime.now().timestamp() - timedelta(days=max(1, retention_days)).total_seconds()
    removed = 0
    reclaimed = 0
    kept = []
    for path in paths:
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                size = stat.st_size
                path.unlink()
                removed += 1
                reclaimed += size
            else:
                kept.append((path, stat.st_size))
        except OSError:
            continue
    maximum = int(max(0.1, max_log_gb) * 1024 ** 3)
    total = sum(size for _path, size in kept)
    for path, size in kept:
        if total <= maximum:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
        reclaimed += size
    return removed, reclaimed


def run_command(
    command: list[str], cwd: Path, log: Path, timeout: int,
    store: SchedulerStore | None = None,
    drain_sentinel: Path | None = None,
) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{now()}] $ {' '.join(command)}\n")
        handle.flush()
        process = subprocess.Popen(
            command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT,
            **child_process_kwargs(),
        )
        started = time.monotonic()
        while process.poll() is None:
            if store is not None:
                state = store.heartbeat()
                desired = str(state.get("desired_state") or "running")
                if desired == "draining" and drain_sentinel is not None:
                    drain_sentinel.parent.mkdir(parents=True, exist_ok=True)
                    drain_sentinel.write_text("draining", encoding="utf-8")
                if desired in {"stopped", "restarting"}:
                    handle.write(f"[{now()}] scheduler requested {desired}; terminating child\n")
                    handle.flush()
                    _terminate_process(process)
                    raise SchedulerInterrupted(desired)
            if timeout > 0 and time.monotonic() - started >= timeout:
                _terminate_process(process)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(1)
        handle.write(f"[{now()}] exit={process.returncode}\n")
    return int(process.returncode or 0)


def load_runtime_env(path: Path) -> None:
    """Load the GitHub token and authoring preferences for child CLI processes."""
    if not path.is_file():
        return
    for line in read_portable_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = str(json.loads(value))
            except json.JSONDecodeError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        if key == "CC_GITHUB_TOKEN" and value:
            os.environ["GH_TOKEN"] = value
            os.environ["GITHUB_TOKEN"] = value
        elif key == "CC_AUTHOR_DIFFICULTY" and value:
            os.environ["CC_AUTHOR_DIFFICULTY"] = value
        elif key == "CC_AUTHOR_DIFFICULTY_WEIGHTS" and value:
            os.environ["CC_AUTHOR_DIFFICULTY_WEIGHTS"] = value
        elif key == "CC_AUTHOR_BATCH_SIZE" and value:
            os.environ["CC_AUTHOR_BATCH_SIZE"] = value
        elif key in {
            "CC_PIPELINE_MODEL_CONCURRENCY", "CC_PIPELINE_CODEX_CONCURRENCY",
            "CC_PIPELINE_READY_TARGET", "CC_NEWS_READY_TARGET",
            "CC_CLAUDE_WORKER_CPUS",
            "CC_CLAUDE_WORKER_MEMORY",
            "CC_CLAUDE_DOCKER_IMAGE", "CC_CLAUDE_HEARTBEAT_SECONDS",
            "CC_CLAUDE_START_TIMEOUT", "CC_CLAUDE_STALLED_TIMEOUT",
            "CC_PIPELINE_WORKER_TIMEOUT",
            "CC_GATEWAY_MAX_ATTEMPTS", "CC_GATEWAY_BACKOFF_BASE",
            "CC_GATEWAY_BACKOFF_MAX", "CC_GATEWAY_CIRCUIT_THRESHOLD",
            "CC_GATEWAY_CIRCUIT_WINDOW", "CC_GATEWAY_CIRCUIT_COOLDOWN",
        } and value:
            os.environ[key] = value


def author_batch_size() -> int:
    try:
        value = int(os.environ.get("CC_AUTHOR_BATCH_SIZE", "10").strip() or "10")
    except ValueError:
        return 10
    return max(1, min(value, 20))


def difficulty_plan() -> dict[str, int]:
    raw = os.environ.get("CC_AUTHOR_DIFFICULTY_WEIGHTS", "").strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    plan: dict[str, int] = {}
    if isinstance(parsed, dict):
        for difficulty in ("中等", "困难", "地狱"):
            try:
                weight = int(parsed.get(difficulty, 0))
            except (TypeError, ValueError):
                weight = 0
            if weight > 0:
                plan[difficulty] = weight
    if plan:
        return plan
    legacy = os.environ.get("CC_AUTHOR_DIFFICULTY", "中等").strip()
    return {legacy if legacy in {"中等", "困难", "地狱"} else "中等": 100}


def difficulty_distribution(count: int, plan: dict[str, int]) -> str:
    order = ("中等", "困难", "地狱")
    total = sum(plan.values())
    entries: list[list[object]] = []
    allocated = 0
    for index, difficulty in enumerate(order):
        if difficulty not in plan:
            continue
        raw = count * plan[difficulty] / total
        base = int(raw)
        allocated += base
        entries.append([raw - base, -index, difficulty, base])
    for entry in sorted(entries, reverse=True)[: count - allocated]:
        entry[3] = int(entry[3]) + 1
    counts = {str(entry[2]): int(entry[3]) for entry in entries}
    return "、".join(
        f"{difficulty} {counts[difficulty]} 道（{plan[difficulty] / total:.0%}）"
        for difficulty in order if difficulty in plan
    )


def codex_command(codex: str, prompt: str) -> list[str]:
    return [codex, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "-C", str(PROJECT_ROOT), prompt]


def resolve_codex(codex: str) -> str:
    """Resolve Codex in both interactive and systemd/non-login environments."""
    if os.path.isabs(codex):
        return codex
    return shutil.which(codex) or str(Path.home() / ".local" / "bin" / codex)


def claim_topics(database: Path, count: int) -> list[dict]:
    """Atomically claim exactly ``count`` distinct topics, or claim none."""
    count = max(1, count)
    with closing(connect(database.resolve())) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT * FROM news_topics WHERE status='new' ORDER BY created_at,id LIMIT ?",
            (count,),
        ).fetchall()
        if len(rows) < count:
            return []
        timestamp = now()
        connection.executemany(
            "UPDATE news_topics SET status='claimed',updated_at=? WHERE id=?",
            [(timestamp, row["id"]) for row in rows],
        )
        connection.commit()
        return [dict(row) for row in rows]


def release_topics(database: Path, topic_ids: list[int], status: str, batch: str = "") -> None:
    if not topic_ids:
        return
    with closing(connect(database.resolve())) as connection:
        timestamp = now()
        connection.executemany(
            "UPDATE news_topics SET status=?,used_batch=?,updated_at=? WHERE id=?",
            [(status, batch, timestamp, topic_id) for topic_id in topic_ids],
        )
        connection.commit()


def count_ready(database: Path) -> int:
    with closing(connect(database.resolve())) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM questions q JOIN batches b ON b.id=q.batch_id "
            "WHERE b.status NOT IN ('completed','partial','failed') "
            "AND q.status='approved' AND q.mechanical_qc='pass' AND q.qc_decision='pass' "
            "AND q.qc_prompt_sha256=q.prompt_sha256"
        ).fetchone()[0])


def active_batches(database: Path) -> list[str]:
    with closing(connect(database.resolve())) as connection:
        return [str(row["name"]) for row in connection.execute(
            "SELECT name FROM batches WHERE status NOT IN ('completed','partial','failed') ORDER BY created_at"
        )]


def manual_jobs_active(database: Path) -> bool:
    with closing(connect(database.resolve())) as connection:
        return connection.execute(
            "SELECT 1 FROM author_jobs WHERE status IN ('queued','running') "
            "UNION ALL SELECT 1 FROM pipeline_jobs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone() is not None


def count_new_topics(database: Path) -> int:
    with closing(connect(database.resolve())) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM news_topics WHERE status='new'"
        ).fetchone()[0])


def has_new_topics(database: Path) -> bool:
    return count_new_topics(database) > 0


def latest_systemic_failure(database: Path, since: str = "") -> str:
    """Return a recent non-retryable worker failure, if one caused pool shutdown."""
    with closing(connect(database.resolve())) as connection:
        row = connection.execute(
            "SELECT failure_kind FROM runs WHERE status IN ('failed','timeout') "
            "AND retryable=0 AND failure_kind IN ('permanent_auth','model_config','environment') "
            "AND (?='' OR started_at>=?) ORDER BY id DESC LIMIT 1",
            (since, since),
        ).fetchone()
    return str(row["failure_kind"] or "") if row else ""


def author_prompt(topics: list[dict], batch: str, batch_size: int, distribution: str) -> str:
    source_urls = list(dict.fromkeys(
        str(topic.get("source_url", "")).strip()
        for topic in topics
        if str(topic.get("source_url", "")).strip()
    ))
    sources = "、".join(source_urls)
    context = json.dumps([
        {
            "question_no": index,
            **{key: topic.get(key, "") for key in ("title", "summary", "article_url", "source_url", "published_at")},
        }
        for index, topic in enumerate(topics, start=1)
    ], ensure_ascii=False)
    return f"""使用 $cc-usr-question-author 创建批次。
批次名：{batch}
题目数量：{batch_size}
难度分配：{distribution}
出题要求：业务关键词：从 {sources} 读取主题来进行出题，本轮必须按下方新闻主题清单的 question_no 将主题与题目一一绑定，每条新闻必须且只能生成一道题，不得遗漏、复用、合并主题或从同一主题派生多道题；技术关键词：需要 Docker，每道题的初始工程必须提供 Dockerfile，有外部依赖时同时提供 Docker Compose，并支持通过命令完成构建和验收；补充要求：在整批中合理覆盖 Node.js（JavaScript 或 TypeScript）、Python、Go、Java，每道题只选择其中一种主要后端技术栈。{backend_only_requirement()}每道题的 difficulty 字段必须严格按上述题数分配，不得擅自改变题数或难度。新闻只作为业务背景种子，不要复制新闻标题，不要把新闻事实当成实现要求，也不要使用新闻网站代码或受版权保护的正文。先检查 production.sqlite3 中已有题目，发现重复或模板化表达必须重写。
新闻主题清单：{context}
严格遵守 项目规范.md。完成真实初始工程、GitHub 可访问快照和登记后，运行出题机械质检，并使用 $cc-usr-question-qc 完成重复、自然度和反模板质检；不要启动目标模型，也不要亲自调用 Claude 目标模型，出题会话正常结束后将由调度器自动接管后续模型流水线。不得读取、调用或修改 SchedulerStore、scheduler_state、scheduler_controls、调度器控制接口、守护进程状态或服务启停状态，也不得为了阻止目标模型而暂停、停止或重启调度器。全过程只修改本项目和题目工作区，完成后直接输出批次名、每题状态和任何阻塞原因。"""


def create_batch(database: Path, codex: str, topics: list[dict], batch: str, log: Path, timeout: int, store: SchedulerStore | None = None) -> int:
    batch_size = author_batch_size()
    if len(topics) != batch_size:
        raise ValueError(f"expected {batch_size} news topics, got {len(topics)}")
    distribution = difficulty_distribution(batch_size, difficulty_plan())
    prompt = author_prompt(topics, batch, batch_size, distribution)
    return run_command(codex_command(codex, prompt), PROJECT_ROOT, log, timeout, store)


def authored_batch_result(database: Path, batch: str, expected_count: int) -> tuple[bool, int, int]:
    with closing(connect(database.resolve())) as connection:
        batch_row = connection.execute(
            "SELECT id,question_count FROM batches WHERE name=?", (batch,),
        ).fetchone()
        if batch_row is None:
            return False, 0, 0
        total = int(connection.execute(
            "SELECT COUNT(*) FROM questions WHERE batch_id=?", (batch_row["id"],),
        ).fetchone()[0])
        ready = int(connection.execute(
            "SELECT COUNT(*) FROM questions WHERE batch_id=? AND status='approved' "
            "AND mechanical_qc='pass' AND qc_decision='pass' "
            "AND qc_prompt_sha256=prompt_sha256 AND repo_url<>'' "
            "AND initial_snapshot<>'' AND local_initial_sha<>''",
            (batch_row["id"],),
        ).fetchone()[0])
        complete = (
            int(batch_row["question_count"]) == expected_count
            and total == expected_count
            and ready == expected_count
        )
        connection.execute(
            "UPDATE batches SET status=?,updated_at=? WHERE id=?",
            ("ready" if complete else "failed", now(), batch_row["id"]),
        )
        connection.commit()
        return complete, total, ready


def fail_authored_batch(database: Path, batch: str) -> None:
    with closing(connect(database.resolve())) as connection:
        connection.execute(
            "UPDATE batches SET status='failed',updated_at=? WHERE name=?",
            (now(), batch),
        )
        connection.commit()


def create_next_batch(
    database: Path, args: argparse.Namespace, store: SchedulerStore | None = None,
) -> tuple[int, str]:
    batch_size = author_batch_size()
    topics = claim_topics(database, batch_size)
    if not topics:
        if store:
            store.heartbeat(
                phase="idle", batch="",
                detail=f"可用新闻不足 {batch_size} 条，等待更多不同主题",
            )
        return 0, ""
    topic_ids = [int(topic["id"]) for topic in topics]
    batch = datetime.now().strftime('%m%d%H%M%S')
    if store:
        store.heartbeat(phase="author", batch=batch, detail="Codex 正在生成并质检题目")
        store.event(
            "topics_claimed", f"已领取 {len(topics)} 条不同新闻主题",
            phase="author", batch=batch,
            details={
                "topic_ids": topic_ids,
                "article_urls": [topic.get("article_url", "") for topic in topics],
            },
        )
    log = args.log_dir.resolve() / f"author-{batch}.log"
    try:
        code = create_batch(database, args.codex, topics, batch, log, args.agent_timeout, store)
    except Exception as exc:
        fail_authored_batch(database, batch)
        release_topics(database, topic_ids, "new")
        if store:
            store.event(
                "author_failed", f"出题任务失败，将保留新闻并退避重试：{exc}",
                level="warning", phase="author", batch=batch,
                details={"failure_kind": "authoring", "retryable": True},
            )
        return AUTHOR_FAILURE_EXIT, batch
    if code == 0:
        complete, total, ready = authored_batch_result(database, batch, batch_size)
        release_topics(database, topic_ids, "used", batch)
        if not complete:
            if store:
                store.event(
                    "author_validation_failed",
                    f"出题结果未通过调度门禁：实际 {total} 道，可运行 {ready} 道",
                    level="error", phase="author", batch=batch,
                    details={"total": total, "ready": ready, "expected": batch_size},
                )
            return AUTHOR_FAILURE_EXIT, batch
    else:
        fail_authored_batch(database, batch)
        release_topics(database, topic_ids, "new")
    return (AUTHOR_FAILURE_EXIT if code else 0), batch


def pipeline_batch_timeout(
    question_count: int, model_concurrency: int, codex_concurrency: int,
    worker_timeout: int, agent_timeout: int, max_attempts: int,
    gateway_max_attempts: int = 0, gateway_backoff_max: int = 0,
) -> int:
    per_wave = (
        worker_timeout * (max_attempts + max(0, gateway_max_attempts))
        + max(0, gateway_max_attempts - 1) * max(0, gateway_backoff_max)
    )
    model_budget = math.ceil(question_count / max(1, model_concurrency)) * per_wave
    delivery_budget = math.ceil(question_count / max(1, codex_concurrency)) * agent_timeout * 2
    return max(model_budget, delivery_budget) + agent_timeout + 600


def run_batch(database: Path, args: argparse.Namespace, batch: str, store: SchedulerStore | None = None) -> int:
    if store:
        store.heartbeat(phase="model", batch=batch, detail="Claude 模型池与 Codex 交付池正在流水线处理")
        store.event("phase_started", "开始模型与交付并行流水线", phase="model", batch=batch)
    with closing(connect(database.resolve())) as connection:
        question_count = int(connection.execute(
            "SELECT COUNT(*) FROM questions q JOIN batches b ON b.id=q.batch_id WHERE b.name=?",
            (batch,),
        ).fetchone()[0])
    batch_timeout = pipeline_batch_timeout(
        question_count, args.concurrency, args.codex_concurrency,
        args.worker_timeout, args.agent_timeout, args.max_attempts,
        args.gateway_max_attempts, args.gateway_backoff_max,
    )
    return run_command(
        [
            sys.executable, str(PROJECT_ROOT / "tools" / "orchestrator.py"),
            "--db", str(database), "--batch", batch,
            "--env-file", str(args.env_file.resolve()),
            "--data-root", str(args.data_root.resolve()),
            "--image", args.worker_image,
            "--concurrency", str(args.concurrency),
            "--deliver",
            "--codex", args.codex,
            "--codex-concurrency", str(args.codex_concurrency),
            "--agent-timeout", str(args.agent_timeout),
            "--max-attempts", str(args.max_attempts),
            "--gateway-max-attempts", str(args.gateway_max_attempts),
            "--gateway-backoff-base", str(args.gateway_backoff_base),
            "--gateway-backoff-max", str(args.gateway_backoff_max),
            "--gateway-circuit-threshold", str(args.gateway_circuit_threshold),
            "--gateway-circuit-window", str(args.gateway_circuit_window),
            "--gateway-circuit-cooldown", str(args.gateway_circuit_cooldown),
            "--timeout", str(args.worker_timeout),
        ],
        PROJECT_ROOT,
        args.log_dir.resolve() / f"workers-{batch}.log",
        batch_timeout,
        store,
    )


def run_global_queue(
    database: Path,
    args: argparse.Namespace,
    sentinel: Path,
    store: SchedulerStore,
) -> int:
    store.heartbeat(
        phase="model", batch="", detail="全局模型池、交付池和导出池正在跨批次处理",
    )
    store.event("phase_started", "开始跨批次全局流水线", phase="model")
    return run_command(
        [
            sys.executable, str(PROJECT_ROOT / "tools" / "orchestrator.py"),
            "--db", str(database), "--all-active", "--deliver",
            "--producer-sentinel", str(sentinel),
            "--idle-timeout", str(args.global_idle_timeout),
            "--poll-seconds", "5",
            "--env-file", str(args.env_file.resolve()),
            "--data-root", str(args.data_root.resolve()),
            "--image", args.worker_image,
            "--concurrency", str(args.concurrency),
            "--cpus", str(args.worker_cpus),
            "--memory", args.worker_memory,
            "--codex", args.codex,
            "--codex-concurrency", str(args.codex_concurrency),
            "--agent-timeout", str(args.agent_timeout),
            "--max-attempts", str(args.max_attempts),
            "--gateway-max-attempts", str(args.gateway_max_attempts),
            "--gateway-backoff-base", str(args.gateway_backoff_base),
            "--gateway-backoff-max", str(args.gateway_backoff_max),
            "--gateway-circuit-threshold", str(args.gateway_circuit_threshold),
            "--gateway-circuit-window", str(args.gateway_circuit_window),
            "--gateway-circuit-cooldown", str(args.gateway_circuit_cooldown),
            "--heartbeat-seconds", str(args.heartbeat_seconds),
            "--start-timeout", str(args.start_timeout),
            "--stalled-timeout", str(args.stalled_timeout),
            "--timeout", str(args.worker_timeout),
        ],
        PROJECT_ROOT,
        args.log_dir.resolve() / "workers-global.log",
        0,
        store,
        sentinel,
    )


def maintain_ready_buffer(
    database: Path,
    args: argparse.Namespace,
    store: SchedulerStore,
    stop_event: threading.Event,
    sentinel: Path,
    outcome: dict[str, object],
) -> None:
    """Keep authoring in parallel until control state asks the global pool to drain."""
    try:
        author_failures = 0
        next_news_refill_at = 0.0
        while not stop_event.is_set():
            desired = str(store.state().get("desired_state") or "running")
            if desired != "running":
                break
            batch_size = author_batch_size()
            available_topics = count_new_topics(database)
            refill_delay = min(60, max(5, args.poll_seconds))
            if available_topics < args.news_watermark and time.monotonic() >= next_news_refill_at:
                feeds = configured_feeds(database) if args.dynamic_feeds else args.feeds
                report = ingest_report(database, feeds, args.feed_timeout)
                added = int(report["added"])
                errors = list(report["errors"])
                available_topics = count_new_topics(database)
                next_news_refill_at = time.monotonic() + refill_delay
                parsed = int(report["parsed"])
                duplicates = int(report["duplicates"])
                store.event(
                    "news_refill",
                    (
                        f"新闻补充完成：解析 {parsed} 条，新增 {added} 条，"
                        f"重复 {duplicates} 条，当前待用 {available_topics}/{args.news_watermark} 条"
                    ),
                    level="warning" if errors and not added else "info",
                    phase="news",
                    details={
                        "added": added,
                        "parsed": parsed,
                        "duplicates": duplicates,
                        "available": available_topics,
                        "target": args.news_watermark,
                        "next_refill_seconds": refill_delay,
                        "feed_errors": errors,
                        "feeds": report["feeds"],
                    },
                )
            if count_ready(database) >= args.ready_watermark:
                stop_event.wait(2)
                continue
            if available_topics < batch_size:
                store.heartbeat(
                    phase="idle", batch="",
                    detail=(
                        f"新闻待用 {available_topics}/{args.news_watermark} 条，"
                        f"不足单批 {batch_size} 条，{refill_delay} 秒后重新抓取"
                    ),
                )
                stop_event.wait(refill_delay)
                continue
            code, batch = create_next_batch(database, args, store)
            if code:
                if code == AUTHOR_FAILURE_EXIT:
                    author_failures += 1
                    delay = min(900, 30 * (2 ** min(author_failures - 1, 5)))
                    outcome.update(
                        code=0, batch=batch, author_failure_count=author_failures,
                        last_author_error=f"批次 {batch or '-'} 出题失败",
                    )
                    store.heartbeat(
                        phase="author", batch=batch,
                        detail=f"出题失败，{delay} 秒后重试；已有题目继续处理",
                    )
                    store.event(
                        "author_backoff", f"出题失败，{delay} 秒后重试",
                        level="warning", phase="author", batch=batch,
                        details={"delay_seconds": delay, "failure_count": author_failures},
                    )
                    stop_event.wait(delay)
                    continue
                outcome.update(code=code, batch=batch)
                break
            if batch:
                author_failures = 0
                store.event(
                    "buffer_batch_created",
                    f"题目缓冲池新增批次 {batch}",
                    phase="author", batch=batch,
                    details={"ready_target": args.ready_watermark},
                )
    except Exception as exc:
        outcome.update(code=1, error=f"{type(exc).__name__}: {exc}")
    finally:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("producer stopped", encoding="utf-8")


def cycle(args: argparse.Namespace, store: SchedulerStore | None = None) -> tuple[int, str]:
    load_runtime_env(args.env_file.resolve())
    database = args.db.resolve()
    args.ready_watermark = getattr(args, "ready_watermark", 40)
    args.news_watermark = getattr(args, "news_watermark", 40)
    args.worker_cpus = getattr(args, "worker_cpus", 1.0)
    args.worker_memory = getattr(args, "worker_memory", "2g")
    args.heartbeat_seconds = getattr(args, "heartbeat_seconds", 5)
    args.start_timeout = getattr(args, "start_timeout", 300)
    args.stalled_timeout = getattr(args, "stalled_timeout", 900)
    args.global_idle_timeout = getattr(args, "global_idle_timeout", 1800)
    args.worker_timeout = getattr(args, "worker_timeout", 3600)
    args.max_attempts = getattr(args, "max_attempts", 2)
    for field, default in (
        ("gateway_max_attempts", 3),
        ("gateway_backoff_base", 30),
        ("gateway_backoff_max", 300),
        ("gateway_circuit_threshold", 2),
        ("gateway_circuit_window", 120),
        ("gateway_circuit_cooldown", 180),
    ):
        setattr(args, field, getattr(args, field, default))
    feeds = configured_feeds(database) if args.dynamic_feeds else args.feeds
    try:
        args.concurrency = max(1, min(32, int(os.environ.get("CC_PIPELINE_MODEL_CONCURRENCY", args.concurrency))))
    except ValueError:
        pass
    try:
        args.codex_concurrency = max(1, min(32, int(os.environ.get("CC_PIPELINE_CODEX_CONCURRENCY", args.codex_concurrency))))
    except ValueError:
        pass
    try:
        args.ready_watermark = max(
            1, min(200, int(os.environ.get("CC_PIPELINE_READY_TARGET", args.ready_watermark)))
        )
    except ValueError:
        pass
    try:
        args.news_watermark = max(
            author_batch_size(),
            min(1000, int(os.environ.get("CC_NEWS_READY_TARGET", args.news_watermark))),
        )
    except ValueError:
        pass
    try:
        args.worker_cpus = max(
            0.25, min(4.0, float(os.environ.get("CC_CLAUDE_WORKER_CPUS", args.worker_cpus)))
        )
    except ValueError:
        pass
    worker_memory = os.environ.get("CC_CLAUDE_WORKER_MEMORY", args.worker_memory).strip().lower()
    if re.fullmatch(r"[1-9][0-9]*(?:[mg])", worker_memory):
        args.worker_memory = worker_memory
    try:
        args.worker_timeout = max(
            1, int(os.environ.get("CC_PIPELINE_WORKER_TIMEOUT", args.worker_timeout))
        )
    except ValueError:
        pass
    for field, key in (
        ("heartbeat_seconds", "CC_CLAUDE_HEARTBEAT_SECONDS"),
        ("start_timeout", "CC_CLAUDE_START_TIMEOUT"),
        ("stalled_timeout", "CC_CLAUDE_STALLED_TIMEOUT"),
    ):
        try:
            setattr(args, field, max(1, int(os.environ.get(key, getattr(args, field)))))
        except ValueError:
            pass
    for field, key in (
        ("gateway_max_attempts", "CC_GATEWAY_MAX_ATTEMPTS"),
        ("gateway_backoff_base", "CC_GATEWAY_BACKOFF_BASE"),
        ("gateway_backoff_max", "CC_GATEWAY_BACKOFF_MAX"),
        ("gateway_circuit_threshold", "CC_GATEWAY_CIRCUIT_THRESHOLD"),
        ("gateway_circuit_window", "CC_GATEWAY_CIRCUIT_WINDOW"),
        ("gateway_circuit_cooldown", "CC_GATEWAY_CIRCUIT_COOLDOWN"),
    ):
        try:
            setattr(args, field, max(1, int(os.environ.get(key, getattr(args, field)))))
        except ValueError:
            pass
    args.worker_image = os.environ.get("CC_CLAUDE_DOCKER_IMAGE", args.worker_image).strip() or args.worker_image
    if store:
        store.heartbeat(phase="news", batch="", detail="正在抓取新闻主题")
    added, errors = ingest(database, feeds, args.feed_timeout)
    print(f"news: added={added} feed_errors={len(errors)}", flush=True)
    for error in errors:
        print(f"news warning: {error}", file=sys.stderr, flush=True)
    if errors and not added and not has_new_topics(database):
        return 1, ""
    if manual_jobs_active(database):
        if store:
            store.heartbeat(phase="idle", batch="", detail="手动任务运行中，自动调度暂不接单")
        return 0, ""
    run_mode = str(
        (store.state().get("run_mode") if store else getattr(args, "run_mode", "full")) or "full"
    )
    if run_mode == "author_only":
        return create_next_batch(database, args, store)
    if store is not None:
        if not active_batches(database):
            code, created = create_next_batch(database, args, store)
            if code or not created:
                return code, created
        sentinel = args.log_dir.resolve() / f"producer-{os.getpid()}.done"
        sentinel.unlink(missing_ok=True)
        stop_event = threading.Event()
        outcome: dict[str, object] = {"code": 0, "batch": ""}
        producer = threading.Thread(
            target=maintain_ready_buffer,
            args=(database, args, store, stop_event, sentinel, outcome),
            name="question-buffer-producer", daemon=True,
        )
        producer.start()
        try:
            code = run_global_queue(database, args, sentinel, store)
        finally:
            stop_event.set()
            producer.join()
            sentinel.unlink(missing_ok=True)
        producer_code = int(outcome.get("code") or 0)
        if producer_code:
            error = str(outcome.get("error") or "题目缓冲池生产失败")
            store.event("buffer_failed", error, level="error", phase="author")
        return code or producer_code, str(outcome.get("batch") or "")
    for existing_batch in active_batches(database):
        if run_batch(database, args, existing_batch, store):
            return 1, existing_batch
    created_batch = ""
    if not active_batches(database) and count_ready(database) < args.ready_watermark:
        code, created_batch = create_next_batch(database, args, store)
        if code:
            return code, created_batch
        if created_batch:
            run_code = run_batch(database, args, created_batch, store)
            if run_code:
                return run_code, created_batch
    return 0, created_batch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "production.sqlite3")
    parser.add_argument("--codex", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--feeds", default=os.environ.get("NEWS_FEEDS", ",".join(DEFAULT_FEEDS)));
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "runs" / "daemon")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "runs")
    parser.add_argument("--worker-image", default="ccusr-claude-worker:local")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--codex-concurrency", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--gateway-max-attempts", type=int, default=3)
    parser.add_argument("--gateway-backoff-base", type=int, default=30)
    parser.add_argument("--gateway-backoff-max", type=int, default=300)
    parser.add_argument("--gateway-circuit-threshold", type=int, default=2)
    parser.add_argument("--gateway-circuit-window", type=int, default=120)
    parser.add_argument("--gateway-circuit-cooldown", type=int, default=180)
    parser.add_argument("--worker-timeout", type=int, default=3600)
    parser.add_argument("--worker-cpus", type=float, default=1.0)
    parser.add_argument("--worker-memory", default="2g")
    parser.add_argument("--heartbeat-seconds", type=int, default=5)
    parser.add_argument("--start-timeout", type=int, default=300)
    parser.add_argument("--stalled-timeout", type=int, default=900)
    parser.add_argument("--global-idle-timeout", type=int, default=1800)
    parser.add_argument("--ready-watermark", type=int, default=40)
    parser.add_argument("--news-watermark", type=int, default=40)
    parser.add_argument("--feed-timeout", type=int, default=20)
    parser.add_argument("--agent-timeout", type=int, default=3600)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--min-free-gb", type=float, default=15.0)
    parser.add_argument("--log-retention-days", type=int, default=30)
    parser.add_argument("--max-log-gb", type=float, default=2.0)
    args = parser.parse_args()
    load_runtime_env(args.env_file.resolve())
    args.codex = resolve_codex(args.codex)
    args.dynamic_feeds = "NEWS_FEEDS" not in os.environ and args.feeds == ",".join(DEFAULT_FEEDS)
    if args.dynamic_feeds:
        persisted_feeds = configured_feeds(args.db)
        if persisted_feeds:
            args.feeds = ",".join(persisted_feeds)
    args.feeds = [value.strip() for value in args.feeds.split(",") if value.strip()]
    lock_path = args.log_dir / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    store = SchedulerStore(args.db.resolve())
    try:
        lock_context = daemon_lock(lock_path)
        lock_context.__enter__()
    except BlockingIOError:
        print("another pipeline daemon is already running", file=sys.stderr)
        return 2
    try:
        store.startup()
        recovered = recover_interrupted_runs(args.db.resolve())
        released_topics, preserved_topics = recover_interrupted_authoring(args.db.resolve())
        clear_question_leases(args.db.resolve())
        if recovered:
            store.event(
                "runs_recovered", f"已回收 {recovered} 个中断的 Claude 任务",
                level="warning", details={"count": recovered},
            )
        if released_topics or preserved_topics:
            store.event(
                "authoring_recovered",
                f"已恢复中断出题：释放 {released_topics} 条新闻，保留 {preserved_topics} 条已完成新闻",
                level="warning" if released_topics else "info",
                phase="author",
                details={"released_topics": released_topics, "preserved_topics": preserved_topics},
            )
        cleaned, reclaimed = cleanup_logs(args.log_dir.resolve(), args.log_retention_days, args.max_log_gb)
        if cleaned:
            store.event(
                "logs_cleaned", f"已清理 {cleaned} 个过期日志文件",
                details={"count": cleaned, "reclaimed_bytes": reclaimed},
            )
        while True:
            state = store.state()
            desired = str(state.get("desired_state") or "running")
            if desired in {"paused", "stopped"}:
                actual = "paused" if desired == "paused" else "stopped"
                store.apply_controls(desired, "控制指令已生效")
                store.update(actual_state=actual, phase="idle", batch_name="", detail="等待开始指令", heartbeat_at=now())
                if not args.loop:
                    return 0
                wait_with_heartbeat(store, min(args.poll_seconds, 2))
                continue
            if desired == "draining":
                store.apply_controls("paused", "当前没有运行中的周期，已排空")
                store.update(actual_state="paused", phase="idle", batch_name="", detail="已排空并暂停", heartbeat_at=now())
                store.event("scheduler_drained", "调度器已排空并暂停")
                continue
            if desired == "restarting":
                store.apply_controls("running", "已执行逻辑重启")
                store.update(
                    actual_state="running", phase="idle", batch_name="", detail="逻辑重启完成",
                    heartbeat_at=now(), restart_count=int(state.get("restart_count") or 0) + 1,
                )
                store.event("scheduler_restarted", "调度器逻辑重启完成")
            else:
                store.apply_controls("running", "控制指令已生效")

            database_ok, database_detail = store.database_healthcheck()
            if not database_ok:
                try:
                    current = store.state()
                    previous_kind = str(current.get("failure_kind") or "")
                    failure_count = (
                        int(current.get("consecutive_failures") or 0) + 1
                        if previous_kind == "database_unavailable" else 1
                    )
                    circuit_open = failure_count >= max(1, args.failure_threshold)
                    opened_at = now() if circuit_open else ""
                    reason = (
                        f"database_unavailable 连续失败达到 {args.failure_threshold} 次"
                        if circuit_open else ""
                    )
                    store.update(
                        desired_state="paused" if circuit_open else "running",
                        actual_state="error", phase="error", database_status="unavailable",
                        failure_kind="database_unavailable", last_error=database_detail,
                        consecutive_failures=failure_count,
                        circuit_reason=reason, circuit_opened_at=opened_at,
                        detail="SQLite 数据库不可用，暂停领取新任务", heartbeat_at=now(),
                    )
                    store.event(
                        "database_unavailable", database_detail, level="error",
                        details={
                            "failure_kind": "database_unavailable",
                            "failure_count": failure_count, "circuit_open": circuit_open,
                        },
                    )
                except sqlite3.Error:
                    print(f"database unavailable: {database_detail}", file=sys.stderr, flush=True)
                if not args.loop:
                    return 1
                time.sleep(min(max(1, args.poll_seconds), 10))
                continue

            free_gb = shutil.disk_usage(PROJECT_ROOT).free / (1024 ** 3)
            if free_gb < max(0, args.min_free_gb):
                detail = f"可用磁盘仅 {free_gb:.1f} GB，低于 {args.min_free_gb:.1f} GB 安全阈值"
                store.update(desired_state="paused", actual_state="error", phase="error", detail=detail, last_error=detail, heartbeat_at=now())
                store.event("disk_guard", detail, level="error")
                if not args.loop:
                    return 1
                wait_with_heartbeat(store, min(args.poll_seconds, 5))
                continue

            cycle_id = store.begin_cycle()
            cycle_started_at = now()
            batch = ""
            try:
                code, batch = cycle(args, store)
            except SchedulerInterrupted as exc:
                recovered = recover_interrupted_runs(args.db.resolve())
                store.finish_cycle(cycle_id, status="interrupted", batch=batch)
                if recovered:
                    store.event(
                        "runs_recovered", f"已回收 {recovered} 个中断的 Claude 任务",
                        level="warning", batch=batch, details={"count": recovered},
                    )
                if exc.desired_state == "restarting":
                    store.apply_controls("running", "运行中任务已终止，逻辑重启完成")
                    current = store.state()
                    store.update(
                        actual_state="running", detail="逻辑重启完成", heartbeat_at=now(),
                        restart_count=int(current.get("restart_count") or 0) + 1,
                    )
                    store.event("scheduler_restarted", "运行中任务已终止，调度器已重新开始")
                else:
                    store.apply_controls("stopped", "运行中任务已终止")
                    store.update(actual_state="stopped", phase="idle", batch_name="", detail="已立即停止", heartbeat_at=now())
                    store.event("scheduler_stopped", "运行中任务已终止，调度器已停止")
                if not args.loop:
                    return 0
                continue
            except subprocess.TimeoutExpired as exc:
                code = 1
                error = f"命令执行超时：{exc.timeout} 秒"
                store.finish_cycle(
                    cycle_id, status="degraded", batch=batch, error=error,
                    failure_kind="command_timeout",
                )
            except sqlite3.Error as exc:
                code = 1
                error = f"{type(exc).__name__}: {exc}"
                store.finish_cycle(
                    cycle_id, status="failed", batch=batch, error=error,
                    failure_kind="database_unavailable",
                )
            except Exception as exc:  # keep the resident scheduler observable after one bad cycle
                code = 1
                error = f"{type(exc).__name__}: {exc}"
                store.finish_cycle(
                    cycle_id, status="degraded", batch=batch, error=error,
                    failure_kind="operational",
                )
            else:
                if code == AUTHOR_FAILURE_EXIT:
                    store.finish_cycle(
                        cycle_id, status="degraded", batch=batch,
                        error="出题任务失败，已保留新闻主题等待退避重试",
                        failure_kind="authoring",
                    )
                elif code:
                    failure_kind = (
                        latest_systemic_failure(args.db.resolve(), cycle_started_at)
                        or "pipeline_failure"
                    )
                    cycle_status = "failed" if failure_kind in SYSTEMIC_FAILURE_KINDS else "degraded"
                    store.finish_cycle(
                        cycle_id, status=cycle_status, batch=batch,
                        error=f"流水线退出码 {code}", failure_kind=failure_kind,
                    )
                else:
                    store.finish_cycle(cycle_id, status="completed", batch=batch)

            state = store.state()
            failure_kind = str(state.get("failure_kind") or "")
            if (
                code and failure_kind in SYSTEMIC_FAILURE_KINDS
                and int(state.get("consecutive_failures") or 0) >= max(1, args.failure_threshold)
            ):
                opened_at = now()
                reason = f"{failure_kind} 连续失败达到 {args.failure_threshold} 次"
                store.update(
                    desired_state="paused", actual_state="error",
                    detail="系统性故障达到阈值，已暂停领取新任务",
                    circuit_reason=reason, circuit_opened_at=opened_at,
                )
                store.event(
                    "circuit_opened", f"系统性故障触发自动熔断：{reason}",
                    level="error", details={
                        "failure_threshold": args.failure_threshold,
                        "failure_kind": failure_kind, "opened_at": opened_at,
                    },
                )
            elif str(state.get("desired_state")) == "draining":
                store.apply_controls("paused", "当前周期已完成，已排空")
                store.update(actual_state="paused", detail="已排空并暂停")
                store.event("scheduler_drained", "当前周期完成，调度器已排空并暂停")
            if not args.loop:
                return code
            wait_with_heartbeat(store, max(1, args.poll_seconds))
    finally:
        lock_context.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
