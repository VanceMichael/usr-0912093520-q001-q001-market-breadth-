#!/usr/bin/env python3
"""Serve the local CC USR production console without external dependencies."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import webbrowser
from contextlib import closing
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
DEFAULT_DATABASE = PROJECT_ROOT / "production.sqlite3"
QUESTION_ID_RE = re.compile(r"^\d+$")
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SNAPSHOT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/commit/[0-9a-fA-F]{40}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_KEYS = ("CC_SWITCH_BASE_URL", "CC_SWITCH_MODEL", "CC_SWITCH_API_KEY", "CC_USR_SUBMITTER")
PIPELINE_ENV_KEYS = (
    "CC_CLAUDE_DOCKER_IMAGE", "CC_CLAUDE_DOCKER_COMMAND",
    "CC_PIPELINE_MODEL_MODE",
    "CC_PIPELINE_QC_CONCURRENCY", "CC_PIPELINE_MODEL_CONCURRENCY",
    "CC_PIPELINE_CODEX_CONCURRENCY",
)
AUTHOR_JOB_OUTPUT_LIMIT = 200_000
AUTHOR_JOB_STATUSES = {"queued", "running", "completed", "failed", "interrupted"}

sys.path.insert(0, str(PROJECT_ROOT))
from tools.runtime_environment import docker_info, repair_docker_engine  # noqa: E402
from tools.batch_pipeline import SCHEMA_VERSION, connect as initialize_database  # noqa: E402

AUTHOR_JOB_SCHEMA = """
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
    model_mode TEXT NOT NULL DEFAULT 'local',
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


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


def json_value(raw: object, fallback: object) -> object:
    if not isinstance(raw, str):
        return fallback
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    return parsed


def runtime_info() -> dict[str, str]:
    if sys.platform == "darwin":
        return {"platform": "macOS", "terminal": "iTerm2"}
    if sys.platform == "win32":
        return {"platform": "Windows", "terminal": "PowerShell"}
    return {"platform": "Linux", "terminal": "system terminal"}


def safe_console_print(message: object, *, file: object = sys.stdout) -> None:
    """Print pipeline output without letting a Windows code page abort a job."""
    text = str(message)
    try:
        print(text, file=file)
    except UnicodeEncodeError:
        print(text.encode("ascii", "backslashreplace").decode("ascii"), file=file)


def open_local_path(target: Path, cwd: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(target)], cwd=cwd)
        return
    if sys.platform == "win32":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise RuntimeError("当前 Python 环境不支持打开目录")
        startfile(str(target))
        return
    opener = shutil.which("xdg-open")
    if not opener:
        raise RuntimeError("未找到可用的目录打开程序")
    subprocess.Popen([opener, str(target)], cwd=cwd)


def secure_file(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run(
        ["whoami"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=flags, check=False,
    )
    account = identity.stdout.strip()
    if identity.returncode or not account:
        raise OSError(identity.stdout.strip() or "无法确定当前 Windows 用户")
    acl = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=flags, check=False,
    )
    if acl.returncode:
        raise OSError(acl.stdout.strip() or "无法限制 .env 文件权限")


class ConsoleData:
    def __init__(self, database: Path, project_root: Path = PROJECT_ROOT) -> None:
        # Preserve the caller's absolute spelling (notably /var vs /private on macOS)
        # so generated commands and UI paths match the workspace the user selected.
        self.database = Path(database).absolute()
        self.project_root = Path(project_root).absolute()
        self.env_file = self.project_root / ".env"
        self.author_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="codex-author")
        self.pipeline_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
        self._pipeline_processes: dict[int, subprocess.Popen[str]] = {}
        self._pipeline_process_lock = threading.Lock()
        if self.database.is_file():
            connection = initialize_database(self.database)
            connection.close()
        self._initialize_author_jobs()
        self._initialize_pipeline_jobs()

    def _write_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_author_jobs(self) -> None:
        if not self.database.is_file():
            return
        with closing(self._write_connection()) as connection:
            connection.executescript(AUTHOR_JOB_SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(author_jobs)")}
            if "author_mode" not in columns:
                connection.execute("ALTER TABLE author_jobs ADD COLUMN author_mode TEXT NOT NULL DEFAULT '0-1'")
            if "task_type" not in columns:
                connection.execute("ALTER TABLE author_jobs ADD COLUMN task_type TEXT NOT NULL DEFAULT '0-1 代码生成'")
            if "mother_id" not in columns:
                connection.execute("ALTER TABLE author_jobs ADD COLUMN mother_id INTEGER")
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                "UPDATE author_jobs SET status='interrupted', error=?, finished_at=? "
                "WHERE status IN ('queued','running')",
                ("控制台服务已重启，任务执行中断", timestamp),
            )
            connection.commit()

    def _initialize_pipeline_jobs(self) -> None:
        if not self.database.is_file():
            return
        active_ids: list[int] = []
        with closing(self._write_connection()) as connection:
            connection.executescript(AUTHOR_JOB_SCHEMA)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(pipeline_jobs)")
            }
            if "pid" not in columns:
                connection.execute("ALTER TABLE pipeline_jobs ADD COLUMN pid INTEGER")
            if "retry_of_job_id" not in columns:
                connection.execute(
                    "ALTER TABLE pipeline_jobs ADD COLUMN retry_of_job_id INTEGER REFERENCES pipeline_jobs(id) ON DELETE SET NULL"
                )
            if "model_mode" not in columns:
                connection.execute(
                    "ALTER TABLE pipeline_jobs ADD COLUMN model_mode TEXT NOT NULL DEFAULT 'local'"
                )
            item_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(pipeline_items)")
            }
            for name in ("heartbeat_at", "activity_at", "health_status", "health_detail"):
                if name not in item_columns:
                    connection.execute(
                        f"ALTER TABLE pipeline_items ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            active_ids = [
                int(row["id"]) for row in connection.execute(
                    "SELECT id FROM pipeline_jobs WHERE status IN ('queued','running')"
                )
            ]
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                connection.execute(
                    f"UPDATE pipeline_items SET status='interrupted', finished_at=? "
                    f"WHERE pipeline_job_id IN ({placeholders}) "
                    "AND status IN ('queued','qc_running','model_running','producing','finalizing')",
                    (timestamp, *active_ids),
                )
            connection.execute(
                "UPDATE pipeline_jobs SET status='interrupted', error=?, pid=NULL, finished_at=? "
                "WHERE status IN ('queued','running')",
                ("控制台服务已重启，任务执行中断", timestamp),
            )
            connection.commit()
        self._cleanup_pipeline_containers(active_ids)

    @staticmethod
    def author_prompt(batch: str, count: int, business: str, technology: str, notes: str,
                      mode: str = "0-1", task_type: str = "0-1 代码生成", mother: dict | None = None,
                      derived_notes: str = "", defect_tolerance: str = "") -> str:
        technology_label = "Docker 要求" if technology in {"需要 Docker", "不需要 Docker"} else "技术关键词"
        requirements = "；".join(filter(None, (
            f"业务关键词：{business}" if business else "",
            f"{technology_label}：{technology}" if technology else "",
            f"补充要求：{notes}" if notes else "",
        )))
        if mode == "derived":
            mother_text = "；".join(filter(None, (
                f"母库编号：{mother.get('id')}" if mother else "",
                f"母项目：{mother.get('title')}" if mother else "",
                f"代码路径：{mother.get('workspace_path')}" if mother else "",
                f"Git 地址：{mother.get('repo_url')}" if mother else "",
                f"母题已使用：{mother.get('use_count', 0)} 次" if mother else "",
            )))
            return (
                "在项目根目录执行派生出题任务。先读取项目规范和 cc-usr-question-author 的全部引用，"
                "使用母库中的 0-1 母项目生成独立的非 0-1 题目批次。\n"
                f"批次名：{batch}\n题目数量：{count}\n题型：{task_type}\n"
                f"母库信息：{mother_text}\n出题要求：{requirements or '根据母项目代码、已登记快照和《项目规范.md》自动生成，不需要额外填写关键词。'}\n"
                f"派生方向：{derived_notes or '围绕母项目已有业务设计真实的后续工作'}\n"
                f"可接受的小瑕疵：{defect_tolerance or '允许不影响构建和主要流程的小问题，并将其记录为可迭代方向'}\n"
                "保留母项目路径、Git 地址、初始快照和派生使用关系；每道题使用独立工作区和独立 Prompt，"
                "完成快照发布、机械质检和重复题质检。不要读取任何目标模型轨迹、回复或产物，也不要启动目标模型。"
            )
        return (
            "在项目根目录执行出题任务。先完整读取 项目规范.md、"
            ".agents/skills/cc-usr-question-author/SKILL.md 和 "
            ".agents/skills/cc-usr-question-author/references/task-contract.md、"
            ".agents/skills/cc-usr-question-author/references/content-quality.md，"
            "然后严格使用 cc-usr-question-author 的现有 SQLite 出题流程。\n"
            f"批次名：{batch}\n题目数量：{count}\n出题要求：{requirements}\n"
            "创建完整批次和独立题目工作区，准备并提交干净 baseline，创建并推送可访问的 "
            "GitHub 仓库，登记精确的 40 位 SHA 快照，完成机械质检和重复题质检。"
            "每个 User Prompt 都要写成自然、具体的中文书面需求，不用标题、清单、固定开头或可换名复用的句式。"
            "快照中的 README、设计说明、注释、文档字符串、示例说明和界面文字必须以中文为主，"
            "内容只能描述项目本身，不得出现题目、评测、标注、目标模型、轨迹、质检、难度或脚手架交付口吻。"
            "不得启动 Claude Code 或其他目标模型；当前 Codex CLI 只负责执行本次出题流程。"
            "不得伪造 GitHub、快照或质检结果。"
            "遇到外部权限或发布失败时保持阻塞并引用实际命令或响应说明原因。"
            "所有面向页面的过程说明和最终结论使用自然、克制的中文书面语，最后给出有证据的简短阶段总结。"
        )

    @staticmethod
    def _validate_author_fields(body: dict[str, object]) -> tuple[str, int, str, str, str, str, str, int | None, str, str]:
        batch = str(body.get("batch", "")).strip()
        if not BATCH_RE.fullmatch(batch):
            raise ValueError("批次名必须使用 2-32 位字母、数字、下划线或连字符")
        count = body.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise ValueError("题目数量必须是 1-20 的整数")
        values = []
        for key, limit in (("business", 500), ("technology", 500), ("notes", 3000)):
            value = body.get(key, "")
            if not isinstance(value, str) or "\x00" in value or len(value) > limit:
                raise ValueError(f"{key} 内容无效")
            values.append(" ".join(value.split()))
        mode = str(body.get("mode", "0-1")).strip() or "0-1"
        if mode not in {"0-1", "derived"}:
            raise ValueError("出题模式无效")
        if mode == "0-1" and not values[0]:
            raise ValueError("业务关键词不能为空")
        task_type = str(body.get("task_type", "0-1 代码生成")).strip()
        if mode == "0-1" and task_type != "0-1 代码生成":
            raise ValueError("0-1 模式只能使用 0-1 代码生成")
        if mode == "derived" and task_type not in {"Feature 迭代", "Bug 修复", "代码理解", "代码重构", "工程化", "代码测试"}:
            raise ValueError("派生模式请选择非 0-1 题型")
        mother_id = body.get("mother_id")
        if mode == "derived":
            if isinstance(mother_id, bool) or not isinstance(mother_id, int) or mother_id <= 0:
                raise ValueError("派生模式必须选择母库项目")
        else:
            mother_id = None
        extra = []
        for key, limit in (("derived_notes", 2000), ("defect_tolerance", 1000)):
            value = body.get(key, "")
            if not isinstance(value, str) or "\x00" in value or len(value) > limit:
                raise ValueError(f"{key} 内容无效")
            extra.append(" ".join(value.split()))
        return batch, count, values[0], values[1], values[2], mode, task_type, mother_id, extra[0], extra[1]

    def author_jobs(self, limit: int = 20) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id,batch_name,question_count,business,technology,notes,author_mode,task_type,mother_id,prompt,status,"
                "substr(output,-30000) AS output,length(output) AS output_length,last_message,"
                "error,pid,created_at,started_at,finished_at FROM author_jobs "
                "ORDER BY created_at DESC,id DESC LIMIT ?",
                (max(1, min(limit, 50)),),
            ).fetchall()
        jobs = [dict(row) for row in rows]
        latest_by_batch: dict[str, int] = {}
        for job in jobs:
            latest_by_batch[job["batch_name"]] = max(
                latest_by_batch.get(job["batch_name"], 0), int(job["id"])
            )
        for job in jobs:
            job["can_retry"] = bool(
                job["status"] in {"failed", "interrupted"}
                and latest_by_batch.get(job["batch_name"]) == int(job["id"])
            )
        return jobs

    def retry_author_job(self, body: dict[str, object]) -> dict[str, object]:
        try:
            source_job_id = int(body.get("job_id", 0))
        except (TypeError, ValueError):
            raise ValueError("出题任务编号无效") from None
        if source_job_id <= 0:
            raise ValueError("出题任务编号无效")
        with closing(self._write_connection()) as connection:
            source = connection.execute(
                "SELECT * FROM author_jobs WHERE id=?", (source_job_id,)
            ).fetchone()
            if source is None:
                raise ValueError("原出题任务不存在")
            if source["status"] not in {"failed", "interrupted"}:
                raise ValueError("只有失败或中断的出题任务可以重试")
            latest = connection.execute(
                "SELECT id FROM author_jobs WHERE batch_name=? ORDER BY id DESC LIMIT 1",
                (source["batch_name"],),
            ).fetchone()
            if latest is None or int(latest["id"]) != source_job_id:
                raise ValueError("该批次已有更新的出题任务，请从最新任务重试")
            active = connection.execute(
                "SELECT 1 FROM author_jobs WHERE batch_name=? AND status IN ('queued','running')",
                (source["batch_name"],),
            ).fetchone()
            if active:
                raise ValueError(f"批次 {source['batch_name']} 已有正在执行的出题任务")
            created = datetime.now().astimezone().isoformat(timespec="seconds")
            prompt = (
                str(source["prompt"])
                + "\n这是上一次失败出题任务的重试。请先检查该批次是否已经部分创建；"
                "若已存在，请在现有批次基础上补齐缺失内容并重新执行需要的质检，"
                "不要删除已有可用产物，也不要重复创建同名批次。"
            )
            cursor = connection.execute(
                "INSERT INTO author_jobs(batch_name,question_count,business,technology,notes,author_mode,task_type,mother_id,"
                "prompt,status,created_at,last_message) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
                (
                    source["batch_name"], source["question_count"], source["business"],
                    source["technology"], source["notes"], source["author_mode"], source["task_type"], source["mother_id"], prompt, created,
                    f"等待重试出题任务 #{source_job_id}",
                ),
            )
            job_id = int(cursor.lastrowid)
            connection.commit()
        codex = shutil.which("codex")
        if not codex:
            with closing(self._write_connection()) as connection:
                connection.execute(
                    "UPDATE author_jobs SET status='failed',error=?,last_message=?,finished_at=? WHERE id=?",
                    ("未找到 Codex CLI", "未找到 Codex CLI", datetime.now().astimezone().isoformat(timespec="seconds"), job_id),
                )
                connection.commit()
            raise RuntimeError("未找到 Codex CLI，请先安装并确保 codex 在 PATH 中")
        self.author_executor.submit(self._run_author_job, job_id, codex)
        return {
            "ok": True,
            "job_id": job_id,
            "retry_of_job_id": source_job_id,
            "message": f"重试出题任务 #{job_id} 已启动",
        }

    def pipeline_jobs(self, limit: int = 20) -> list[dict]:
        with closing(self.connect()) as connection:
            jobs = connection.execute(
                "SELECT p.id,p.batch_name,p.question_count,p.qc_concurrency,p.model_concurrency,"
                "p.codex_concurrency,p.docker_image,p.docker_command,p.status,"
                "p.model_mode,"
                "substr(p.output,-30000) AS output,length(p.output) AS output_length,"
                "p.last_message,p.error,p.pid,p.retry_of_job_id,p.created_at,p.started_at,p.finished_at "
                "FROM pipeline_jobs p WHERE p.id=(SELECT MAX(latest.id) FROM pipeline_jobs latest "
                "WHERE latest.batch_name=p.batch_name) ORDER BY p.created_at DESC,p.id DESC LIMIT ?",
                (max(1, min(limit, 50)),),
            ).fetchall()
            job_ids = [int(job["id"]) for job in jobs]
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                items = connection.execute(
                    "SELECT id,pipeline_job_id,question_id,question_no,status,error,"
                    "heartbeat_at,activity_at,health_status,health_detail,started_at,finished_at "
                    f"FROM pipeline_items WHERE pipeline_job_id IN ({placeholders}) "
                    "ORDER BY pipeline_job_id DESC,question_no",
                    job_ids,
                ).fetchall()
            else:
                items = []
        by_job: dict[int, list[dict]] = {}
        for item in items:
            by_job.setdefault(int(item["pipeline_job_id"]), []).append(dict(item))
        return [
            {
                **dict(job),
                "items": by_job.get(int(job["id"]), []),
                "can_retry": (
                    job["status"] in {"failed", "interrupted"}
                ),
            }
            for job in jobs
        ]

    def create_pipeline_job(self, body: dict[str, object]) -> dict[str, object]:
        batch = str(body.get("batch", "")).strip()
        if not BATCH_RE.fullmatch(batch):
            raise ValueError("批次名无效")
        requested_numbers = body.get("numbers")
        selected_numbers = None if requested_numbers is None else self.validate_selection(batch, requested_numbers)[1]
        with closing(self._write_connection()) as connection:
            batch_row = connection.execute("SELECT * FROM batches WHERE name=?", (batch,)).fetchone()
            query = (
                "SELECT q.id, q.question_no FROM questions q JOIN batches b ON b.id=q.batch_id "
                "WHERE b.name=?"
            )
            parameters: list[object] = [batch]
            if selected_numbers:
                query += " AND q.question_no IN (" + ",".join("?" for _ in selected_numbers) + ")"
                parameters.extend(selected_numbers)
            query += " ORDER BY q.question_no"
            rows = connection.execute(query, parameters).fetchall()
            if batch_row is None or not rows:
                raise ValueError("批次不存在或没有题目")
            if selected_numbers and {int(row["question_no"]) for row in rows} != set(selected_numbers):
                raise ValueError("所选题目不存在")
            active = connection.execute(
                "SELECT 1 FROM pipeline_jobs p JOIN pipeline_items pi ON pi.pipeline_job_id=p.id "
                "WHERE p.batch_name=? AND p.status IN ('queued','running') "
                "AND pi.question_no IN (" + ",".join("?" for _ in rows) + ") LIMIT 1",
                [batch, *(int(row["question_no"]) for row in rows)],
            ).fetchone()
            if active:
                raise ValueError(f"批次 {batch} 的所选题目已有正在执行的流水线任务")
            values = self.read_env()
            required = {
                "CC_SWITCH_BASE_URL": "中转 URL",
                "CC_SWITCH_MODEL": "模型名称",
                "CC_SWITCH_API_KEY": "API Key",
                "CC_USR_SUBMITTER": "提交人",
            }
            missing = [label for key, label in required.items() if not values.get(key, "").strip()]
            if missing:
                raise ValueError("请先在运行配置中填写：" + "、".join(missing))
            existing_runs = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE question_id IN ("
                + ",".join("?" for _ in rows) + ")",
                [int(row["id"]) for row in rows],
            ).fetchone()[0]
            if existing_runs:
                raise ValueError("所选题目已有模型运行记录；全流程仅用于尚未跑题的题目")
            image = values.get("CC_CLAUDE_DOCKER_IMAGE", "").strip() or "claude-cli:latest"
            command = values.get("CC_CLAUDE_DOCKER_COMMAND", "claude").strip() or "claude"
            model_mode = values.get("CC_PIPELINE_MODEL_MODE", "local").strip().lower() or "local"
            if model_mode not in {"local", "docker"}:
                raise ValueError("模型运行方式必须是 local 或 docker")
            def concurrency(key: str, default: int) -> int:
                try:
                    return max(1, min(int(values.get(key, str(default))), 8))
                except ValueError:
                    return default
            qc_concurrency = concurrency("CC_PIPELINE_QC_CONCURRENCY", 2)
            model_concurrency = concurrency("CC_PIPELINE_MODEL_CONCURRENCY", 2)
            codex_concurrency = concurrency("CC_PIPELINE_CODEX_CONCURRENCY", 2)
            created = datetime.now().astimezone().isoformat(timespec="seconds")
            cursor = connection.execute(
                "INSERT INTO pipeline_jobs(batch_name,question_count,qc_concurrency,model_concurrency,codex_concurrency,docker_image,docker_command,model_mode,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (batch, len(rows), qc_concurrency, model_concurrency, codex_concurrency, image, command, model_mode, created),
            )
            job_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO pipeline_items(pipeline_job_id,question_id,question_no) VALUES(?,?,?)",
                [(job_id, row["id"], row["question_no"]) for row in rows],
            )
            connection.commit()
        script = self.project_root / "tools" / "auto_pipeline.py"
        self.pipeline_executor.submit(
            self._run_pipeline_process, job_id, batch, image, command,
            qc_concurrency, model_concurrency, codex_concurrency, model_mode, script,
        )
        return {"ok": True, "job_id": job_id, "message": f"一键流水线 #{job_id} 已启动"}

    def retry_pipeline_job(self, body: dict[str, object]) -> dict[str, object]:
        try:
            source_job_id = int(body.get("job_id", 0))
        except (TypeError, ValueError):
            raise ValueError("重试任务编号无效") from None
        if source_job_id <= 0:
            raise ValueError("重试任务编号无效")
        with closing(self._write_connection()) as connection:
            source = connection.execute(
                "SELECT * FROM pipeline_jobs WHERE id=?", (source_job_id,)
            ).fetchone()
            if source is None:
                raise ValueError("原流水线任务不存在")
            if source["status"] not in {"failed", "interrupted"}:
                raise ValueError("只有失败或中断的流水线任务可以重试")
            latest = connection.execute(
                "SELECT id FROM pipeline_jobs WHERE batch_name=? ORDER BY id DESC LIMIT 1",
                (source["batch_name"],),
            ).fetchone()
            if latest is None or int(latest["id"]) != source_job_id:
                raise ValueError("该批次已有更新的流水线任务，请从最新任务重试")
            active = connection.execute(
                "SELECT 1 FROM pipeline_jobs WHERE batch_name=? AND status IN ('queued','running')",
                (source["batch_name"],),
            ).fetchone()
            if active:
                raise ValueError(f"批次 {source['batch_name']} 已有正在执行的一键任务")
            rows = connection.execute(
                "SELECT question_id, question_no FROM pipeline_items "
                "WHERE pipeline_job_id=? ORDER BY question_no",
                (source_job_id,),
            ).fetchall()
            if not rows:
                raise ValueError("原流水线任务没有可重试的题目")
            created = datetime.now().astimezone().isoformat(timespec="seconds")
            cursor = connection.execute(
                "INSERT INTO pipeline_jobs(batch_name,question_count,qc_concurrency,model_concurrency,"
                "codex_concurrency,docker_image,docker_command,model_mode,retry_of_job_id,created_at,output,last_message) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source["batch_name"], len(rows), source["qc_concurrency"],
                    source["model_concurrency"], source["codex_concurrency"],
                    source["docker_image"], source["docker_command"], source["model_mode"], source_job_id,
                    created, f"从失败任务 #{source_job_id} 继续执行\n",
                    f"等待从任务 #{source_job_id} 的失败位置继续",
                ),
            )
            job_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO pipeline_items(pipeline_job_id,question_id,question_no) VALUES(?,?,?)",
                [(job_id, row["question_id"], row["question_no"]) for row in rows],
            )
            connection.commit()
        script = self.project_root / "tools" / "auto_pipeline.py"
        self.pipeline_executor.submit(
            self._run_pipeline_process, job_id, source["batch_name"], source["docker_image"],
            source["docker_command"], int(source["qc_concurrency"]),
            int(source["model_concurrency"]), int(source["codex_concurrency"]), source["model_mode"], script,
        )
        return {
            "ok": True,
            "job_id": job_id,
            "retry_of_job_id": source_job_id,
            "message": f"重试任务 #{job_id} 已从失败位置启动",
        }

    def _run_pipeline_process(self, job_id: int, batch: str, image: str, command: str, qc_concurrency: int, model_concurrency: int, codex_concurrency: int, model_mode: str, script: Path) -> None:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            pipeline_env = os.environ.copy()
            pipeline_env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            process = subprocess.Popen(
                [
                    sys.executable, str(script), "--db", str(self.database), "--batch", batch,
                    "--job-id", str(job_id), "--image", image, "--claude-command", command,
                    "--model-mode", model_mode,
                    "--qc-concurrency", str(qc_concurrency),
                    "--model-concurrency", str(model_concurrency),
                    "--codex-concurrency", str(codex_concurrency),
                ],
                cwd=self.project_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=flags,
                start_new_session=os.name != "nt", env=pipeline_env,
            )
            with self._pipeline_process_lock:
                self._pipeline_processes[job_id] = process
            assert process.stdout is not None
            with closing(self._write_connection()) as connection:
                connection.execute(
                    "UPDATE pipeline_jobs SET pid=?, started_at=? "
                    "WHERE id=? AND status IN ('queued','running')",
                    (process.pid, datetime.now().astimezone().isoformat(timespec="seconds"), job_id),
                )
                connection.commit()
            for line in process.stdout:
                message = line.rstrip()
                if message:
                    safe_console_print(f"[pipeline {job_id}] {message}")
            process.wait()
        except Exception as exc:
            finished = datetime.now().astimezone().isoformat(timespec="seconds")
            with closing(self._write_connection()) as connection:
                connection.execute(
                    "UPDATE pipeline_jobs SET status='failed', error=?, last_message=?, "
                    "pid=NULL, finished_at=? WHERE id=? AND status IN ('queued','running')",
                    (str(exc), str(exc)[:1000], finished, job_id),
                )
                connection.commit()
        finally:
            with self._pipeline_process_lock:
                self._pipeline_processes.pop(job_id, None)
            with closing(self._write_connection()) as connection:
                connection.execute("UPDATE pipeline_jobs SET pid=NULL WHERE id=?", (job_id,))
                connection.commit()

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
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
                try:
                    process.kill()
                except OSError:
                    pass
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except OSError:
                    pass

    @staticmethod
    def _cleanup_pipeline_containers(job_ids: list[int]) -> None:
        docker = shutil.which("docker")
        if not docker:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        for job_id in job_ids:
            try:
                listed = subprocess.run(
                    [docker, "ps", "-aq", "--filter", f"label=ccusr.pipeline_job={job_id}"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=flags, timeout=15, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            container_ids = [value for value in listed.stdout.splitlines() if value.strip()]
            if listed.returncode == 0 and container_ids:
                try:
                    subprocess.run(
                        [docker, "rm", "-f", *container_ids], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=flags, timeout=30, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue

    def shutdown(self) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(self._write_connection()) as connection:
            connection.execute(
                "UPDATE pipeline_jobs SET status='interrupted', error=?, pid=NULL, finished_at=? "
                "WHERE status IN ('queued','running')",
                ("控制台服务已停止，任务执行中断", timestamp),
            )
            connection.execute(
                "UPDATE pipeline_items SET status='interrupted', finished_at=? "
                "WHERE pipeline_job_id IN (SELECT id FROM pipeline_jobs WHERE status='interrupted') "
                "AND status IN ('queued','qc_running','model_running','producing','finalizing')",
                (timestamp,),
            )
            connection.commit()
        with self._pipeline_process_lock:
            processes = list(self._pipeline_processes.items())
        for _job_id, process in processes:
            self._terminate_process_tree(process)
        self._cleanup_pipeline_containers([job_id for job_id, _process in processes])
        self.author_executor.shutdown(wait=False, cancel_futures=False)
        self.pipeline_executor.shutdown(wait=False, cancel_futures=True)

    def create_author_job(self, body: dict[str, object]) -> dict[str, object]:
        normalized_body = dict(body)
        if str(normalized_body.get("mode", "0-1")).strip() == "derived":
            mothers = self.mother_library()
            if not mothers:
                raise ValueError("母库中没有符合项目规范的可用项目")
            requested_mother = normalized_body.get("mother_id")
            mother = next((item for item in mothers if str(item["id"]) == str(requested_mother)), mothers[0])
            normalized_body["mother_id"] = int(mother["id"])
            requested_type = str(normalized_body.get("task_type", "")).strip()
            allowed_types = {"Bug 修复", "Feature 迭代", "代码理解", "代码重构", "工程化", "代码测试"}
            if requested_type not in allowed_types:
                normalized_body["task_type"] = "Bug 修复" if mother["bugfix_ready"] else "Feature 迭代"
        batch, count, business, technology, notes, mode, task_type, mother_id, derived_notes, defect_tolerance = self._validate_author_fields(normalized_body)
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("未找到 Codex CLI，请先安装并确保 codex 在 PATH 中")
        mother = None
        if mother_id is not None:
            with closing(self.connect()) as read_connection:
                row = read_connection.execute("SELECT * FROM mother_library WHERE id=?", (mother_id,)).fetchone()
                if row is None:
                    raise ValueError("母库项目不存在")
                if task_type == "Bug 修复" and not row["bugfix_ready"]:
                    raise ValueError("该母项目暂不适合生成 Bug 修复题")
                if task_type == "Feature 迭代" and not row["iteration_ready"]:
                    raise ValueError("该母项目暂不适合生成 Feature 迭代题")
                mother = dict(row)
        prompt = self.author_prompt(batch, count, business, technology, notes, mode, task_type, mother, derived_notes, defect_tolerance)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(self._write_connection()) as connection:
            existing = connection.execute("SELECT 1 FROM batches WHERE name=?", (batch,)).fetchone()
            active = connection.execute(
                "SELECT 1 FROM author_jobs WHERE batch_name=? AND status IN ('queued','running')",
                (batch,),
            ).fetchone()
            if existing or (self.project_root / batch).exists():
                raise ValueError(f"批次 {batch} 已存在")
            if active:
                raise ValueError(f"批次 {batch} 已有正在执行的出题任务")
            cursor = connection.execute(
                "INSERT INTO author_jobs(batch_name, question_count, business, technology, notes, author_mode, task_type, mother_id, "
                "prompt, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?)",
                (batch, count, business, technology, notes, mode, task_type, mother_id, prompt, timestamp),
            )
            job_id = int(cursor.lastrowid)
            connection.commit()
        self.author_executor.submit(self._run_author_job, job_id, codex)
        return {"ok": True, "job_id": job_id, "message": f"Codex 出题任务 #{job_id} 已加入队列"}

    @staticmethod
    def _event_message(line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return json.dumps(event, ensure_ascii=False)
        event_type = str(event.get("type", "event"))
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type", ""))
        text = item.get("text") or item.get("command") or event.get("message") or event.get("error")
        if isinstance(text, dict):
            text = text.get("message") or json.dumps(text, ensure_ascii=False)
        if isinstance(text, list):
            text = " ".join(str(value) for value in text)
        if text:
            label = item_type or event_type
            return f"[{label}] {text}"
        if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed"}:
            return f"[{event_type}]"
        return ""

    @staticmethod
    def _redact_log(value: str) -> str:
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
        return re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", value)

    def _update_author_job(self, job_id: int, **values: object) -> None:
        if not values:
            return
        unknown = set(values) - {
            "status", "output", "last_message", "error", "pid", "started_at", "finished_at"
        }
        if unknown:
            raise ValueError(f"unsupported author job fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key}=?" for key in values)
        with closing(self._write_connection()) as connection:
            connection.execute(
                f"UPDATE author_jobs SET {assignments} WHERE id=?",
                [*values.values(), job_id],
            )
            connection.commit()

    def _append_author_output(self, job_id: int, message: str) -> None:
        message = self._redact_log(message.strip())
        if not message:
            return
        with closing(self._write_connection()) as connection:
            connection.execute(
                "UPDATE author_jobs SET output=substr(output || ?, -?), last_message=? WHERE id=?",
                (message + "\n", AUTHOR_JOB_OUTPUT_LIMIT, message[:1000], job_id),
            )
            connection.commit()

    def _author_result_error(self, batch: str, expected_count: int) -> str:
        with closing(self._write_connection()) as connection:
            batch_row = connection.execute(
                "SELECT id,folder_path,markdown_path,question_count FROM batches WHERE name=?",
                (batch,),
            ).fetchone()
            if batch_row is None:
                return f"Codex CLI 已结束，但未创建批次 {batch}"
            questions = connection.execute(
                "SELECT question_no,folder_path,repo_url,initial_snapshot,local_initial_sha,mechanical_qc "
                "FROM questions WHERE batch_id=? ORDER BY question_no",
                (batch_row["id"],),
            ).fetchall()

        issues: list[str] = []
        if int(batch_row["question_count"]) != expected_count or len(questions) != expected_count:
            issues.append(f"题目数量应为 {expected_count}，实际为 {len(questions)}")
        if [int(row["question_no"]) for row in questions] != list(range(1, len(questions) + 1)):
            issues.append("题号不连续")
        if not Path(batch_row["folder_path"]).is_dir():
            issues.append("批次目录不存在")
        if not Path(batch_row["markdown_path"]).is_file():
            issues.append("批次 Markdown 不存在")
        for row in questions:
            number = int(row["question_no"])
            repo_url = str(row["repo_url"]).rstrip("/")
            snapshot = str(row["initial_snapshot"])
            sha = str(row["local_initial_sha"])
            if not Path(row["folder_path"]).is_dir():
                issues.append(f"第 {number} 题目录不存在")
            if not repo_url.startswith("https://github.com/"):
                issues.append(f"第 {number} 题仓库地址无效")
            if not SNAPSHOT_RE.fullmatch(snapshot):
                issues.append(f"第 {number} 题快照地址无效")
            elif not repo_url or not snapshot.startswith(repo_url + "/commit/"):
                issues.append(f"第 {number} 题仓库与快照不一致")
            if not SHA_RE.fullmatch(sha) or snapshot.rsplit("/", 1)[-1].lower() != sha.lower():
                issues.append(f"第 {number} 题快照 SHA 不一致")
            if row["mechanical_qc"] != "pass":
                issues.append(f"第 {number} 题机械质检未通过")
        if not issues:
            return ""
        detail = "；".join(issues[:8])
        if len(issues) > 8:
            detail += f"；另有 {len(issues) - 8} 项问题"
        return f"Codex CLI 已结束，但出题产物不完整：{detail}"

    def _run_author_job(self, job_id: int, codex: str) -> None:
        with closing(self._write_connection()) as connection:
            row = connection.execute(
                "SELECT batch_name,question_count,prompt FROM author_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        command = [
            codex, "exec", "--json", "--cd", str(self.project_root),
            "--sandbox", "danger-full-access", "--skip-git-repo-check", "-",
        ]
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            process = subprocess.Popen(
                command, cwd=self.project_root, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, creationflags=flags,
            )
            self._update_author_job(
                job_id, status="running", pid=process.pid, started_at=timestamp,
                last_message="Codex CLI 已启动",
            )
            assert process.stdin is not None
            process.stdin.write(row["prompt"])
            process.stdin.close()
            assert process.stdout is not None
            for line in process.stdout:
                message = self._event_message(line.strip())
                if message:
                    self._append_author_output(job_id, message)
            returncode = process.wait()
            finished = datetime.now().astimezone().isoformat(timespec="seconds")
            if returncode == 0:
                error = self._author_result_error(row["batch_name"], int(row["question_count"]))
                if error:
                    self._append_author_output(job_id, error)
                    self._update_author_job(
                        job_id, status="failed", pid=None, finished_at=finished, error=error,
                        last_message=error,
                    )
                else:
                    self._update_author_job(
                        job_id, status="completed", pid=None, finished_at=finished,
                        last_message="Codex CLI 执行完成，出题产物校验通过",
                    )
            else:
                error = f"Codex CLI 退出码：{returncode}"
                self._append_author_output(job_id, error)
                self._update_author_job(
                    job_id, status="failed", pid=None, finished_at=finished, error=error,
                    last_message=error,
                )
        except Exception as exc:
            error = self._redact_log(str(exc))
            self._append_author_output(job_id, error)
            self._update_author_job(
                job_id, status="failed", pid=None,
                finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                error=error, last_message=error,
            )

    @staticmethod
    def _env_value(raw: str) -> str:
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            return value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        if len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1]
        return value.split(" #", 1)[0].rstrip()

    def read_env(self) -> dict[str, str]:
        values = {key: "" for key in (*ENV_KEYS, *PIPELINE_ENV_KEYS)}
        if not self.env_file.exists():
            return values
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            key = key.removeprefix("export ").strip()
            if key in values:
                values[key] = self._env_value(raw)
        return values

    def env_config(self) -> dict[str, object]:
        values = self.read_env()
        key = values["CC_SWITCH_API_KEY"]
        def env_int(name: str, default: int) -> int:
            try:
                return max(1, min(int(values.get(name, str(default))), 8))
            except ValueError:
                return default
        return {
            "base_url": values["CC_SWITCH_BASE_URL"],
            "model": values["CC_SWITCH_MODEL"],
            "submitter": values["CC_USR_SUBMITTER"],
            "api_key_configured": bool(key),
            "api_key_hint": f"已配置（末尾 {key[-4:]}）" if len(key) >= 4 else ("已配置" if key else "未配置"),
            **runtime_info(),
            "docker_image": values.get("CC_CLAUDE_DOCKER_IMAGE", "").strip() or "claude-cli:latest",
            "docker_command": values.get("CC_CLAUDE_DOCKER_COMMAND", "").strip() or "claude",
            "model_mode": values.get("CC_PIPELINE_MODEL_MODE", "local").strip().lower() or "local",
            "qc_concurrency": env_int("CC_PIPELINE_QC_CONCURRENCY", 2),
            "model_concurrency": env_int("CC_PIPELINE_MODEL_CONCURRENCY", 2),
            "codex_concurrency": env_int("CC_PIPELINE_CODEX_CONCURRENCY", 2),
        }

    def environment_status(self, repair: bool = False) -> dict[str, object]:
        """Check local tools and repair the bundled Claude image when possible."""
        checks: list[dict[str, object]] = []
        repair_messages: list[str] = []

        def add(name: str, ok: bool, detail: str, repairable: bool = False) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "repairable": repairable})

        codex = shutil.which("codex")
        codex_ok = False
        codex_detail = str(codex or "未找到 codex，请安装后加入 PATH")
        if codex:
            try:
                codex_probe = subprocess.run(
                    [codex, "--version"], text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False, timeout=30,
                )
                codex_output = codex_probe.stdout.strip()
                codex_ok = codex_probe.returncode == 0 and bool(re.search(r"\d+(?:\.\d+)+", codex_output))
                codex_detail = codex_output[-300:] if codex_output else "Codex CLI 未返回版本"
            except (OSError, subprocess.TimeoutExpired) as exc:
                codex_detail = f"Codex CLI 无法运行：{exc}"
        add("Codex CLI", codex_ok, codex_detail)
        mode = self.read_env().get("CC_PIPELINE_MODEL_MODE", "local").strip().lower() or "local"
        if mode not in {"local", "docker"}:
            add("模型运行方式", False, "必须是 local 或 docker")
            mode = "local"
        if mode == "local":
            command = self.read_env().get("CC_CLAUDE_DOCKER_COMMAND", "claude").strip() or "claude"
            claude = shutil.which(command) or (command if Path(command).is_file() else None)
            claude_ok = False
            claude_detail = str(claude or f"未找到本地 Claude CLI：{command}")
            if claude:
                try:
                    probe = subprocess.run(
                        [claude, "--version"], text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, check=False, timeout=30,
                    )
                    claude_ok = probe.returncode == 0 and bool(probe.stdout.strip())
                    claude_detail = probe.stdout.strip()[-300:] or claude_detail
                except (OSError, subprocess.TimeoutExpired) as exc:
                    claude_detail = str(exc)
            add("本地 Claude CLI", claude_ok, claude_detail)
            return {
                "ok": all(bool(check["ok"]) for check in checks),
                "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "checks": checks, "repair_attempted": repair,
                "repair_messages": repair_messages,
            }
        docker = shutil.which("docker")
        add("Docker CLI", bool(docker), str(docker or "未找到 docker，请安装 Docker Desktop"))
        image = self.read_env().get("CC_CLAUDE_DOCKER_IMAGE", "").strip() or "claude-cli:latest"
        command = self.read_env().get("CC_CLAUDE_DOCKER_COMMAND", "").strip() or "claude"
        docker_ready = False
        if docker:
            info = docker_info(docker)
            docker_ready = info.returncode == 0
            if not docker_ready and repair:
                docker_ready, repair_detail = repair_docker_engine(docker)
                repair_messages.append(repair_detail)
            add("Docker 引擎", docker_ready, "运行中" if docker_ready else info.stdout.strip()[-300:] or "无法连接 Docker 引擎", True)
        if docker and docker_ready:
            inspect = subprocess.run(
                [docker, "image", "inspect", image], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=15,
            )
            image_ok = inspect.returncode == 0
            image_user = ""
            nonroot_label = False
            if image_ok:
                try:
                    config = json.loads(inspect.stdout)[0].get("Config", {})
                    image_user = str(config.get("User") or "").strip().lower()
                    nonroot_label = (config.get("Labels") or {}).get("ccusr.claude.nonroot") == "true"
                except (ValueError, TypeError, IndexError):
                    image_ok = False
            needs_repair = image == "claude-cli:latest" and (not image_ok or image_user in {"", "root", "0", "0:0"} or not nonroot_label)
            if needs_repair and repair:
                dockerfile = self.project_root / "docker" / "claude-cli" / "Dockerfile"
                if dockerfile.is_file():
                    built = subprocess.run(
                        [docker, "build", "-t", image, str(dockerfile.parent)],
                        cwd=self.project_root, text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, check=False, timeout=1800,
                    )
                    if built.returncode == 0:
                        repair_messages.append("已重建非 root Claude CLI 镜像")
                        inspect = subprocess.run(
                            [docker, "image", "inspect", image], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=15,
                        )
                        image_ok = inspect.returncode == 0
                        if image_ok:
                            try:
                                config = json.loads(inspect.stdout)[0].get("Config", {})
                                image_user = str(config.get("User") or "").strip().lower()
                                nonroot_label = (config.get("Labels") or {}).get("ccusr.claude.nonroot") == "true"
                            except (ValueError, TypeError, IndexError):
                                image_ok = False
                    else:
                        repair_messages.append("Claude CLI 镜像重建失败：" + built.stdout.strip()[-300:])
            image_ready = image_ok and (
                image != "claude-cli:latest"
                or (image_user not in {"", "root", "0", "0:0"} and nonroot_label)
            )
            detail = f"{image}，用户 {image_user or 'root'}"
            if not image_ok:
                detail = f"{image} 不存在"
            add("Claude Docker 镜像", image_ready, detail, image == "claude-cli:latest")
            if image_ready:
                user_probe = subprocess.run(
                    [docker, "run", "--rm", "--user", "1000:1000", image, "id", "-u"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
                )
                probe = subprocess.run(
                    [docker, "run", "--rm", "--user", "1000:1000", "-e", "HOME=/home/node", image, command, "--version"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=60,
                )
                probe_ok = user_probe.returncode == 0 and user_probe.stdout.strip() != "0" and probe.returncode == 0
                probe_detail = probe.stdout.strip()[-300:] or user_probe.stdout.strip()[-300:] or "探针失败"
                add("Claude CLI 运行探针", probe_ok, probe_detail)
        ok = all(bool(check["ok"]) for check in checks)
        return {
            "ok": ok,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "checks": checks,
            "repair_attempted": repair,
            "repair_messages": repair_messages,
        }

    @staticmethod
    def _validate_env_input(values: dict[str, object]) -> dict[str, str]:
        result = {}
        for field in ("base_url", "model", "api_key", "submitter", "docker_image", "docker_command", "model_mode"):
            value = values.get(field, "")
            if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError(f"{field} 配置无效")
            result[field] = value.strip()
        if not result["base_url"] or not result["model"] or not result["submitter"]:
            raise ValueError("中转 URL、模型名和提交人不能为空")
        if result["model_mode"] not in {"local", "docker"}:
            raise ValueError("model_mode must be local or docker")
        parsed = urlparse(result["base_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("中转 URL 必须是完整的 http(s) 地址")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("非本机中转地址必须使用 HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("中转 URL 不能包含账号、密码、查询参数或片段")
        if len(result["model"]) > 200 or len(result["submitter"]) > 200:
            raise ValueError("模型名或提交人过长")
        return result

    def update_env(self, body: dict[str, object]) -> dict[str, object]:
        current = self.read_env()
        incoming = {
            "base_url": body.get("base_url", current["CC_SWITCH_BASE_URL"]),
            "model": body.get("model", current["CC_SWITCH_MODEL"]),
            "api_key": body.get("api_key", ""),
            "submitter": body.get("submitter", current["CC_USR_SUBMITTER"]),
            "docker_image": body.get("docker_image", current.get("CC_CLAUDE_DOCKER_IMAGE", "claude-cli:latest")),
            "docker_command": body.get("docker_command", current.get("CC_CLAUDE_DOCKER_COMMAND", "claude")),
            "model_mode": body.get("model_mode", current.get("CC_PIPELINE_MODEL_MODE", "local")) or "local",
        }
        values = self._validate_env_input(incoming)
        concurrency_values = {}
        for field, env_key in (
            ("qc_concurrency", "CC_PIPELINE_QC_CONCURRENCY"),
            ("model_concurrency", "CC_PIPELINE_MODEL_CONCURRENCY"),
            ("codex_concurrency", "CC_PIPELINE_CODEX_CONCURRENCY"),
        ):
            raw = body.get(field, current.get(env_key, ""))
            if raw in (None, ""):
                raw = "2"
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                raise ValueError(f"{field} 配置无效")
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{field} 配置无效") from exc
            if not 1 <= value <= 8:
                raise ValueError(f"{field} 必须是 1-8")
            concurrency_values[env_key] = str(value)
        if not values["api_key"]:
            values["api_key"] = current["CC_SWITCH_API_KEY"]
        if not values["api_key"]:
            raise ValueError("API Key 尚未配置")
        output_values = {
            "CC_SWITCH_BASE_URL": values["base_url"],
            "CC_SWITCH_MODEL": values["model"],
            "CC_SWITCH_API_KEY": values["api_key"],
            "CC_USR_SUBMITTER": values["submitter"],
            "CC_CLAUDE_DOCKER_IMAGE": values["docker_image"],
            "CC_CLAUDE_DOCKER_COMMAND": values["docker_command"],
            "CC_PIPELINE_MODEL_MODE": values["model_mode"],
            **concurrency_values,
        }
        lines = self.env_file.read_text(encoding="utf-8").splitlines() if self.env_file.exists() else []
        replaced: set[str] = set()
        output: list[str] = []
        for line in lines:
            stripped = line.strip()
            key = stripped.split("=", 1)[0].removeprefix("export ").strip() if "=" in stripped else ""
            if key in output_values and ENV_KEY_RE.fullmatch(key):
                output.append(f'{key}={json.dumps(output_values[key], ensure_ascii=False)}')
                replaced.add(key)
            else:
                output.append(line)
        if output and output[-1].strip():
            output.append("")
        for key in (*ENV_KEYS, *PIPELINE_ENV_KEYS):
            if key not in replaced:
                output.append(f'{key}={json.dumps(output_values[key], ensure_ascii=False)}')
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".env.", dir=self.env_file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(output).rstrip() + "\n")
            secure_file(Path(temp_name))
            os.replace(temp_name, self.env_file)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return {"ok": True, "message": "运行配置已保存", "config": self.env_config()}

    def connect(self) -> sqlite3.Connection:
        if not self.database.is_file():
            raise FileNotFoundError(f"数据库不存在：{self.database}")
        connection = sqlite3.connect(f"file:{quote(str(self.database))}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def batches(self, connection: sqlite3.Connection) -> list[dict]:
        rows = connection.execute(
            "SELECT b.*, "
            "(SELECT COUNT(*) FROM questions q WHERE q.batch_id=b.id) AS actual_questions, "
            "(SELECT COUNT(*) FROM records r JOIN questions q ON q.id=r.question_id "
            " WHERE q.batch_id=b.id) AS record_count, "
            "(SELECT COUNT(*) FROM records r JOIN questions q ON q.id=r.question_id "
            " WHERE q.batch_id=b.id AND r.delivery_qc_passed=1) AS qc_record_count "
            "FROM batches b ORDER BY b.created_at DESC, b.name DESC"
        ).fetchall()
        return [
            {
                "name": row["name"],
                "brief": row["brief"],
                "question_count": row["actual_questions"],
                "record_count": row["record_count"],
                "qc_record_count": row["qc_record_count"],
                "author_mode": row["author_mode"],
                "mother_id": row["mother_id"],
                "folder_path": row["folder_path"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def mother_library(self, limit: int = 200) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT m.*, COUNT(u.id) AS derived_count "
                "FROM mother_library m LEFT JOIN mother_usages u ON u.mother_id=m.id "
                "WHERE m.repo_url <> '' AND m.initial_snapshot <> '' AND m.local_initial_sha <> '' "
                "GROUP BY m.id ORDER BY CASE WHEN m.bugfix_ready=1 THEN 0 ELSE 1 END, m.use_count ASC, m.updated_at DESC, m.id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _export_files(batch_folder: Path) -> tuple[list[dict], list[dict]]:
        if not batch_folder.is_dir():
            return [], []

        def describe(path: Path) -> dict:
            stat = path.stat()
            return {
                "name": path.name,
                "path": str(path),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
                    timespec="seconds"
                ),
            }

        workbooks = sorted(
            (
                path for path in batch_folder.glob("CC_Codex*.xlsx")
                if path.is_file() and not path.name.startswith(".~")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        trajectories = sorted(
            (path for path in batch_folder.glob("轨迹_*.jsonl") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [describe(path) for path in workbooks], [describe(path) for path in trajectories]

    @staticmethod
    def _exported_question_numbers(batch: str, workbooks: list[dict]) -> set[int]:
        exported: set[int] = set()
        pattern = re.compile(
            rf"^CC_Codex 用户满意度标注（{re.escape(batch)}-第([0-9_-]+)题）(?:_\d+)?\.xlsx$"
        )
        for workbook in workbooks:
            match = pattern.fullmatch(str(workbook.get("name", "")))
            if not match:
                continue
            for token in match.group(1).split("_"):
                if token.isdigit():
                    exported.add(int(token))
                    continue
                range_match = re.fullmatch(r"(\d+)-(\d+)", token)
                if range_match:
                    start, end = map(int, range_match.groups())
                    exported.update(range(start, end + 1))
        return exported

    def dashboard(self, requested_batch: str | None = None) -> dict:
        with closing(self.connect()) as connection:
            batches = self.batches(connection)
            if not batches:
                return {
                    "batches": [], "batch": None, "questions": [], "stages": [],
                    "summary": {"total": 0, "delivered": 0, "qc_passed": 0, "waiting": 0},
                }
            names = {batch["name"] for batch in batches}
            batch_name = requested_batch if requested_batch in names else batches[0]["name"]
            batch = next(item for item in batches if item["name"] == batch_name)
            rows = connection.execute(
                "SELECT q.*, "
                "(SELECT COUNT(*) FROM runs x WHERE x.question_id=q.id) AS run_count, "
                "(SELECT x.launched_at FROM runs x WHERE x.question_id=q.id "
                " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS launched_at, "
                "(SELECT x.session_id FROM runs x WHERE x.question_id=q.id "
                " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS latest_session_id, "
                "(SELECT x.harness_version FROM runs x WHERE x.question_id=q.id "
                " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS harness_version, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id) AS record_count, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id "
                " AND r.delivery_qc_passed=1) AS passed_record_count, "
                "(SELECT AVG((r.delivery_score+r.instruction_score+r.planning_score+"
                " r.reasoning_score+r.execution_score)/5.0) FROM records r "
                " WHERE r.question_id=q.id) AS average_score "
                "FROM questions q JOIN batches b ON b.id=q.batch_id "
                "WHERE b.name=? ORDER BY q.question_no",
                (batch_name,),
            ).fetchall()
            workbooks, trajectories = self._export_files(Path(batch["folder_path"]))
            exported_numbers = self._exported_question_numbers(batch_name, workbooks)
            questions = []
            for row in rows:
                qc_current = row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
                question_qc = bool(
                    row["mechanical_qc"] == "pass"
                    and row["qc_decision"] == "pass"
                    and qc_current
                    and row["status"] in {"approved", "running", "completed"}
                )
                run_count = int(row["run_count"] or 0)
                record_count = int(row["record_count"] or 0)
                passed_count = int(row["passed_record_count"] or 0)
                delivery_qc = record_count > 0 and passed_count == record_count
                exported = delivery_qc and int(row["question_no"]) in exported_numbers
                if not question_qc:
                    stage_index, stage_label = 1, "题目待质检"
                elif run_count == 0:
                    stage_index, stage_label = 2, "待模型跑题"
                elif record_count == 0:
                    stage_index, stage_label = 3, "待交付生产"
                elif not delivery_qc:
                    stage_index, stage_label = 4, "待交付质检"
                elif not exported:
                    stage_index, stage_label = 5, "待导出"
                else:
                    stage_index, stage_label = 6, "已交付"
                questions.append({
                    "id": row["id"],
                    "question_no": row["question_no"],
                    "task_id": row["task_id"],
                    "title": row["title"],
                    "author_mode": row["author_mode"],
                    "mother_id": row["mother_id"],
                    "task_type": row["task_type"],
                    "repo_url": row["repo_url"],
                    "folder_name": row["folder_name"],
                    "difficulty": row["difficulty"],
                    "languages": row["languages"],
                    "question_qc": question_qc,
                    "mechanical_qc": row["mechanical_qc"],
                    "qc_decision": row["qc_decision"],
                    "qc_current": qc_current,
                    "run_count": run_count,
                    "launched_at": row["launched_at"] or "",
                    "session_id": row["latest_session_id"] or "",
                    "harness_version": row["harness_version"] or "",
                    "record_count": record_count,
                    "passed_record_count": passed_count,
                    "delivery_qc": delivery_qc,
                    "average_score": round(float(row["average_score"]), 1)
                    if row["average_score"] is not None else None,
                    "exported": exported,
                    "stage_index": stage_index,
                    "stage_label": stage_label,
                    "can_launch": question_qc and row["status"] == "approved" and run_count == 0,
                })

        total = len(questions)
        stages = [
            {"id": 1, "label": "题目质检", "complete": sum(q["question_qc"] for q in questions)},
            {"id": 2, "label": "模型跑题", "complete": sum(q["run_count"] > 0 for q in questions)},
            {"id": 3, "label": "交付生产", "complete": sum(q["record_count"] > 0 for q in questions)},
            {"id": 4, "label": "交付质检", "complete": sum(q["delivery_qc"] for q in questions)},
            {"id": 5, "label": "Excel 交付", "complete": sum(q["exported"] for q in questions)},
        ]
        for stage in stages:
            stage["current"] = sum(q["stage_index"] == stage["id"] for q in questions)
            stage["pending"] = total - stage["complete"]
        return {
            "batches": batches,
            "batch": {
                **batch,
                "workbooks": workbooks,
                "trajectories": trajectories,
                "latest_workbook": workbooks[0] if workbooks else None,
            },
            "questions": questions,
            "stages": stages,
            "summary": {
                "total": total,
                "delivered": sum(q["exported"] for q in questions),
                "qc_passed": sum(q["delivery_qc"] for q in questions),
                "waiting": sum(q["stage_index"] < 6 for q in questions),
            },
        }

    def question_detail(self, question_id: int) -> dict:
        with closing(self.connect()) as connection:
            question = connection.execute(
                "SELECT q.*, b.name AS batch_name FROM questions q "
                "JOIN batches b ON b.id=q.batch_id WHERE q.id=?",
                (question_id,),
            ).fetchone()
            if question is None:
                raise ValueError("题目不存在")
            runs = connection.execute(
                "SELECT batch_run_id, launched_at, session_id, harness, harness_version "
                "FROM runs WHERE question_id=? ORDER BY launched_at DESC, id DESC",
                (question_id,),
            ).fetchall()
            records = connection.execute(
                "SELECT record_id, turn_no, user_prompt, session_id, turn_id, trajectory_file, "
                "delivery_score, delivery_description, instruction_score, instruction_description, "
                "planning_score, planning_description, reasoning_score, reasoning_description, "
                "execution_score, execution_description, other_issues, submitted_at, "
                "delivery_qc_passed, delivery_qc_note, delivery_qc_checked_at "
                "FROM records WHERE question_id=? ORDER BY turn_no",
                (question_id,),
            ).fetchall()
        return {
            "id": question["id"],
            "batch": question["batch_name"],
            "question_no": question["question_no"],
            "task_id": question["task_id"],
            "title": question["title"],
            "prompt": question["prompt"],
            "task_type": question["task_type"],
            "difficulty": question["difficulty"],
            "languages": question["languages"],
            "repo_url": question["repo_url"],
            "author_mode": question["author_mode"],
            "mother_id": question["mother_id"],
            "initial_snapshot": question["initial_snapshot"],
            "folder_path": question["folder_path"],
            "reproducibility": question["reproducibility"],
            "expected_areas": json_value(question["expected_areas"], []),
            "difficulty_evidence": json_value(question["difficulty_evidence"], []),
            "qc_report": question["qc_report"],
            "runs": [dict(row) for row in runs],
            "records": [dict(row) for row in records],
        }

    def resolve_open_target(self, kind: str, target_id: object) -> Path:
        with closing(self.connect()) as connection:
            if kind == "batch":
                batch = str(target_id or "")
                row = connection.execute(
                    "SELECT folder_path FROM batches WHERE name=?", (batch,)
                ).fetchone()
                if row is None:
                    raise ValueError("批次不存在")
                target = Path(row["folder_path"])
            elif kind == "question":
                value = str(target_id or "")
                if not QUESTION_ID_RE.fullmatch(value):
                    raise ValueError("题目编号无效")
                row = connection.execute(
                    "SELECT folder_path FROM questions WHERE id=?", (int(value),)
                ).fetchone()
                if row is None:
                    raise ValueError("题目不存在")
                target = Path(row["folder_path"])
            else:
                raise ValueError("不支持的打开目标")
        target = target.resolve(strict=True)
        if not target.is_dir():
            raise ValueError("目标目录不可用")
        return target

    def validate_selection(self, batch: object, numbers: object) -> tuple[str, list[int]]:
        batch_name = str(batch or "")
        if not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        if not isinstance(numbers, list) or not numbers or len(numbers) > 100:
            raise ValueError("请选择至少一道题")
        normalized: list[int] = []
        for number in numbers:
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError("题号必须为正整数")
            if number not in normalized:
                normalized.append(number)
        with closing(self.connect()) as connection:
            placeholders = ",".join("?" for _ in normalized)
            found = connection.execute(
                f"SELECT q.question_no FROM questions q JOIN batches b ON b.id=q.batch_id "
                f"WHERE b.name=? AND q.question_no IN ({placeholders})",
                [batch_name, *normalized],
            ).fetchall()
        if {row["question_no"] for row in found} != set(normalized):
            raise ValueError("选择中包含不存在的题号")
        return batch_name, sorted(normalized)

    def run_tool(self, command: list[str], timeout: int = 120) -> dict:
        result = subprocess.run(
            command,
            cwd=self.project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = result.stdout.strip()
        if result.returncode:
            raise RuntimeError(output or f"操作失败，退出码 {result.returncode}")
        return {"ok": True, "output": output}

    def launch(self, batch: object, numbers: object) -> dict:
        batch_name, selected = self.validate_selection(batch, numbers)
        if len(selected) > 4:
            raise ValueError("单次最多启动 4 道题")
        script = self.project_root / ".agents/skills/cc-usr-claude-runner/scripts/run_tasks.py"
        return self.run_tool([
            sys.executable,
            str(script),
            "--db",
            str(self.database),
            "--batch",
            batch_name,
            "--select",
            ",".join(map(str, selected)),
            "--launch",
            "--mode",
            "auto",
        ])

    def qc_check(self, batch: object) -> dict:
        batch_name = str(batch or "")
        if not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        script = self.project_root / ".agents/skills/cc-usr-delivery-qc/scripts/validate_records.py"
        return self.run_tool([
            sys.executable, str(script), "--db", str(self.database), "--batch", batch_name
        ])

    def export(self, batch: object, numbers: object | None) -> dict:
        batch_name = str(batch or "")
        if numbers:
            batch_name, selected = self.validate_selection(batch_name, numbers)
        else:
            if not BATCH_RE.fullmatch(batch_name):
                raise ValueError("批次名无效")
            selected = []
        with closing(self.connect()) as connection:
            batch_row = connection.execute(
                "SELECT folder_path FROM batches WHERE name=?", (batch_name,)
            ).fetchone()
        if batch_row is None:
            raise ValueError("batch does not exist")
        trajectory_root = Path(batch_row["folder_path"]).absolute() / ".runs"
        script = self.project_root / ".agents/skills/cc-usr-excel-exporter/scripts/export_xlsx.py"
        command = [
            sys.executable, str(script), "--db", str(self.database), "--batch", batch_name,
            "--claude-root", str(trajectory_root),
        ]
        if selected:
            command.extend(["--select", ",".join(map(str, selected))])
        return self.run_tool(command)


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "CCUSRConsole/1.0"

    @property
    def data(self) -> ConsoleData:
        return self.server.data  # type: ignore[attr-defined]

    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, message: str, status: HTTPStatus) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def send_static(self, relative: str) -> None:
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            self.send_error_json("文件不存在", HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error_json("文件不存在", HTTPStatus.NOT_FOUND)
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 65_536:
            raise ValueError("请求内容大小无效")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求格式无效")
        return value

    def post_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/dashboard":
                batch = parse_qs(parsed.query).get("batch", [None])[0]
                self.send_json(self.data.dashboard(batch))
                return
            if parsed.path == "/api/config":
                self.send_json(self.data.env_config())
                return
            if parsed.path == "/api/environment":
                self.send_json(self.data.environment_status())
                return
            if parsed.path == "/api/author-jobs":
                self.send_json({"jobs": self.data.author_jobs()})
                return
            if parsed.path == "/api/mother-library":
                self.send_json({"mothers": self.data.mother_library()})
                return
            if parsed.path == "/api/pipeline-jobs":
                self.send_json({"jobs": self.data.pipeline_jobs()})
                return
            if parsed.path.startswith("/api/questions/"):
                raw_id = parsed.path.rsplit("/", 1)[-1]
                if not QUESTION_ID_RE.fullmatch(raw_id):
                    raise ValueError("题目编号无效")
                self.send_json(self.data.question_detail(int(raw_id)))
                return
            if parsed.path in {"/", "/index.html"}:
                self.send_static("index.html")
                return
            if parsed.path.startswith("/static/"):
                self.send_static(unquote(parsed.path[len("/static/"):]))
                return
            self.send_error_json("页面不存在", HTTPStatus.NOT_FOUND)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        if not self.post_allowed():
            self.send_error_json("拒绝跨站请求", HTTPStatus.FORBIDDEN)
            return
        try:
            body = self.read_json()
            if self.path == "/api/actions/open":
                target = self.data.resolve_open_target(str(body.get("kind", "")), body.get("id"))
                open_local_path(target, self.data.project_root)
                result = {"ok": True, "message": f"已打开 {target.name}"}
            elif self.path == "/api/actions/launch":
                result = self.data.launch(body.get("batch"), body.get("numbers"))
            elif self.path == "/api/actions/qc-check":
                result = self.data.qc_check(body.get("batch"))
            elif self.path == "/api/actions/export":
                result = self.data.export(body.get("batch"), body.get("numbers"))
            elif self.path == "/api/config":
                result = self.data.update_env(body)
            elif self.path == "/api/actions/environment-repair":
                result = self.data.environment_status(repair=True)
                result["message"] = "环境检测与修复已完成" if result["ok"] else "环境仍不可用，请查看检测结果"
            elif self.path == "/api/actions/codex-author":
                result = self.data.create_author_job(body)
            elif self.path == "/api/actions/codex-author-retry":
                result = self.data.retry_author_job(body)
            elif self.path == "/api/actions/auto-pipeline":
                result = self.data.create_pipeline_job(body)
            elif self.path == "/api/actions/auto-pipeline-retry":
                result = self.data.retry_pipeline_job(body)
            else:
                self.send_error_json("操作不存在", HTTPStatus.NOT_FOUND)
                return
            self.send_json(result)
        except json.JSONDecodeError:
            self.send_error_json("请求不是有效 JSON", HTTPStatus.BAD_REQUEST)
        except subprocess.TimeoutExpired:
            self.send_error_json("操作超时，请检查终端状态", HTTPStatus.REQUEST_TIMEOUT)
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


class ConsoleServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], data: ConsoleData) -> None:
        super().__init__(address, ConsoleHandler)
        self.data = data

    def server_close(self) -> None:
        self.data.shutdown()
        super().server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="CC USR local production console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("为保护本地数据，控制台只允许绑定本机地址。", file=sys.stderr)
        return 2
    data = ConsoleData(args.db)
    try:
        data.dashboard()
        server = ConsoleServer((args.host, args.port), data)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"无法启动控制台：{exc}", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}"
    print(f"CC USR 生产控制台已启动：{url}")
    print("按 Ctrl+C 停止。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
