#!/usr/bin/env python3
"""SQLite-backed batch, question, run, and delivery storage."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = 12
DEFAULT_NEWS_FEEDS = (
    "https://channel.chinanews.com.cn/cns/cl/gn-js.shtml",
    "https://channel.chinanews.com.cn/cns/cl/gn-kjww.shtml",
    "https://www.chinanews.com/finance/",
)
TASK_TYPES = {
    "0-1 代码生成", "Feature 迭代", "Bug 修复", "代码理解",
    "代码重构", "工程化", "代码测试",
}
FIRST_TURN_TASK_TYPE = "0-1 代码生成"
DIFFICULTIES = {"简单", "中等", "困难", "地狱"}
REPRODUCIBILITY = {
    "无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境",
}
QUESTION_STATUSES = {"draft", "approved", "running", "completed", "rejected"}
QC_DECISIONS = {"pending", "pass", "revise", "reject"}
SNAPSHOT_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/commit/[0-9a-fA-F]{40}$"
)
SAFE_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
BANNED_TERMS = {
    "贪吃蛇", "打砖块", "俄罗斯方块", "坦克大战", "塔防", "2d 解谜",
    "潜行", "平台跳跃", "喂食", "记忆翻牌", "连连看", "五子棋", "2048",
    "扫雷", "打地鼠", "粒子模拟", "物理模拟", "星系模拟", "烟花模拟",
    "落沙模拟", "布料模拟", "命令行工具", "代码片段管理器", "批量重命名",
    "截图标注", "文件管理", "文件同步", "书签管理", "密码管理", "购物车",
    "rbac", "库存管理", "投票问卷", "考勤", "图书借阅", "博客 cms",
    "医院挂号", "外卖点餐", "crm", "即时通讯", "拍卖", "停车场", "工单客服",
    "商品+excel", "积分商城", "预约系统", "streamlit", "csv 看板", "报表统计",
    "记账", "健康健身", "菜谱", "天气", "番茄钟", "习惯打卡", "音乐播放器",
    "旅行日记", "观影记录",
}
DOCUMENT_SUFFIXES = {".md", ".rst"}
DOCUMENT_TEXT_NAMES = {"readme", "readme.txt"}
SNAPSHOT_META_PATTERNS = (
    ("评测或标注语境", re.compile(r"评测|测评|标注任务|满意度数据|目标模型|答题模型|出题|质检")),
    ("工具或模型名称", re.compile(r"\b(?:coding agent|claude|codex|user prompt)\b", re.IGNORECASE)),
    ("英文评测术语", re.compile(r"\b(?:benchmark|evaluation)\b", re.IGNORECASE)),
    ("脚手架或答题说明", re.compile(
        r"起始工作区|初始脚手架|任务执行者|本题|题目要求|"
        r"starting workspace|starter (?:workspace|repository)|task owner|"
        r"implementation is intentionally left",
        re.IGNORECASE,
    )),
)
SNAPSHOT_META_FILENAME_RE = re.compile(
    r"(?:^|[/\\])(?:prompt|evaluation|benchmark|rubric|题目说明|评测说明|质检报告)(?:[._-]|$)",
    re.IGNORECASE,
)
PROMPT_LABEL_RE = re.compile(
    r"(?:^|[；;。])\s*(?:背景|目标|功能|技术|要求|验收|注意事项)\s*[:：]"
)
PROMPT_CANNED_OPENING_RE = re.compile(
    r"^\s*(?:请(?:你)?\s*)?从零(?:开始)?(?:构建|实现|开发|搭建|创建)一套"
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_topics (
    id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,
    article_url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    topic_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'new',
    used_batch TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_topics_status
    ON news_topics(status, created_at);

CREATE TABLE IF NOT EXISTS news_feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    folder_path TEXT NOT NULL UNIQUE,
    markdown_path TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    author_mode TEXT NOT NULL DEFAULT '0-1',
    mother_id INTEGER,
    task_types TEXT NOT NULL DEFAULT '0-1 代码生成',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT,
    question_no INTEGER NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    folder_name TEXT NOT NULL,
    folder_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    task_type TEXT NOT NULL,
    author_mode TEXT NOT NULL DEFAULT '0-1',
    mother_id INTEGER,
    difficulty TEXT NOT NULL,
    languages TEXT NOT NULL,
    repo_url TEXT NOT NULL DEFAULT '',
    initial_snapshot TEXT NOT NULL DEFAULT '',
    local_initial_sha TEXT NOT NULL DEFAULT '',
    reproducibility TEXT NOT NULL,
    expected_areas TEXT NOT NULL DEFAULT '[]',
    difficulty_evidence TEXT NOT NULL DEFAULT '[]',
    similarity_tags TEXT NOT NULL DEFAULT '[]',
    mechanical_qc TEXT NOT NULL DEFAULT 'pending',
    qc_decision TEXT NOT NULL DEFAULT 'pending',
    qc_report TEXT NOT NULL DEFAULT '{}',
    qc_prompt_sha256 TEXT NOT NULL DEFAULT '',
    human_approved INTEGER NOT NULL DEFAULT 0 CHECK (human_approved IN (0, 1)),
    human_reviewer TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, question_no),
    UNIQUE (batch_id, folder_name)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE RESTRICT,
    batch_run_id TEXT NOT NULL,
    launched_at TEXT NOT NULL,
    codex_version TEXT NOT NULL DEFAULT '',
    relay_provider TEXT NOT NULL DEFAULT '',
    relay_host TEXT NOT NULL DEFAULT '',
    relay_wire_api TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    harness TEXT NOT NULL DEFAULT 'Claude Code',
    harness_version TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    container_cwd TEXT NOT NULL DEFAULT '',
    trajectory_root TEXT NOT NULL DEFAULT '',
    operating_system TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'registered',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    error_message TEXT NOT NULL DEFAULT '',
    container_id TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    heartbeat_at TEXT NOT NULL DEFAULT '',
    UNIQUE (question_id, batch_run_id)
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE RESTRICT,
    record_id TEXT NOT NULL UNIQUE,
    turn_no INTEGER NOT NULL CHECK (turn_no BETWEEN 1 AND 10),
    user_prompt TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    initial_snapshot TEXT NOT NULL,
    trajectory_file TEXT NOT NULL,
    reproducibility TEXT NOT NULL,
    harness TEXT NOT NULL,
    harness_version TEXT NOT NULL,
    operating_system TEXT NOT NULL,
    task_type TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    languages TEXT NOT NULL,
    delivery_score INTEGER NOT NULL CHECK (delivery_score BETWEEN 1 AND 5),
    delivery_description TEXT NOT NULL,
    instruction_score INTEGER NOT NULL CHECK (instruction_score BETWEEN 1 AND 5),
    instruction_description TEXT NOT NULL,
    planning_score INTEGER NOT NULL CHECK (planning_score BETWEEN 1 AND 5),
    planning_description TEXT NOT NULL,
    reasoning_score INTEGER NOT NULL CHECK (reasoning_score BETWEEN 1 AND 5),
    reasoning_description TEXT NOT NULL,
    execution_score INTEGER NOT NULL CHECK (execution_score BETWEEN 1 AND 5),
    execution_description TEXT NOT NULL,
    other_issues TEXT NOT NULL DEFAULT '',
    submitter TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    parent_record TEXT NOT NULL DEFAULT '',
    turn_completed_at TEXT NOT NULL,
    human_authored INTEGER NOT NULL DEFAULT 1 CHECK (human_authored IN (0, 1)),
    human_qc_approved INTEGER NOT NULL DEFAULT 0 CHECK (human_qc_approved IN (0, 1)),
    human_qc_reviewer TEXT NOT NULL DEFAULT '',
    human_qc_approved_at TEXT NOT NULL DEFAULT '',
    delivery_qc_passed INTEGER NOT NULL DEFAULT 0 CHECK (delivery_qc_passed IN (0, 1)),
    delivery_qc_note TEXT NOT NULL DEFAULT '',
    delivery_qc_checked_at TEXT NOT NULL DEFAULT '',
    delivery_qc_changes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (session_id, turn_id),
    UNIQUE (question_id, turn_no)
);

CREATE INDEX IF NOT EXISTS idx_questions_batch ON questions(batch_id, question_no);
CREATE INDEX IF NOT EXISTS idx_records_question ON records(question_id, turn_no);

CREATE TABLE IF NOT EXISTS mother_library (
    id INTEGER PRIMARY KEY,
    source_question_id INTEGER NOT NULL UNIQUE REFERENCES questions(id) ON DELETE RESTRICT,
    source_batch TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    repo_url TEXT NOT NULL DEFAULT '',
    initial_snapshot TEXT NOT NULL DEFAULT '',
    local_initial_sha TEXT NOT NULL DEFAULT '',
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    last_used_at TEXT NOT NULL DEFAULT '',
    bugfix_ready INTEGER NOT NULL DEFAULT 1 CHECK (bugfix_ready IN (0, 1)),
    iteration_ready INTEGER NOT NULL DEFAULT 1 CHECK (iteration_ready IN (0, 1)),
    defect_note TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mother_usages (
    id INTEGER PRIMARY KEY,
    mother_id INTEGER NOT NULL REFERENCES mother_library(id) ON DELETE RESTRICT,
    derived_question_id INTEGER NOT NULL UNIQUE REFERENCES questions(id) ON DELETE RESTRICT,
    derived_task_type TEXT NOT NULL,
    used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mother_library_ready ON mother_library(bugfix_ready, iteration_ready, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mother_usages_mother ON mother_usages(mother_id, used_at DESC);

CREATE TABLE IF NOT EXISTS author_jobs (
    id INTEGER PRIMARY KEY,
    batch_name TEXT NOT NULL,
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    business TEXT NOT NULL DEFAULT '',
    technology TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    author_mode TEXT NOT NULL DEFAULT '0-1',
    task_type TEXT NOT NULL DEFAULT '0-1 代码生成',
    mother_id INTEGER,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_author_jobs_created ON author_jobs(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id INTEGER PRIMARY KEY,
    batch_name TEXT NOT NULL,
    question_count INTEGER NOT NULL CHECK (question_count > 0),
    qc_concurrency INTEGER NOT NULL DEFAULT 2,
    model_concurrency INTEGER NOT NULL DEFAULT 2,
    codex_concurrency INTEGER NOT NULL DEFAULT 2,
    docker_image TEXT NOT NULL,
    docker_command TEXT NOT NULL DEFAULT 'claude',
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    retry_of_job_id INTEGER REFERENCES pipeline_jobs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pipeline_items (
    id INTEGER PRIMARY KEY,
    pipeline_job_id INTEGER NOT NULL REFERENCES pipeline_jobs(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE RESTRICT,
    question_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    activity_at TEXT NOT NULL DEFAULT '',
    health_status TEXT NOT NULL DEFAULT '',
    health_detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    UNIQUE (pipeline_job_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_created ON pipeline_jobs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_items_job ON pipeline_items(pipeline_job_id, question_no);
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    feed_initialized = connection.execute(
        "SELECT 1 FROM schema_meta WHERE key='news_feeds_initialized'"
    ).fetchone()
    if feed_initialized is None:
        timestamp = now()
        connection.executemany(
            "INSERT OR IGNORE INTO news_feeds(url,enabled,created_at,updated_at) VALUES(?,?,?,?)",
            [(url, 1, timestamp, timestamp) for url in DEFAULT_NEWS_FEEDS],
        )
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('news_feeds_initialized','1')"
        )
    batch_columns = {row["name"] for row in connection.execute("PRAGMA table_info(batches)")}
    if "author_mode" not in batch_columns:
        connection.execute("ALTER TABLE batches ADD COLUMN author_mode TEXT NOT NULL DEFAULT '0-1'")
    if "mother_id" not in batch_columns:
        connection.execute("ALTER TABLE batches ADD COLUMN mother_id INTEGER")
    if "task_types" not in batch_columns:
        connection.execute("ALTER TABLE batches ADD COLUMN task_types TEXT NOT NULL DEFAULT '0-1 代码生成'")
    question_columns = {row["name"] for row in connection.execute("PRAGMA table_info(questions)")}
    if "author_mode" not in question_columns:
        connection.execute("ALTER TABLE questions ADD COLUMN author_mode TEXT NOT NULL DEFAULT '0-1'")
    if "mother_id" not in question_columns:
        connection.execute("ALTER TABLE questions ADD COLUMN mother_id INTEGER")
    # Backfill the mother catalog for databases created before the catalog existed.
    connection.execute(
        "INSERT OR IGNORE INTO mother_library(source_question_id, source_batch, source_task_id, title, prompt, workspace_path, "
        "repo_url, initial_snapshot, local_initial_sha, created_at, updated_at) "
        "SELECT q.id, b.name, q.task_id, q.title, q.prompt, q.folder_path, q.repo_url, q.initial_snapshot, q.local_initial_sha, "
        "q.created_at, q.updated_at FROM questions q JOIN batches b ON b.id=q.batch_id "
        "WHERE q.author_mode='0-1' AND q.task_type=?",
        (FIRST_TURN_TASK_TYPE,),
    )
    author_columns = {row["name"] for row in connection.execute("PRAGMA table_info(author_jobs)")}
    if "author_mode" not in author_columns:
        connection.execute("ALTER TABLE author_jobs ADD COLUMN author_mode TEXT NOT NULL DEFAULT '0-1'")
    if "task_type" not in author_columns:
        connection.execute("ALTER TABLE author_jobs ADD COLUMN task_type TEXT NOT NULL DEFAULT '0-1 代码生成'")
    if "mother_id" not in author_columns:
        connection.execute("ALTER TABLE author_jobs ADD COLUMN mother_id INTEGER")
    record_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(records)")
    }
    if "trajectory_file" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN trajectory_file TEXT NOT NULL DEFAULT ''"
        )
    if "human_qc_reviewer" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN human_qc_reviewer TEXT NOT NULL DEFAULT ''"
        )
    if "human_qc_approved_at" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN human_qc_approved_at TEXT NOT NULL DEFAULT ''"
        )
    if "delivery_qc_passed" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN delivery_qc_passed INTEGER NOT NULL DEFAULT 0 "
            "CHECK (delivery_qc_passed IN (0, 1))"
        )
    if "delivery_qc_note" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN delivery_qc_note TEXT NOT NULL DEFAULT ''"
        )
    if "delivery_qc_checked_at" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN delivery_qc_checked_at TEXT NOT NULL DEFAULT ''"
        )
    if "delivery_qc_changes" not in record_columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN delivery_qc_changes TEXT NOT NULL DEFAULT '[]'"
        )
    run_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(runs)")
    }
    if "harness" not in run_columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN harness TEXT NOT NULL DEFAULT 'Claude Code'"
        )
    if "harness_version" not in run_columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN harness_version TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "UPDATE runs SET harness_version=codex_version WHERE harness_version=''"
        )
    if "container_cwd" not in run_columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN container_cwd TEXT NOT NULL DEFAULT ''"
        )
    if "trajectory_root" not in run_columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN trajectory_root TEXT NOT NULL DEFAULT ''"
        )
    if "operating_system" not in run_columns:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN operating_system TEXT NOT NULL DEFAULT ''"
        )
    run_migrations = {
        "status": "TEXT NOT NULL DEFAULT 'registered'",
        "started_at": "TEXT NOT NULL DEFAULT ''",
        "finished_at": "TEXT NOT NULL DEFAULT ''",
        "exit_code": "INTEGER",
        "error_message": "TEXT NOT NULL DEFAULT ''",
        "container_id": "TEXT NOT NULL DEFAULT ''",
        "log_path": "TEXT NOT NULL DEFAULT ''",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in run_migrations.items():
        if name not in run_columns:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    pipeline_job_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(pipeline_jobs)")
    }
    if "pid" not in pipeline_job_columns:
        connection.execute("ALTER TABLE pipeline_jobs ADD COLUMN pid INTEGER")
    if "retry_of_job_id" not in pipeline_job_columns:
        connection.execute(
            "ALTER TABLE pipeline_jobs ADD COLUMN retry_of_job_id INTEGER REFERENCES pipeline_jobs(id) ON DELETE SET NULL"
        )
    pipeline_item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(pipeline_items)")
    }
    for name in ("heartbeat_at", "activity_at", "health_status", "health_detail"):
        if name not in pipeline_item_columns:
            connection.execute(
                f"ALTER TABLE pipeline_items ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def chinese_character_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))


def prompt_style_issues(prompt: str) -> list[str]:
    """Return objective style failures; semantic naturalness remains a QC judgment."""
    issues: list[str] = []
    stripped = prompt.strip()
    if chinese_character_count(stripped) < 20:
        issues.append("User Prompt 必须以中文书面语为主")
    if PROMPT_CANNED_OPENING_RE.search(stripped):
        issues.append("User Prompt 使用了“从零构建一套”式固定开头")
    if re.match(r"^(?:#{1,6}\s|[-*+]\s|\d+[.、)]\s*)", stripped):
        issues.append("User Prompt 不能使用标题或清单格式")
    if len(PROMPT_LABEL_RE.findall(stripped)) >= 2:
        issues.append("User Prompt 不能把背景、功能、技术、验收等标签串成模板")
    if re.search(r"评测模型|测试模型能力|用于评测|用于测评|标注数据", stripped):
        issues.append("User Prompt 不能暴露评测或标注用途")
    return issues


def tracked_workspace_files(folder: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(folder), "ls-files", "-z"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        return []
    return [folder / raw.decode("utf-8", errors="surrogateescape") for raw in result.stdout.split(b"\0") if raw]


def snapshot_content_issues(folder: Path) -> list[str]:
    """Check tracked project documents for language and evaluation leakage."""
    issues: list[str] = []
    for path in tracked_workspace_files(folder):
        relative = path.relative_to(folder).as_posix()
        if SNAPSHOT_META_FILENAME_RE.search(relative):
            issues.append(f"快照包含与项目无关的内部文件：{relative}")
        is_document = (
            path.suffix.lower() in DOCUMENT_SUFFIXES
            or path.name.casefold() in DOCUMENT_TEXT_NAMES
            or (path.suffix.lower() == ".txt" and "docs" in {part.casefold() for part in path.parts})
        )
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            issues.append(f"项目文档不是有效的 UTF-8 文本：{relative}")
            continue
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(content, filename=str(path))
            except SyntaxError:
                tree = None
            if tree is not None:
                docstrings: list[str] = []
                module_doc = ast.get_docstring(tree, clean=False)
                if module_doc:
                    docstrings.append(module_doc)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        doc = ast.get_docstring(node, clean=False)
                        if doc:
                            docstrings.append(doc)
                for docstring in docstrings:
                    if chinese_character_count(docstring) < 4 and len(re.findall(r"\b[A-Za-z]{2,}\b", docstring)) >= 3:
                        issues.append(f"Python 文档字符串应使用中文书面语：{relative}")
                    for label, pattern in SNAPSHOT_META_PATTERNS:
                        match = pattern.search(docstring)
                        if match:
                            issues.append(f"Python 文档字符串包含{label}：{relative}（{match.group(0)}）")
        if not is_document:
            for label, pattern in SNAPSHOT_META_PATTERNS:
                match = pattern.search(content)
                if match:
                    issues.append(f"项目源码包含{label}：{relative}（{match.group(0)}）")
            continue
        prose = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        prose = re.sub(r"`[^`]+`|https?://\S+", "", prose)
        chinese_count = chinese_character_count(prose)
        latin_words = len(re.findall(r"\b[A-Za-z]{2,}\b", prose))
        if chinese_count < 8 or chinese_count < latin_words // 2:
            issues.append(f"项目文档应以中文书面语为主：{relative}")
        for label, pattern in SNAPSHOT_META_PATTERNS:
            match = pattern.search(content)
            if match:
                issues.append(f"项目文档包含{label}：{relative}（{match.group(0)}）")
    return list(dict.fromkeys(issues))


def trigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", normalize(text))
    return {compact[index:index + 3] for index in range(max(0, len(compact) - 2))}


def trigram_similarity(left: str, right: str) -> float:
    left_set, right_set = trigrams(left), trigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def parse_selection(raw: str, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    by_number = {str(row["question_no"]): row for row in rows}
    by_id = {str(row["task_id"]): row for row in rows}
    selected: list[sqlite3.Row] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token in by_id:
            selected.append(by_id[token])
        elif token.isdigit() and token in by_number:
            selected.append(by_number[token])
        elif re.fullmatch(r"\d+-\d+", token):
            start, end = (int(value) for value in token.split("-", 1))
            if start > end:
                raise ValueError(f"invalid descending range: {token}")
            for number in range(start, end + 1):
                row = by_number.get(str(number))
                if row is None:
                    raise ValueError(f"question number does not exist: {number}")
                selected.append(row)
        else:
            raise ValueError(f"unknown question selection: {token}")
    if not selected:
        raise ValueError("selection is empty")
    unique: dict[int, sqlite3.Row] = {}
    for row in selected:
        unique[int(row["id"])] = row
    return list(unique.values())


def batch_row(connection: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM batches WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"batch does not exist: {name}")
    return row


def question_rows(connection: sqlite3.Connection, batch: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT q.* FROM questions q JOIN batches b ON b.id=q.batch_id "
        "WHERE b.name=? ORDER BY q.question_no",
        (batch,),
    ).fetchall()


def safe_folder_name(value: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or value.startswith("."):
        raise ValueError(f"invalid question folder name: {value!r}")
    return value


def initialize_git_workspace(folder: Path) -> str:
    ignore = """.env
*.key
*.pem
node_modules/
.venv/
venv/
dist/
build/
coverage/
__pycache__/
*.py[cod]
.DS_Store
"""
    (folder / ".gitignore").write_text(ignore, encoding="utf-8")
    result = subprocess.run(
        ["git", "-C", str(folder), "init", "-q", "-b", "main"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or f"git init failed in {folder}")
    subprocess.run(["git", "-C", str(folder), "add", ".gitignore"], check=True)
    result = subprocess.run(
        [
            "git", "-C", str(folder), "-c", "user.name=项目维护者",
            "-c", "user.email=project-maintainer@local.invalid", "commit", "-q",
            "-m", "初始化项目",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or f"initial commit failed in {folder}")
    sha = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "HEAD"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.strip()
    return sha


def workspace_snapshot(folder: Path) -> str:
    if not folder.is_dir() or not (folder / ".git").is_dir():
        raise ValueError("question workspace is not a Git repository")
    status = subprocess.run(
        ["git", "-C", str(folder), "status", "--porcelain"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if status.returncode:
        raise ValueError(status.stdout.strip() or "cannot inspect question workspace")
    if status.stdout.strip():
        raise ValueError("question workspace must be clean before snapshot registration")
    result = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "HEAD"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if result.returncode or not re.fullmatch(r"[0-9a-fA-F]{40}", result.stdout.strip()):
        raise ValueError(result.stdout.strip() or "question workspace has no valid HEAD")
    return result.stdout.strip()


def render_batch(connection: sqlite3.Connection, batch: str) -> Path:
    batch_data = batch_row(connection, batch)
    questions = question_rows(connection, batch)
    lines = [
        f"# 批次 {batch}", "", f"题目数量：{len(questions)}", "",
        f"出题要求：{batch_data['brief'] or '未填写'}", "",
        "> 本文件用于人工查看，production.sqlite3 是唯一数据源。", "",
    ]
    for row in questions:
        current = row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        ready = (
            row["mechanical_qc"] == "pass"
            and row["qc_decision"] == "pass"
            and row["status"] == "approved"
            and current
        )
        lines.extend([
            f"## {row['question_no']}. {row['title']}", "",
            f"- 任务 ID：`{row['task_id']}`",
            f"- 工作目录：`{row['folder_name']}`",
            f"- 任务类型：{row['task_type']}",
            f"- 难度：{row['difficulty']}",
            f"- 语言/框架：{row['languages']}",
            f"- 机械质检：{row['mechanical_qc']}",
            f"- 出题质检：{row['qc_decision']}",
            f"- 启动状态：{'可运行' if ready else '已阻塞'}", "", "### 用户需求", "", row["prompt"], "", "---", "",
        ])
    path = Path(batch_data["markdown_path"])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def create_batch(connection: sqlite3.Connection, workspace: Path, spec_path: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or not isinstance(spec.get("questions"), list):
        raise ValueError("spec must contain a questions array")
    name = str(spec.get("batch", "")).strip()
    if not SAFE_BATCH_RE.fullmatch(name):
        raise ValueError("batch name must use 2-32 letters, digits, _ or -")
    questions = spec["questions"]
    if not questions:
        raise ValueError("questions array is empty")
    batch_dir = (workspace / name).absolute()
    try:
        batch_dir.relative_to(workspace.absolute())
    except ValueError as exc:
        raise ValueError("batch directory must stay inside the workspace") from exc
    if batch_dir.exists():
        raise FileExistsError(f"batch directory already exists: {batch_dir}")
    author_mode = str(spec.get("author_mode", "0-1")).strip() or "0-1"
    if author_mode not in {"0-1", "derived"}:
        raise ValueError("author_mode must be 0-1 or derived")
    mother_id = spec.get("mother_id")
    if author_mode == "derived":
        if isinstance(mother_id, bool) or not isinstance(mother_id, int) or mother_id <= 0:
            raise ValueError("derived batches require a valid mother_id")
        mother = connection.execute("SELECT * FROM mother_library WHERE id=?", (mother_id,)).fetchone()
        if mother is None:
            raise ValueError("mother_id does not exist")
    else:
        mother_id = None

    seen_folders: set[str] = set()
    prepared = []
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"question {index} must be an object")
        folder_name = safe_folder_name(str(item.get("folder", f"q{index:03d}")))
        if folder_name in seen_folders:
            raise ValueError(f"duplicate folder name: {folder_name}")
        seen_folders.add(folder_name)
        prompt = str(item.get("prompt", "")).strip()
        title = str(item.get("title", "")).strip()
        task_type = str(item.get("task_type", "")).strip()
        difficulty = str(item.get("difficulty", "")).strip()
        languages = item.get("languages", [])
        reproducibility = str(item.get("reproducibility", "无外部依赖")).strip()
        if not prompt or not title:
            raise ValueError(f"question {index} requires title and prompt")
        if "\n" in prompt or "\r" in prompt:
            raise ValueError(f"question {index} first-turn prompt must be one paragraph")
        if task_type not in TASK_TYPES:
            raise ValueError(f"question {index} has invalid task_type")
        if author_mode == "0-1" and task_type != FIRST_TURN_TASK_TYPE:
            raise ValueError(
                f"question {index} first-turn task_type must be {FIRST_TURN_TASK_TYPE}"
            )
        if author_mode == "derived" and task_type == FIRST_TURN_TASK_TYPE:
            raise ValueError(f"question {index} derived task_type cannot be {FIRST_TURN_TASK_TYPE}")
        if author_mode == "derived" and task_type == "Bug 修复" and not mother["bugfix_ready"]:
            raise ValueError("selected mother is not ready for Bug 修复")
        if author_mode == "derived" and task_type == "Feature 迭代" and not mother["iteration_ready"]:
            raise ValueError("selected mother is not ready for Feature 迭代")
        if difficulty not in DIFFICULTIES or difficulty == "简单":
            raise ValueError(f"question {index} first-turn difficulty must be 中等/困难/地狱")
        if not isinstance(languages, list) or not languages or not all(
            isinstance(value, str) and value.strip() for value in languages
        ):
            raise ValueError(f"question {index} languages must be a non-empty string array")
        if reproducibility not in REPRODUCIBILITY:
            raise ValueError(f"question {index} has invalid reproducibility")
        prepared.append((index, item, folder_name, title, prompt, task_type, difficulty, languages, reproducibility))

    batch_dir.mkdir(parents=True)
    timestamp = now()
    markdown_path = batch_dir / f"题目_{name}.md"
    try:
        cursor = connection.execute(
            "INSERT INTO batches(name, folder_path, markdown_path, brief, question_count, author_mode, mother_id, task_types, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, str(batch_dir), str(markdown_path), str(spec.get("brief", "")).strip(), len(prepared), author_mode, mother_id,
             ", ".join(sorted({item[5] for item in prepared})), timestamp, timestamp),
        )
        batch_id = cursor.lastrowid
        for index, item, folder_name, title, prompt, task_type, difficulty, languages, reproducibility in prepared:
            folder = batch_dir / folder_name
            folder.mkdir()
            local_sha = initialize_git_workspace(folder)
            task_id = str(item.get("task_id", f"{name}-{index:03d}")).strip()
            connection.execute(
                """INSERT INTO questions(
                    batch_id, question_no, task_id, folder_name, folder_path, title, prompt,
                    prompt_sha256, task_type, author_mode, mother_id, difficulty, languages, repo_url, initial_snapshot,
                    local_initial_sha, reproducibility, expected_areas, difficulty_evidence,
                    similarity_tags, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id, index, task_id, folder_name, str(folder), title, prompt,
                    prompt_hash(prompt), task_type, author_mode, mother_id, difficulty, ", ".join(languages),
                    str(item.get("repo_url", "")).strip(),
                    str(item.get("initial_snapshot", "")).strip(), local_sha, reproducibility,
                    json.dumps(item.get("expected_areas", []), ensure_ascii=False),
                    json.dumps(item.get("difficulty_evidence", []), ensure_ascii=False),
                    json.dumps(item.get("similarity_tags", []), ensure_ascii=False),
                    timestamp, timestamp,
                ),
            )
            if author_mode == "0-1":
                connection.execute(
                    "INSERT INTO mother_library(source_question_id, source_batch, source_task_id, title, prompt, workspace_path, created_at, updated_at) "
                    "SELECT id, ?, task_id, title, prompt, folder_path, ?, ? FROM questions WHERE batch_id=? AND question_no=?",
                    (name, timestamp, timestamp, batch_id, index),
                )
        if author_mode == "derived":
            question_ids = connection.execute(
                "SELECT id FROM questions WHERE batch_id=? ORDER BY question_no", (batch_id,)
            ).fetchall()
            for row in question_ids:
                connection.execute(
                    "INSERT INTO mother_usages(mother_id, derived_question_id, derived_task_type, used_at) "
                    "SELECT ?, ?, task_type, ? FROM questions WHERE id=?",
                    (mother_id, row["id"], timestamp, row["id"]),
                )
            connection.execute(
                "UPDATE mother_library SET use_count=use_count+?, last_used_at=?, updated_at=? WHERE id=?",
                (len(question_ids), timestamp, timestamp, mother_id),
            )
        connection.commit()
        render_batch(connection, name)
    except Exception:
        connection.rollback()
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        raise
    print(f"Created batch {name}: {len(prepared)} questions")
    print(f"Folder: {batch_dir}")
    print(f"Markdown: {markdown_path}")


def check_question(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    prompt = row["prompt"].strip()
    combined = normalize(row["title"] + "\n" + prompt)
    if "\n" in prompt or "\r" in prompt:
        errors.append("首轮 User Prompt 必须是一个自然语言段落")
    errors.extend(prompt_style_issues(prompt))
    if row["difficulty"] == "简单":
        errors.append("首轮题目不能是简单")
    banned = sorted(term for term in BANNED_TERMS if term in combined)
    if re.search(r"\bcli\b", combined):
        banned.append("CLI")
    if banned:
        errors.append("命中禁题或饱和题材：" + "、".join(banned))
    folder = Path(row["folder_path"])
    if not folder.is_dir() or not (folder / ".git").is_dir():
        errors.append("题目工作目录不存在或不是 Git 仓库")
    elif subprocess.run(
        ["git", "-C", str(folder), "status", "--porcelain"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    ).stdout.strip():
        errors.append("题目工作目录不是干净快照")
    elif folder.is_dir() and (folder / ".git").is_dir():
        errors.extend(snapshot_content_issues(folder))
    if not SNAPSHOT_RE.fullmatch(row["initial_snapshot"]):
        errors.append("缺少可访问的 GitHub 40 位初始快照链接")
    elif folder.is_dir() and (folder / ".git").is_dir():
        try:
            local_sha = workspace_snapshot(folder)
            if row["initial_snapshot"].rsplit("/", 1)[-1].lower() != local_sha.lower():
                errors.append("GitHub 初始快照 SHA 与题目工作目录 HEAD 不一致")
        except ValueError as exc:
            errors.append(str(exc))
    expected = json.loads(row["expected_areas"])
    evidence = json.loads(row["difficulty_evidence"])
    tags = json.loads(row["similarity_tags"])
    if not expected:
        errors.append("expected_areas 为空")
    if not evidence:
        errors.append("difficulty_evidence 为空")
    if not tags:
        errors.append("similarity_tags 为空")

    others = connection.execute(
        "SELECT task_id, prompt, repo_url, similarity_tags FROM questions WHERE id != ?",
        (row["id"],),
    ).fetchall()
    for other in others:
        ratio = difflib.SequenceMatcher(None, normalize(prompt), normalize(other["prompt"])).ratio()
        tri = trigram_similarity(prompt, other["prompt"])
        longest = difflib.SequenceMatcher(
            None, normalize(prompt), normalize(other["prompt"])
        ).find_longest_match().size
        if ratio >= 0.82 or tri >= 0.30 or longest >= 50:
            errors.append(
                f"与 {other['task_id']} 高度相似（整体 {ratio:.0%}，三字片段 {tri:.0%}，最长连续 {longest} 字）"
            )
        other_tags = set(json.loads(other["similarity_tags"]))
        current_tags = set(tags)
        if current_tags and other_tags:
            overlap = len(current_tags & other_tags) / len(current_tags | other_tags)
            if overlap >= 0.75 and row["repo_url"] == other["repo_url"]:
                errors.append(f"与同仓库题目 {other['task_id']} 的相似标签重合 {overlap:.0%}")
    return {
        "task_id": row["task_id"],
        "question_no": row["question_no"],
        "prompt_sha256": prompt_hash(prompt),
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def check_duplicates(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
    prompt = row["prompt"].strip()
    current_tags = set(json.loads(row["similarity_tags"]))
    comparisons = []
    for other in connection.execute(
        "SELECT task_id, prompt, repo_url, similarity_tags FROM questions WHERE id != ?",
        (row["id"],),
    ).fetchall():
        ratio = difflib.SequenceMatcher(None, normalize(prompt), normalize(other["prompt"])).ratio()
        tri = trigram_similarity(prompt, other["prompt"])
        longest = difflib.SequenceMatcher(
            None, normalize(prompt), normalize(other["prompt"])
        ).find_longest_match().size
        other_tags = set(json.loads(other["similarity_tags"]))
        tag_overlap = 0.0
        if current_tags and other_tags:
            tag_overlap = len(current_tags & other_tags) / len(current_tags | other_tags)
        same_repo = bool(row["repo_url"]) and row["repo_url"] == other["repo_url"]
        reasons = []
        if ratio >= 0.82:
            reasons.append("overall_similarity")
        if tri >= 0.30:
            reasons.append("trigram_similarity")
        if longest >= 50:
            reasons.append("longest_common_substring")
        if same_repo and tag_overlap >= 0.75:
            reasons.append("same_repo_tag_overlap")
        comparisons.append({
            "task_id": other["task_id"],
            "overall_similarity": round(ratio, 4),
            "trigram_similarity": round(tri, 4),
            "longest_common_substring": longest,
            "same_repository": same_repo,
            "tag_overlap": round(tag_overlap, 4),
            "duplicate_reasons": reasons,
        })
    comparisons.sort(
        key=lambda item: (
            bool(item["duplicate_reasons"]),
            item["trigram_similarity"],
            item["overall_similarity"],
            item["longest_common_substring"],
        ),
        reverse=True,
    )
    return {
        "task_id": row["task_id"],
        "question_no": row["question_no"],
        "prompt_sha256": prompt_hash(prompt),
        "duplicates": [item for item in comparisons if item["duplicate_reasons"]],
        "closest": comparisons[:3],
    }


def run_duplicate_qc(
    connection: sqlite3.Connection, batch: str, selection: str | None
) -> int:
    batch_row(connection, batch)
    rows = question_rows(connection, batch)
    if selection:
        rows = parse_selection(selection, rows)
    reports = [check_duplicates(connection, row) for row in rows]
    print(json.dumps({"batch": batch, "questions": reports}, ensure_ascii=False, indent=2))
    return 1 if any(report["duplicates"] for report in reports) else 0


def run_mechanical_qc(connection: sqlite3.Connection, batch: str, selection: str | None) -> int:
    rows = question_rows(connection, batch)
    if selection:
        rows = parse_selection(selection, rows)
    reports = []
    for row in rows:
        report = check_question(connection, row)
        reports.append(report)
        state = "pass" if not report["errors"] else "reject"
        connection.execute(
            "UPDATE questions SET mechanical_qc=?, qc_decision=CASE WHEN ?='reject' THEN 'reject' ELSE 'pending' END, "
            "qc_report=?, qc_prompt_sha256='', human_approved=0, status='draft', updated_at=? WHERE id=?",
            (state, state, json.dumps(report, ensure_ascii=False), now(), row["id"]),
        )
    connection.commit()
    render_batch(connection, batch)
    print(json.dumps({"batch": batch, "questions": reports}, ensure_ascii=False, indent=2))
    return 1 if any(report["errors"] for report in reports) else 0


def set_semantic_qc(
    connection: sqlite3.Connection, batch: str, selection: str, decision: str, report_text: str
) -> None:
    if decision not in {"pass", "revise", "reject"}:
        raise ValueError("decision must be pass, revise, or reject")
    if not report_text.strip():
        raise ValueError("semantic QC report must contain concrete evidence")
    if decision == "pass":
        report_text = "质检通过"
    rows = parse_selection(selection, question_rows(connection, batch))
    for row in rows:
        if decision == "pass" and row["mechanical_qc"] != "pass":
            raise ValueError(f"{row['task_id']}: mechanical QC has not passed")
        connection.execute(
            "UPDATE questions SET qc_decision=?, qc_report=?, qc_prompt_sha256=?, "
            "human_approved=0, human_reviewer='', approved_at='', status=?, updated_at=? WHERE id=?",
            (
                decision, report_text, prompt_hash(row["prompt"]),
                "approved" if decision == "pass" else ("rejected" if decision == "reject" else "draft"),
                now(), row["id"],
            ),
        )
    connection.commit()
    render_batch(connection, batch)


def approve_questions(connection: sqlite3.Connection, batch: str, selection: str, reviewer: str) -> None:
    if not reviewer.strip():
        raise ValueError("reviewer is required")
    rows = parse_selection(selection, question_rows(connection, batch))
    timestamp = now()
    for row in rows:
        if row["mechanical_qc"] != "pass" or row["qc_decision"] != "pass":
            raise ValueError(f"{row['task_id']}: question QC has not passed")
        if row["qc_prompt_sha256"] != prompt_hash(row["prompt"]):
            raise ValueError(f"{row['task_id']}: prompt changed after QC")
        connection.execute(
            "UPDATE questions SET human_approved=1, human_reviewer=?, approved_at=?, "
            "status='approved', updated_at=? WHERE id=?",
            (reviewer.strip(), timestamp, timestamp, row["id"]),
        )
    connection.commit()
    render_batch(connection, batch)


def set_repository(
    connection: sqlite3.Connection, batch: str, number: int, repo_url: str, snapshot: str
) -> None:
    if not SNAPSHOT_RE.fullmatch(snapshot):
        raise ValueError("snapshot must be a GitHub commit permalink with a 40-character SHA")
    row = connection.execute(
        "SELECT q.id, q.folder_path FROM questions q JOIN batches b ON b.id=q.batch_id "
        "WHERE b.name=? AND q.question_no=?",
        (batch, number),
    ).fetchone()
    if row is None:
        raise ValueError("question does not exist")
    normalized_repo = repo_url.strip().rstrip("/")
    if not normalized_repo.startswith("https://github.com/"):
        raise ValueError("repo_url must be an https://github.com repository URL")
    if not snapshot.startswith(normalized_repo + "/commit/"):
        raise ValueError("repo_url and snapshot must refer to the same repository")
    local_sha = workspace_snapshot(Path(row["folder_path"]))
    if snapshot.rsplit("/", 1)[-1].lower() != local_sha.lower():
        raise ValueError("snapshot SHA must match the question workspace HEAD")
    connection.execute(
        "UPDATE questions SET repo_url=?, initial_snapshot=?, local_initial_sha=?, mechanical_qc='pending', "
        "qc_decision='pending', qc_prompt_sha256='', human_approved=0, status='draft', updated_at=? WHERE id=?",
        (normalized_repo, snapshot, local_sha, now(), row["id"]),
    )
    connection.execute(
        "UPDATE mother_library SET repo_url=?, initial_snapshot=?, local_initial_sha=?, updated_at=? "
        "WHERE source_question_id=?",
        (normalized_repo, snapshot, local_sha, now(), row["id"]),
    )
    connection.commit()
    render_batch(connection, batch)


def update_prompt(
    connection: sqlite3.Connection, batch: str, number: int, prompt: str
) -> None:
    """Replace a stored prompt and invalidate every dependent QC fingerprint."""
    normalized = prompt.strip()
    if not normalized:
        raise ValueError("prompt must not be empty")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("prompt must be one paragraph")
    row = connection.execute(
        "SELECT q.id, q.author_mode FROM questions q JOIN batches b ON b.id=q.batch_id "
        "WHERE b.name=? AND q.question_no=?",
        (batch, number),
    ).fetchone()
    if row is None:
        raise ValueError("question does not exist")
    timestamp = now()
    connection.execute(
        "UPDATE questions SET prompt=?, prompt_sha256=?, mechanical_qc='pending', "
        "qc_decision='pending', qc_report='{}', qc_prompt_sha256='', human_approved=0, "
        "human_reviewer='', approved_at='', status='draft', updated_at=? WHERE id=?",
        (normalized, prompt_hash(normalized), timestamp, row["id"]),
    )
    if row["author_mode"] == "0-1":
        connection.execute(
            "UPDATE mother_library SET prompt=?, updated_at=? WHERE source_question_id=?",
            (normalized, timestamp, row["id"]),
        )
    connection.commit()
    render_batch(connection, batch)


def list_batches(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name, question_count, folder_path FROM batches ORDER BY created_at"
    ).fetchall()
    for row in rows:
        ready = sum(
            question["mechanical_qc"] == "pass"
            and question["qc_decision"] == "pass"
            and question["status"] == "approved"
            and question["qc_prompt_sha256"] == prompt_hash(question["prompt"])
            for question in question_rows(connection, row["name"])
        )
        print(f"{row['name']:<12} {ready}/{row['question_count']} READY  {row['folder_path']}")


def list_mothers(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT id, source_batch, source_task_id, title, workspace_path, repo_url, initial_snapshot, "
        "use_count, last_used_at, bugfix_ready, iteration_ready FROM mother_library ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))


def list_questions(connection: sqlite3.Connection, batch: str) -> None:
    for row in question_rows(connection, batch):
        current = row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        ready = (
            row["mechanical_qc"] == "pass" and row["qc_decision"] == "pass"
            and row["status"] == "approved" and current
        )
        mark = "READY" if ready else "BLOCKED"
        print(f"{row['question_no']:>3}  {mark:<7}  {row['task_id']:<14}  {row['title']}  [{row['folder_name']}]")


def relocate_paths(
    connection: sqlite3.Connection, workspace: Path, batch_name: str | None = None
) -> int:
    """Rebind stored absolute paths after copying the project to another host."""
    workspace = workspace.resolve(strict=True)
    if batch_name:
        batches = connection.execute(
            "SELECT id,name FROM batches WHERE name=? ORDER BY id", (batch_name,)
        ).fetchall()
        if not batches:
            raise ValueError(f"batch does not exist: {batch_name}")
    else:
        batches = connection.execute("SELECT id,name FROM batches ORDER BY id").fetchall()
    updated = 0
    for batch in batches:
        folder = workspace / batch["name"]
        markdown = folder / f"题目_{batch['name']}.md"
        if not folder.is_dir():
            raise ValueError(f"batch folder does not exist under new workspace: {folder}")
        connection.execute(
            "UPDATE batches SET folder_path=?,markdown_path=?,updated_at=? WHERE id=?",
            (str(folder), str(markdown), now(), batch["id"]),
        )
        questions = connection.execute(
            "SELECT id,folder_name FROM questions WHERE batch_id=?", (batch["id"],)
        ).fetchall()
        for question in questions:
            question_folder = folder / question["folder_name"]
            if not question_folder.is_dir():
                raise ValueError(
                    f"question folder does not exist under new workspace: {question_folder}"
                )
            connection.execute(
                "UPDATE questions SET folder_path=?,updated_at=? WHERE id=?",
                (str(question_folder), now(), question["id"]),
            )
            updated += 1
    connection.commit()
    return updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    create_parser = subparsers.add_parser("create-batch")
    create_parser.add_argument("--spec", type=Path, required=True)
    create_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--batch")
    subparsers.add_parser("mother-list")
    qc_parser = subparsers.add_parser("qc-check")
    qc_parser.add_argument("--batch", required=True)
    qc_parser.add_argument("--select")
    duplicate_parser = subparsers.add_parser("duplicate-check")
    duplicate_parser.add_argument("--batch", required=True)
    duplicate_parser.add_argument("--select")
    set_qc_parser = subparsers.add_parser("qc-set")
    set_qc_parser.add_argument("--batch", required=True)
    set_qc_parser.add_argument("--select", required=True)
    set_qc_parser.add_argument("--decision", required=True)
    set_qc_parser.add_argument("--report", default="")
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("--batch", required=True)
    approve_parser.add_argument("--select", required=True)
    approve_parser.add_argument("--reviewer", required=True)
    repo_parser = subparsers.add_parser("set-repo")
    repo_parser.add_argument("--batch", required=True)
    repo_parser.add_argument("--number", type=int, required=True)
    repo_parser.add_argument("--repo-url", required=True)
    repo_parser.add_argument("--snapshot", required=True)
    prompt_parser = subparsers.add_parser("prompt-update")
    prompt_parser.add_argument("--batch", required=True)
    prompt_parser.add_argument("--number", type=int, required=True)
    prompt_parser.add_argument("--prompt-file", type=Path, required=True)
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--batch", required=True)
    relocate_parser = subparsers.add_parser("relocate")
    relocate_parser.add_argument("--workspace", type=Path, required=True)
    relocate_parser.add_argument("--batch")

    args = parser.parse_args()
    try:
        connection = connect(args.db.resolve())
        if args.command == "init":
            print(f"Initialized {args.db.resolve()}")
        elif args.command == "create-batch":
            create_batch(connection, args.workspace.resolve(), args.spec.resolve())
        elif args.command == "list":
            list_questions(connection, args.batch) if args.batch else list_batches(connection)
        elif args.command == "mother-list":
            list_mothers(connection)
        elif args.command == "qc-check":
            return run_mechanical_qc(connection, args.batch, args.select)
        elif args.command == "duplicate-check":
            return run_duplicate_qc(connection, args.batch, args.select)
        elif args.command == "qc-set":
            set_semantic_qc(connection, args.batch, args.select, args.decision, args.report)
            print(f"Stored {args.decision} question QC for {args.batch}: {args.select}")
        elif args.command == "approve":
            approve_questions(connection, args.batch, args.select, args.reviewer)
            print(f"Approved {args.batch}: {args.select}")
        elif args.command == "set-repo":
            set_repository(connection, args.batch, args.number, args.repo_url, args.snapshot)
            print(f"Updated repository metadata for {args.batch} question {args.number}")
        elif args.command == "prompt-update":
            prompt = args.prompt_file.read_text(encoding="utf-8")
            update_prompt(connection, args.batch, args.number, prompt)
            print(f"Updated prompt for {args.batch} question {args.number}; QC reset")
        elif args.command == "render":
            print(render_batch(connection, args.batch))
        elif args.command == "relocate":
            count = relocate_paths(connection, args.workspace, args.batch)
            print(f"Relocated {count} question paths under {args.workspace.resolve()}")
        connection.close()
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
