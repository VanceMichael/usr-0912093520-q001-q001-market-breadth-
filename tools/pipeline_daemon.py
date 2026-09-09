#!/usr/bin/env python3
"""Run one serialized autonomous production cycle.

The control agent authors and audits batches; Claude Code question runs remain
isolated Docker workers managed by ``tools.orchestrator``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402
from tools.authoring_policy import backend_only_requirement  # noqa: E402
from tools.news_topics import DEFAULT_FEEDS, configured_feeds, ingest  # noqa: E402
from tools.scheduler_state import SchedulerStore  # noqa: E402


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SchedulerInterrupted(RuntimeError):
    def __init__(self, desired_state: str) -> None:
        super().__init__(desired_state)
        self.desired_state = desired_state


def _terminate_process(process: subprocess.Popen, grace_seconds: int = 10) -> None:
    if process.poll() is not None:
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
    with connect(database.resolve()) as connection:
        rows = connection.execute(
            "SELECT r.id,r.question_id,r.container_id FROM runs r WHERE r.status='running'"
        ).fetchall()
        if not rows:
            return 0
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
        connection.commit()
    return len(rows)


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
) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{now()}] $ {' '.join(command)}\n")
        handle.flush()
        process = subprocess.Popen(
            command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        started = time.monotonic()
        while process.poll() is None:
            if store is not None:
                state = store.heartbeat()
                desired = str(state.get("desired_state") or "running")
                if desired in {"stopped", "restarting"}:
                    handle.write(f"[{now()}] scheduler requested {desired}; terminating child\n")
                    handle.flush()
                    _terminate_process(process)
                    raise SchedulerInterrupted(desired)
            if time.monotonic() - started >= timeout:
                _terminate_process(process)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(1)
        handle.write(f"[{now()}] exit={process.returncode}\n")
    return int(process.returncode or 0)


def load_runtime_env(path: Path) -> None:
    """Load the GitHub token and authoring preferences for child CLI processes."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
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
            "CC_CLAUDE_DOCKER_IMAGE",
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


def claim_topic(database: Path) -> dict | None:
    with connect(database.resolve()) as connection:
        row = connection.execute(
            "SELECT * FROM news_topics WHERE status='new' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        connection.execute("UPDATE news_topics SET status='claimed',updated_at=? WHERE id=?", (now(), row["id"]))
        connection.commit()
        return dict(row)


def release_topic(database: Path, topic_id: int, status: str, batch: str = "") -> None:
    with connect(database.resolve()) as connection:
        connection.execute("UPDATE news_topics SET status=?,used_batch=?,updated_at=? WHERE id=?", (status, batch, now(), topic_id))
        connection.commit()


def count_ready(database: Path) -> int:
    with connect(database.resolve()) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM questions WHERE status='approved' AND mechanical_qc='pass' AND qc_decision='pass' AND qc_prompt_sha256=prompt_sha256").fetchone()[0])


def active_batches(database: Path) -> list[str]:
    with connect(database.resolve()) as connection:
        return [str(row["name"]) for row in connection.execute(
            "SELECT name FROM batches WHERE status NOT IN ('completed','partial','failed') ORDER BY created_at"
        )]


def manual_jobs_active(database: Path) -> bool:
    with connect(database.resolve()) as connection:
        return connection.execute(
            "SELECT 1 FROM author_jobs WHERE status IN ('queued','running') "
            "UNION ALL SELECT 1 FROM pipeline_jobs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone() is not None


def has_new_topics(database: Path) -> bool:
    with connect(database.resolve()) as connection:
        return connection.execute("SELECT 1 FROM news_topics WHERE status='new' LIMIT 1").fetchone() is not None


def create_batch(database: Path, codex: str, topic: dict, batch: str, log: Path, timeout: int, store: SchedulerStore | None = None) -> int:
    context = json.dumps({key: topic.get(key, "") for key in ("title", "summary", "article_url", "source_url", "published_at")}, ensure_ascii=False)
    batch_size = author_batch_size()
    distribution = difficulty_distribution(batch_size, difficulty_plan())
    prompt = f"""你是持续生产控制 agent。严格读取并遵守项目根目录的 项目规范.md，以及 .agents/skills/cc-usr-question-author/SKILL.md 和 cc-usr-question-qc/SKILL.md。现在创建一个名为 {batch} 的首轮 0-1 代码生成批次，生成 {batch_size} 道彼此明显不同、业务导向、可执行验收的题目。难度必须严格分配为：{distribution}；每道题的 difficulty 字段按此填写，不得擅自改变题数或难度。{backend_only_requirement()}新闻主题只作为业务背景种子，不要复制新闻标题，不要把新闻事实当成实现要求，也不要使用新闻网站代码或受版权保护的正文。先检查现有 production.sqlite3 的题目避免重复；完成真实初始工程、GitHub 可访问快照、机械质检和重复性质检后才算完成。主题种子如下：{context}。全过程只修改本项目和题目工作区，完成后输出批次名、每题状态和任何阻塞原因。"""
    return run_command(codex_command(codex, prompt), PROJECT_ROOT, log, timeout, store)


def authored_batch_result(database: Path, batch: str, expected_count: int) -> tuple[bool, int, int]:
    with connect(database.resolve()) as connection:
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
    with connect(database.resolve()) as connection:
        connection.execute(
            "UPDATE batches SET status='failed',updated_at=? WHERE name=?",
            (now(), batch),
        )
        connection.commit()


def create_next_batch(
    database: Path, args: argparse.Namespace, store: SchedulerStore | None = None,
) -> tuple[int, str]:
    topic = claim_topic(database)
    if topic is None:
        return 0, ""
    batch = f"news{datetime.now().strftime('%m%d%H%M%S')}"
    if store:
        store.heartbeat(phase="author", batch=batch, detail="Codex 正在生成并质检题目")
        store.event(
            "topic_claimed", f"已领取新闻主题：{topic.get('title', '')}",
            phase="author", batch=batch,
            details={"article_url": topic.get("article_url", ""), "source_url": topic.get("source_url", "")},
        )
    log = args.log_dir.resolve() / f"author-{batch}.log"
    try:
        code = create_batch(database, args.codex, topic, batch, log, args.agent_timeout, store)
    except Exception:
        fail_authored_batch(database, batch)
        release_topic(database, topic["id"], "new")
        raise
    if code == 0:
        complete, total, ready = authored_batch_result(database, batch, author_batch_size())
        release_topic(database, topic["id"], "used", batch)
        if not complete:
            if store:
                store.event(
                    "author_validation_failed",
                    f"出题结果未通过调度门禁：实际 {total} 道，可运行 {ready} 道",
                    level="error", phase="author", batch=batch,
                    details={"total": total, "ready": ready, "expected": author_batch_size()},
                )
            return 1, batch
    else:
        fail_authored_batch(database, batch)
        release_topic(database, topic["id"], "new")
    return code, batch


def pipeline_batch_timeout(
    question_count: int, model_concurrency: int, codex_concurrency: int,
    worker_timeout: int, agent_timeout: int, max_attempts: int,
) -> int:
    model_budget = math.ceil(question_count / max(1, model_concurrency)) * worker_timeout * max_attempts
    delivery_budget = math.ceil(question_count / max(1, codex_concurrency)) * agent_timeout * 2
    return max(model_budget, delivery_budget) + agent_timeout + 600


def run_batch(database: Path, args: argparse.Namespace, batch: str, store: SchedulerStore | None = None) -> int:
    if store:
        store.heartbeat(phase="model", batch=batch, detail="Claude 模型池与 Codex 交付池正在流水线处理")
        store.event("phase_started", "开始模型与交付并行流水线", phase="model", batch=batch)
    with connect(database.resolve()) as connection:
        question_count = int(connection.execute(
            "SELECT COUNT(*) FROM questions q JOIN batches b ON b.id=q.batch_id WHERE b.name=?",
            (batch,),
        ).fetchone()[0])
    batch_timeout = pipeline_batch_timeout(
        question_count, args.concurrency, args.codex_concurrency,
        args.worker_timeout, args.agent_timeout, args.max_attempts,
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
            "--timeout", str(args.worker_timeout),
        ],
        PROJECT_ROOT,
        args.log_dir.resolve() / f"workers-{batch}.log",
        batch_timeout,
        store,
    )


def cycle(args: argparse.Namespace, store: SchedulerStore | None = None) -> tuple[int, str]:
    load_runtime_env(args.env_file.resolve())
    database = args.db.resolve()
    feeds = configured_feeds(database) if args.dynamic_feeds else args.feeds
    try:
        args.concurrency = max(1, min(8, int(os.environ.get("CC_PIPELINE_MODEL_CONCURRENCY", args.concurrency))))
    except ValueError:
        pass
    try:
        args.codex_concurrency = max(1, min(8, int(os.environ.get("CC_PIPELINE_CODEX_CONCURRENCY", args.codex_concurrency))))
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
    parser.add_argument("--worker-timeout", type=int, default=3600)
    parser.add_argument("--ready-watermark", type=int, default=4)
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
    with lock_path.open("w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another pipeline daemon is already running", file=sys.stderr)
            return 2
        store.startup()
        recovered = recover_interrupted_runs(args.db.resolve())
        if recovered:
            store.event(
                "runs_recovered", f"已回收 {recovered} 个中断的 Claude 任务",
                level="warning", details={"count": recovered},
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
                store.finish_cycle(cycle_id, status="failed", batch=batch, error=error)
            except Exception as exc:  # keep the resident scheduler observable after one bad cycle
                code = 1
                error = f"{type(exc).__name__}: {exc}"
                store.finish_cycle(cycle_id, status="failed", batch=batch, error=error)
            else:
                if code:
                    store.finish_cycle(cycle_id, status="failed", batch=batch, error=f"流水线退出码 {code}")
                else:
                    store.finish_cycle(cycle_id, status="completed", batch=batch)

            state = store.state()
            if code and int(state.get("consecutive_failures") or 0) >= max(1, args.failure_threshold):
                store.update(
                    desired_state="paused", actual_state="error", detail="连续失败达到阈值，已暂停新周期",
                )
                store.event(
                    "circuit_opened", "连续失败达到阈值，调度器已暂停",
                    level="error", details={"failure_threshold": args.failure_threshold},
                )
            elif str(state.get("desired_state")) == "draining":
                store.apply_controls("paused", "当前周期已完成，已排空")
                store.update(actual_state="paused", detail="已排空并暂停")
                store.event("scheduler_drained", "当前周期完成，调度器已排空并暂停")
            if not args.loop:
                return code
            wait_with_heartbeat(store, max(1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
