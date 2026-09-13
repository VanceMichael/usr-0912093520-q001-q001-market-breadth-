#!/usr/bin/env python3
"""Migrate untouched legacy questions to the SQLite-only authoring policy.

Only questions without runs and delivery records are eligible. Historical
questions are deliberately left byte-for-byte unchanged. The migration is
transactional and writes a JSON audit report under ``runs/maintenance``.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect, now, prompt_hash


EXTERNAL_LANGUAGE_RE = re.compile(
    r"(?i)postgres(?:ql)?(?:\s*\d+(?:\.\d+)?)?|redis|mongo(?:db)?|"
    r"mysql|mariadb|h2|clickhouse|elasticsearch|opensearch|cassandra|"
    r"kafka(?:\s*[^,]*)?|nats(?:\s*[^,]*)?|minio|docker(?:\s+compose)?"
)


def rewrite_prompt(prompt: str) -> str:
    """Remove external service requirements without changing the business goal."""
    text = prompt.strip()
    replacements = (
        (r"(?i)通过\s*Docker\s*Compose\s*提供(?:数据库和消息服务|运行环境|依赖)[，。]?", ""),
        (r"(?i)Docker\s*Compose\s*(?:负责|提供|启动)[^，。；;]*[，。；;]?", ""),
        (r"(?i)Dockerfile\s*(?:中|需|提供|负责)[^，。；;]*[，。；;]?", ""),
        (r"(?i)使用\s*PostgreSQL(?:\s*\d+(?:\.\d+)?)?\s*(?:与|和|、)\s*Redis", "使用 SQLite 保存业务数据，缓存使用进程内实现"),
        (r"(?i)使用\s*PostgreSQL(?:\s*\d+(?:\.\d+)?)?\s*(?:与|和|、)\s*(?:MongoDB|MinIO|NATS[^，。；;]*)", "使用 SQLite 保存业务数据，文件和异步队列使用本地进程实现"),
        (r"(?i)使用\s*(?:PostgreSQL|Postgres|MySQL|MariaDB|MongoDB|Redis|H2|ClickHouse|Elasticsearch|OpenSearch|Cassandra)(?:\s*\d+(?:\.\d+)?)?(?:\s*(?:与|和|、)\s*(?:PostgreSQL|Postgres|MySQL|MariaDB|MongoDB|Redis|H2|ClickHouse|Elasticsearch|OpenSearch|Cassandra)(?:\s*\d+(?:\.\d+)?)?)*", "使用 SQLite 保存业务数据"),
        (r"(?i)使用\s*(?:Kafka|NATS(?:\s*[^，。；;]*)?)", "使用进程内异步队列"),
        (r"(?i)(?:PostgreSQL|Postgres|MySQL|MariaDB|MongoDB|Redis|H2|ClickHouse|Elasticsearch|OpenSearch|Cassandra)", "SQLite"),
        (r"(?i)(?:Kafka|NATS(?:\s*JetStream)?)", "进程内异步队列"),
        (r"(?i)MinIO", "本地文件目录"),
        (r"(?i)Docker(?:\s+Compose)?", "外部容器环境"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[，、]\s*[，、]", "，", text)
    text = re.sub(r"外部容器环境(?:file|容器内|\s*镜像)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)Compose\s*(?:负责启动依赖|启动数据库|提供依赖)[，。；;]?", "", text)
    text = re.sub(r"(?i)(?:由|通过)\s*\s*[，。]?\s*建立完整运行环境", "", text)
    text = re.sub(r"(?i)(?:项目)?\s*由\s*[，。]?\s*构建", "项目可直接构建", text)
    text = re.sub(r"\s+([，。；;])", r"\1", text)
    text = re.sub(r"([，。；;])\s*(?:并|由)\s*([，。；;])", r"\1", text)
    text = re.sub(r"提供\s*及\s*命令行", "提供命令行", text)
    text = re.sub(r"由\s*(?=集成测试|pytest|JUnit|Go 测试|Node 测试)", "", text)
    text = re.sub(r"使用\s+和\s+", "使用", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"；\s*；", "；", text)
    if "SQLite" not in text:
        text += " 使用 SQLite 保存业务数据，验收通过本地命令和自动化测试完成，无需外部服务。"
    elif "无需外部服务" not in text and "不依赖外部服务" not in text:
        text += " 验收通过本地命令和自动化测试完成，无需外部服务。"
    return text.strip()


def rewrite_languages(value: str) -> str:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    kept: list[str] = []
    for part in parts:
        if EXTERNAL_LANGUAGE_RE.search(part):
            continue
        kept.append(part)
    if not any(re.search(r"(?i)sqlite", part) for part in kept):
        kept.append("SQLite")
    return ", ".join(dict.fromkeys(kept))


def needs_migration(prompt: str, languages: str) -> bool:
    return bool(EXTERNAL_LANGUAGE_RE.search(prompt) or EXTERNAL_LANGUAGE_RE.search(languages))


def eligible_rows(connection: sqlite3.Connection):
    return connection.execute(
        """SELECT q.* FROM questions q
           WHERE NOT EXISTS (SELECT 1 FROM runs r WHERE r.question_id=q.id)
             AND NOT EXISTS (SELECT 1 FROM records r WHERE r.question_id=q.id)
           ORDER BY q.id"""
    ).fetchall()


def migrate(database: Path, report_path: Path, dry_run: bool = False) -> dict:
    connection = connect(database.resolve())
    changed: list[dict] = []
    skipped: list[dict] = []
    try:
        rows = eligible_rows(connection)
        for row in rows:
            if not needs_migration(row["prompt"], row["languages"]):
                skipped.append({"task_id": row["task_id"], "reason": "already SQLite-only"})
                continue
            new_prompt = rewrite_prompt(row["prompt"])
            new_languages = rewrite_languages(row["languages"])
            if new_prompt == row["prompt"] and new_languages == row["languages"]:
                skipped.append({"task_id": row["task_id"], "reason": "already SQLite-only"})
                continue
            changed.append({
                "question_id": row["id"],
                "task_id": row["task_id"],
                "batch": connection.execute(
                    "SELECT name FROM batches WHERE id=?", (row["batch_id"],)
                ).fetchone()[0],
                "old_prompt": row["prompt"],
                "new_prompt": new_prompt,
                "old_languages": row["languages"],
                "new_languages": new_languages,
            })
        if not dry_run:
            timestamp = now()
            for item in changed:
                connection.execute(
                    """UPDATE questions SET prompt=?, prompt_sha256=?, languages=?,
                       reproducibility='无外部依赖', mechanical_qc='pending',
                       qc_decision='pending', qc_report='{}', qc_prompt_sha256='',
                       human_approved=0, human_reviewer='', approved_at='', status='draft',
                       updated_at=? WHERE id=?""",
                    (
                        item["new_prompt"], prompt_hash(item["new_prompt"]),
                        item["new_languages"], timestamp, item["question_id"],
                    ),
                )
                connection.execute(
                    "UPDATE mother_library SET prompt=?, updated_at=? WHERE source_question_id=?",
                    (item["new_prompt"], timestamp, item["question_id"]),
                )
            connection.commit()
        else:
            connection.rollback()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database.resolve()),
        "dry_run": dry_run,
        "changed_count": len(changed),
        "skipped_count": len(skipped),
        "changed": changed,
        "skipped": skipped,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--report", type=Path, default=Path("runs/maintenance/sqlite-migration.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = migrate(args.db, args.report, args.dry_run)
    print(json.dumps({key: report[key] for key in ("dry_run", "changed_count", "skipped_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
