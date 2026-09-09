#!/usr/bin/env python3
"""Run one serialized autonomous production cycle.

The control agent authors and audits batches; Claude Code question runs remain
isolated Docker workers managed by ``tools.orchestrator``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402
from tools.news_topics import DEFAULT_FEEDS, configured_feeds, ingest  # noqa: E402


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_command(command: list[str], cwd: Path, log: Path, timeout: int) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{now()}] $ {' '.join(command)}\n")
        handle.flush()
        result = subprocess.run(command, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        handle.write(f"[{now()}] exit={result.returncode}\n")
    return result.returncode


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


def runnable_batches(database: Path, max_attempts: int) -> list[str]:
    with connect(database.resolve()) as connection:
        return [row["name"] for row in connection.execute(
            "SELECT DISTINCT b.name FROM batches b JOIN questions q ON q.batch_id=b.id "
            "WHERE q.status='approved' AND q.mechanical_qc='pass' AND q.qc_decision='pass' "
            "AND q.qc_prompt_sha256=q.prompt_sha256 AND "
            "(SELECT COUNT(*) FROM runs r WHERE r.question_id=q.id AND r.status IN ('failed','timeout')) < ? "
            "ORDER BY b.created_at",
            (max_attempts,),
        )]


def has_new_topics(database: Path) -> bool:
    with connect(database.resolve()) as connection:
        return connection.execute("SELECT 1 FROM news_topics WHERE status='new' LIMIT 1").fetchone() is not None


def create_batch(database: Path, codex: str, topic: dict, batch: str, log: Path, timeout: int) -> int:
    context = json.dumps({key: topic.get(key, "") for key in ("title", "summary", "article_url", "source_url", "published_at")}, ensure_ascii=False)
    batch_size = author_batch_size()
    distribution = difficulty_distribution(batch_size, difficulty_plan())
    prompt = f"""你是持续生产控制 agent。严格读取并遵守项目根目录的 项目规范.md，以及 .agents/skills/cc-usr-question-author/SKILL.md 和 cc-usr-question-qc/SKILL.md。现在创建一个名为 {batch} 的首轮 0-1 代码生成批次，生成 {batch_size} 道彼此明显不同、业务导向、可执行验收的题目。难度必须严格分配为：{distribution}；每道题的 difficulty 字段按此填写，不得擅自改变题数或难度。新闻主题只作为业务背景种子，不要复制新闻标题，不要把新闻事实当成实现要求，也不要使用新闻网站代码或受版权保护的正文。先检查现有 production.sqlite3 的题目避免重复；完成真实初始工程、GitHub 可访问快照、机械质检和重复性质检后才算完成。主题种子如下：{context}。全过程只修改本项目和题目工作区，完成后输出批次名、每题状态和任何阻塞原因。"""
    return run_command(codex_command(codex, prompt), PROJECT_ROOT, log, timeout)


def process_ready(database: Path, codex: str, log: Path, timeout: int) -> int:
    with connect(database.resolve()) as connection:
        batches = [dict(row) for row in connection.execute(
            "SELECT name,folder_path FROM batches WHERE status!='completed' ORDER BY created_at"
        )]
    for batch_row in batches:
        batch = batch_row["name"]
        with connect(database.resolve()) as connection:
            needs_producer = connection.execute("SELECT COUNT(*) FROM questions q JOIN batches b ON b.id=q.batch_id WHERE b.name=? AND EXISTS(SELECT 1 FROM runs r WHERE r.question_id=q.id AND r.status='succeeded') AND NOT EXISTS(SELECT 1 FROM records d WHERE d.question_id=q.id)", (batch,)).fetchone()[0]
        if needs_producer:
            prompt = f"使用 $cc-usr-delivery-producer 处理批次 {batch} 的全部已完成题目，读取原始 Claude JSONL 轨迹和实际产物，按项目规范逐轮评分入库；不要修改目标模型代码。完成后停止。"
            if run_command(codex_command(codex, prompt), PROJECT_ROOT, log, timeout) != 0:
                return 1
        with connect(database.resolve()) as connection:
            needs_qc = connection.execute("SELECT COUNT(*) FROM records r JOIN questions q ON q.id=r.question_id JOIN batches b ON b.id=q.batch_id WHERE b.name=? AND r.delivery_qc_passed=0", (batch,)).fetchone()[0]
        if needs_qc:
            prompt = f"使用 $cc-usr-delivery-qc 质检批次 {batch} 的全部交付记录；从可核验证据修正问题并最终写入质检通过，不要导出 Excel。完成后停止。"
            if run_command(codex_command(codex, prompt), PROJECT_ROOT, log, timeout) != 0:
                return 1
        with connect(database.resolve()) as connection:
            totals = connection.execute(
                "SELECT COUNT(DISTINCT q.id),COUNT(DISTINCT CASE WHEN q.status='completed' THEN q.id END),"
                "COUNT(DISTINCT CASE WHEN r.id IS NOT NULL THEN q.id END),"
                "COUNT(CASE WHEN r.id IS NOT NULL AND r.delivery_qc_passed=0 THEN 1 END) "
                "FROM questions q JOIN batches b ON b.id=q.batch_id LEFT JOIN records r ON r.question_id=q.id WHERE b.name=?",
                (batch,),
            ).fetchone()
        total_questions, completed_questions, recorded_questions, unpassed_records = map(int, totals)
        if total_questions and (completed_questions, recorded_questions, unpassed_records) == (total_questions, total_questions, 0):
            batch_folder = Path(batch_row["folder_path"])
            before = set(batch_folder.glob("*.xlsx"))
            prompt = f"使用 $cc-usr-excel-exporter 导出批次 {batch} 中已经通过交付质检的完整记录，复制原始 JSONL 轨迹并报告输出路径；不要修改评分或记录。完成后停止。"
            if run_command(codex_command(codex, prompt), PROJECT_ROOT, log, timeout) != 0:
                return 1
            if not set(batch_folder.glob("*.xlsx")) - before:
                print(f"batch {batch}: exporter reported success but created no workbook", file=sys.stderr, flush=True)
                return 1
            with connect(database.resolve()) as connection:
                connection.execute("UPDATE batches SET status='completed',updated_at=? WHERE name=?", (now(), batch))
                connection.commit()
    return 0


def run_batch(database: Path, args: argparse.Namespace, batch: str) -> int:
    return run_command(
        [
            sys.executable, str(PROJECT_ROOT / "tools" / "orchestrator.py"),
            "--db", str(database), "--batch", batch,
            "--env-file", str(args.env_file.resolve()),
            "--data-root", str(args.data_root.resolve()),
            "--image", args.worker_image,
            "--concurrency", str(args.concurrency),
            "--max-attempts", str(args.max_attempts),
            "--timeout", str(args.worker_timeout),
        ],
        PROJECT_ROOT,
        args.log_dir.resolve() / f"workers-{batch}.log",
        max(args.agent_timeout, args.worker_timeout * args.max_attempts + 300),
    )


def cycle(args: argparse.Namespace) -> int:
    load_runtime_env(args.env_file.resolve())
    database = args.db.resolve()
    added, errors = ingest(database, args.feeds, args.feed_timeout)
    print(f"news: added={added} feed_errors={len(errors)}", flush=True)
    for error in errors:
        print(f"news warning: {error}", file=sys.stderr, flush=True)
    if errors and not added and not has_new_topics(database):
        return 1
    for existing_batch in runnable_batches(database, args.max_attempts):
        if run_batch(database, args, existing_batch):
            return 1
    if process_ready(database, args.codex, args.log_dir.resolve() / "delivery.log", args.agent_timeout):
        return 1
    if count_ready(database) < args.ready_watermark:
        topic = claim_topic(database)
        if topic:
            batch = f"news{datetime.now().strftime('%m%d%H%M%S')}"
            log = args.log_dir.resolve() / f"author-{batch}.log"
            code = create_batch(database, args.codex, topic, batch, log, args.agent_timeout)
            release_topic(database, topic["id"], "used" if code == 0 else "new", batch if code == 0 else "")
            if code:
                return code
            run_code = run_batch(database, args, batch)
            if run_code:
                return run_code
    return process_ready(database, args.codex, args.log_dir.resolve() / "delivery.log", args.agent_timeout)


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
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--worker-timeout", type=int, default=3600)
    parser.add_argument("--ready-watermark", type=int, default=4)
    parser.add_argument("--feed-timeout", type=int, default=20)
    parser.add_argument("--agent-timeout", type=int, default=3600)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    load_runtime_env(args.env_file.resolve())
    args.codex = resolve_codex(args.codex)
    if "NEWS_FEEDS" not in os.environ and args.feeds == ",".join(DEFAULT_FEEDS):
        persisted_feeds = configured_feeds(args.db)
        if persisted_feeds:
            args.feeds = ",".join(persisted_feeds)
    args.feeds = [value.strip() for value in args.feeds.split(",") if value.strip()]
    lock_path = args.log_dir / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another pipeline daemon is already running", file=sys.stderr)
            return 2
        while True:
            code = cycle(args)
            if not args.loop or code:
                return code
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
