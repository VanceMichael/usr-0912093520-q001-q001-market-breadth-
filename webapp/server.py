#!/usr/bin/env python3
"""Serve the local CC USR production console without external dependencies."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import mimetypes
import os as _os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from contextlib import closing
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse


class _OSProxy:
    """Keep test-time platform overrides local to this module."""

    def __getattr__(self, name: str) -> object:
        return getattr(_os, name)


os = _OSProxy()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
DEFAULT_DATABASE = PROJECT_ROOT / "production.sqlite3"
QUESTION_ID_RE = re.compile(r"^\d+$")
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SNAPSHOT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/commit/[0-9a-fA-F]{40}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_KEYS = (
    "CC_SWITCH_BASE_URL", "CC_SWITCH_MODEL", "CC_SWITCH_API_KEY", "CC_USR_SUBMITTER",
    "CC_GITHUB_TOKEN", "CC_AUTHOR_DIFFICULTY", "CC_AUTHOR_DIFFICULTY_WEIGHTS", "CC_AUTHOR_BATCH_SIZE",
)
PIPELINE_ENV_KEYS = (
    "CC_CLAUDE_DOCKER_IMAGE", "CC_CLAUDE_DOCKER_COMMAND",
    "CC_PIPELINE_MODEL_MODE",
    "CC_PIPELINE_QC_CONCURRENCY", "CC_PIPELINE_MODEL_CONCURRENCY",
    "CC_PIPELINE_CODEX_CONCURRENCY", "CC_PIPELINE_READY_TARGET",
    "CC_CLAUDE_WORKER_CPUS", "CC_CLAUDE_WORKER_MEMORY",
    "CC_CLAUDE_HEARTBEAT_SECONDS",
    "CC_CLAUDE_START_TIMEOUT", "CC_CLAUDE_STALLED_TIMEOUT",
    "CC_PIPELINE_WORKER_TIMEOUT",
    "CC_GATEWAY_MAX_ATTEMPTS", "CC_GATEWAY_BACKOFF_BASE",
    "CC_GATEWAY_BACKOFF_MAX", "CC_GATEWAY_CIRCUIT_THRESHOLD",
    "CC_GATEWAY_CIRCUIT_WINDOW", "CC_GATEWAY_CIRCUIT_COOLDOWN",
    "CC_SOLO2_ORIGIN", "CC_SOLO2_AUTO_SUBMIT", "CC_SOLO2_CONCURRENCY",
    "CC_SOLO2_MAX_ATTEMPTS",
)
NEWS_URL_MAX = 20
AUTHOR_DIFFICULTIES = ("中等", "困难", "地狱")
AUTHOR_JOB_OUTPUT_LIMIT = 200_000
AUTHOR_JOB_STATUSES = {"queued", "running", "completed", "failed", "interrupted"}

sys.path.insert(0, str(PROJECT_ROOT))
from tools.runtime_environment import docker_info, repair_docker_engine  # noqa: E402
from tools.capacity import concurrency_recommendation as scheduler_capacity  # noqa: E402
from tools.batch_pipeline import SCHEMA_VERSION, connect as initialize_database  # noqa: E402
from tools.authoring_policy import backend_only_requirement  # noqa: E402
from tools.scheduler_state import SchedulerStore  # noqa: E402
from tools.text_encoding import read_portable_text  # noqa: E402
from tools.solo2_client import Solo2Client, Solo2Error  # noqa: E402
from tools.solo2_service import submission_overview, submit_records  # noqa: E402
from tools.task_maintenance import begin_takeover, finish_takeover, reset_question  # noqa: E402
from tools.human_review import (  # noqa: E402
    approve_codex_record,
    approve_record as approve_delivery_record,
    approved_history,
    build_codex_review_dossier,
    import_history,
    reject_record as reject_delivery_record,
    review_queue,
)

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

CREATE TABLE IF NOT EXISTS delivery_downloads (
    batch_id INTEGER PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
    download_count INTEGER NOT NULL DEFAULT 0,
    last_downloaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vps_nodes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    ssh_command TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vps_nodes_enabled ON vps_nodes(enabled, name);
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


def parse_codex_review(raw: str) -> dict:
    """Parse and validate the machine-readable final Codex review."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:].lstrip("\r\n")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Codex 复核结果不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Codex 复核结果必须是 JSON 对象")
    if value.get("decision") not in {"approved", "rejected"}:
        raise ValueError("Codex 复核结论必须是 approved 或 rejected")
    dimensions = value.get("dimensions")
    expected = {"delivery", "instruction", "planning", "reasoning", "execution"}
    if not isinstance(dimensions, dict) or set(dimensions) != expected:
        raise ValueError("Codex 复核结果必须完整包含五个维度")
    for name, item in dimensions.items():
        if not isinstance(item, dict) or not isinstance(item.get("approved"), bool):
            raise ValueError(f"Codex 复核维度 {name} 缺少布尔结论")
        if len(str(item.get("reason") or "").strip()) < 8:
            raise ValueError(f"Codex 复核维度 {name} 缺少具体依据")
    if len(str(value.get("summary") or "").strip()) < 8:
        raise ValueError("Codex 复核缺少总结")
    if not isinstance(value.get("issues", []), list):
        raise ValueError("Codex 复核 issues 必须是数组")
    return value


def codex_review_report(review: dict) -> str:
    labels = {
        "delivery": "交付完整性", "instruction": "指令遵循", "planning": "任务规划",
        "reasoning": "推理能力", "execution": "执行能力",
    }
    lines = [
        f"结论：{'通过' if review['decision'] == 'approved' else '不通过'}",
        f"总结：{str(review['summary']).strip()}",
    ]
    for name, label in labels.items():
        item = review["dimensions"][name]
        lines.append(
            f"{label}：{'通过' if item['approved'] else '不通过'}。{str(item['reason']).strip()}"
        )
    issues = [str(item).strip() for item in review.get("issues", []) if str(item).strip()]
    if issues:
        lines.append("阻断问题：" + "；".join(issues))
    return "\n".join(lines)


def codex_review_schema() -> dict:
    """Return the strict response contract used by the isolated Codex review."""
    dimension = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "approved": {"type": "boolean"},
            "reason": {"type": "string", "minLength": 8},
        },
        "required": ["approved", "reason"],
    }
    names = ("delivery", "instruction", "planning", "reasoning", "execution")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["approved", "rejected"]},
            "summary": {"type": "string", "minLength": 8},
            "dimensions": {
                "type": "object",
                "additionalProperties": False,
                "properties": {name: dimension for name in names},
                "required": list(names),
            },
            "issues": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
        "required": ["decision", "summary", "dimensions", "issues"],
    }


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
    target = str(path).replace("/", "\\")
    acl = subprocess.run(
        ["icacls", target, "/inheritance:r", "/grant:r", f"{account}:F"],
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
        connection = initialize_database(self.database)
        connection.close()
        self.scheduler_store = SchedulerStore(self.database)
        self._initialize_author_jobs()
        self._initialize_pipeline_jobs()
        self._initialize_vps_nodes()

    def _initialize_vps_nodes(self) -> None:
        if not self.database.is_file():
            return
        with closing(self._write_connection()) as connection:
            connection.executescript(AUTHOR_JOB_SCHEMA)
            if connection.execute("SELECT 1 FROM vps_nodes LIMIT 1").fetchone() is None:
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                connection.execute(
                    "INSERT INTO vps_nodes(name,base_url,ssh_command,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (
                        "VPS-01", "http://127.0.0.1:18787",
                        "ssh -N -L 18787:127.0.0.1:8787 ubuntu@43.161.250.107",
                        1, timestamp, timestamp,
                    ),
                )
            connection.commit()

    def _write_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_manual_work_allowed(self) -> None:
        state = self.scheduler_store.state()
        heartbeat = str(state.get("heartbeat_at") or "")
        online = False
        if heartbeat and state.get("pid"):
            try:
                age = (datetime.now().astimezone() - datetime.fromisoformat(heartbeat)).total_seconds()
                online = 0 <= age <= 15
            except ValueError:
                pass
        if online and (
            str(state.get("desired_state") or "") in {"running", "draining", "restarting"}
            or str(state.get("actual_state") or "") == "running"
        ):
            raise RuntimeError("自动调度器正在运行，请先暂停或停止调度器，再启动手动任务")

    @staticmethod
    def _validate_vps_node(body: dict[str, object]) -> tuple[int | None, str, str, str, int]:
        raw_id = body.get("id")
        node_id = None if raw_id in (None, "") else int(raw_id)
        name = str(body.get("name", "")).strip()
        base_url = str(body.get("base_url", "")).strip().rstrip("/")
        ssh_command = str(body.get("ssh_command", "")).strip()
        enabled = body.get("enabled", True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{1,47}", name):
            raise ValueError("VPS 名称必须是 2-48 位字母、数字、空格、下划线或连字符")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("VPS 地址必须是完整的 HTTP(S) 地址")
        if isinstance(enabled, bool):
            enabled_value = int(enabled)
        else:
            raise ValueError("VPS 启用状态无效")
        if len(ssh_command) > 500 or "\x00" in ssh_command:
            raise ValueError("SSH 隧道命令无效")
        return node_id, name, base_url, ssh_command, enabled_value

    def save_vps_node(self, body: dict[str, object]) -> dict:
        node_id, name, base_url, ssh_command, enabled = self._validate_vps_node(body)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(self._write_connection()) as connection:
            if node_id is None:
                cursor = connection.execute(
                    "INSERT INTO vps_nodes(name,base_url,ssh_command,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (name, base_url, ssh_command, enabled, timestamp, timestamp),
                )
                node_id = int(cursor.lastrowid)
            else:
                result = connection.execute(
                    "UPDATE vps_nodes SET name=?,base_url=?,ssh_command=?,enabled=?,updated_at=? WHERE id=?",
                    (name, base_url, ssh_command, enabled, timestamp, node_id),
                )
                if result.rowcount != 1:
                    raise ValueError("VPS 节点不存在")
            connection.commit()
        return {"id": node_id, "name": name, "base_url": base_url, "ssh_command": ssh_command, "enabled": bool(enabled)}

    def delete_vps_node(self, node_id: object) -> None:
        try:
            value = int(node_id)
        except (TypeError, ValueError):
            raise ValueError("VPS 节点编号无效") from None
        with closing(self._write_connection()) as connection:
            result = connection.execute("DELETE FROM vps_nodes WHERE id=?", (value,))
            if result.rowcount != 1:
                raise ValueError("VPS 节点不存在")
            connection.commit()

    def _vps_request(self, node: dict, path: str, *, method: str = "GET", payload: object = None) -> tuple[bytes, dict[str, str]]:
        base_url = str(node["base_url"]).rstrip("/") + "/"
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(urljoin(base_url, path.lstrip("/")), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.read(), {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"{node['name']} 请求失败（{exc.code}）：{detail}") from exc
        except OSError as exc:
            raise RuntimeError(f"{node['name']} 不可达：{exc}") from exc

    def _node_rows(self) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id,name,base_url,ssh_command,enabled,created_at,updated_at FROM vps_nodes ORDER BY name,id"
            ).fetchall()
        return [dict(row) for row in rows]

    def vps_nodes(self) -> list[dict]:
        result = []
        for node in self._node_rows():
            item = {**node, "enabled": bool(node["enabled"]), "status": "disabled" if not node["enabled"] else "offline", "error": ""}
            if node["enabled"]:
                try:
                    payload, _headers = self._vps_request(node, "/api/dashboard")
                    item["status"] = "online"
                    item["dashboard"] = json.loads(payload.decode("utf-8"))
                except (OSError, RuntimeError, ValueError) as exc:
                    item["error"] = str(exc)
            result.append(item)
        return result

    def vps_dashboard(self, node_id: object) -> dict:
        node = next((row for row in self._node_rows() if int(row["id"]) == int(node_id)), None)
        if node is None:
            raise ValueError("VPS 节点不存在")
        payload, _headers = self._vps_request(node, "/api/dashboard")
        return json.loads(payload.decode("utf-8"))

    def vps_delivery_package(self, node_id: object, batch: object) -> tuple[str, bytes]:
        node = next((row for row in self._node_rows() if int(row["id"]) == int(node_id)), None)
        if node is None:
            raise ValueError("VPS 节点不存在")
        batch_name = str(batch or "")
        if not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        payload, headers = self._vps_request(node, "/api/delivery-package?batch=" + quote(batch_name))
        disposition = headers.get("content-disposition", "")
        match = re.search(r'filename="([^"]+)"', disposition)
        return (match.group(1) if match else f"ccusr-delivery-{batch_name}.zip"), payload

    def _vps_node(self, node_id: object) -> dict:
        try:
            value = int(node_id)
        except (TypeError, ValueError):
            raise ValueError("VPS 节点编号无效") from None
        node = next((row for row in self._node_rows() if int(row["id"]) == value), None)
        if node is None:
            raise ValueError("VPS 节点不存在")
        return node

    def vps_scheduler(self, node_id: object) -> dict:
        node = self._vps_node(node_id)
        payload, _headers = self._vps_request(node, "/api/scheduler")
        result = json.loads(payload.decode("utf-8"))
        result["node"] = {
            "id": node["id"], "name": node["name"], "base_url": node["base_url"], "kind": "remote",
        }
        return result

    def vps_scheduler_events(self, node_id: object, query: str) -> dict:
        node = self._vps_node(node_id)
        path = "/api/scheduler/events" + (f"?{query}" if query else "")
        payload, _headers = self._vps_request(node, path)
        return json.loads(payload.decode("utf-8"))

    def vps_scheduler_logs(self, node_id: object, query: str) -> dict:
        node = self._vps_node(node_id)
        path = "/api/scheduler/logs" + (f"?{query}" if query else "")
        payload, _headers = self._vps_request(node, path)
        return json.loads(payload.decode("utf-8"))

    def vps_scheduler_run_output(self, node_id: object, run_id: object, query: str) -> dict:
        node = self._vps_node(node_id)
        value = int(run_id)
        path = f"/api/scheduler/runs/{value}/output" + (f"?{query}" if query else "")
        payload, _headers = self._vps_request(node, path)
        return json.loads(payload.decode("utf-8"))

    def vps_scheduler_control(self, node_id: object, action: object) -> dict:
        node = self._vps_node(node_id)
        payload, _headers = self._vps_request(
            node, "/api/scheduler/control", method="POST", payload={"action": action},
        )
        return json.loads(payload.decode("utf-8"))

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

    @classmethod
    def author_prompt(cls, batch: str, count: int, business: str, technology: str, notes: str,
                      mode: str = "0-1", task_type: str = "0-1 代码生成", mother: dict | None = None,
                      derived_notes: str = "", defect_tolerance: str = "", difficulty: object = "中等") -> str:
        difficulty_plan = cls._author_difficulty_plan(difficulty)
        difficulty_text = cls._difficulty_prompt(count, difficulty_plan)
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
                f"技术范围：{backend_only_requirement()}\n"
                f"批次名：{batch}\n题目数量：{count}\n题型：{task_type}\n"
                f"母库信息：{mother_text}\n难度分配：{difficulty_text}。每道题的 difficulty 字段必须严格按此分配填写。\n出题要求：{requirements or '根据母项目代码、已登记快照和《项目规范.md》自动生成，不需要额外填写关键词。'}\n"
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
            f"技术范围：{backend_only_requirement()}\n"
            f"批次名：{batch}\n题目数量：{count}\n难度分配：{difficulty_text}。每道题的 difficulty 字段必须严格按此分配填写。\n出题要求：{requirements}\n"
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
                "FROM pipeline_jobs p ORDER BY p.created_at DESC,p.id DESC LIMIT ?",
                (max(1, min(limit, 50)),),
            ).fetchall()
            job_ids = [int(job["id"]) for job in jobs]
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                items = connection.execute(
                    "SELECT pi.id,pi.pipeline_job_id,pi.question_id,pi.question_no,pi.status,pi.error,"
                    "pi.heartbeat_at,pi.activity_at,pi.health_status,pi.health_detail,pi.started_at,pi.finished_at,"
                    "q.task_id,q.title,(SELECT COUNT(*) FROM runs r WHERE r.question_id=pi.question_id) AS model_attempts "
                    "FROM pipeline_items pi JOIN questions q ON q.id=pi.question_id "
                    f"WHERE pi.pipeline_job_id IN ({placeholders}) "
                    "ORDER BY pi.pipeline_job_id DESC,pi.question_no",
                    job_ids,
                ).fetchall()
            else:
                items = []
        by_job: dict[int, list[dict]] = {}
        for item in items:
            by_job.setdefault(int(item["pipeline_job_id"]), []).append(dict(item))
        latest_by_batch: dict[str, int] = {}
        for job in jobs:
            latest_by_batch.setdefault(str(job["batch_name"]), int(job["id"]))
        return [
            {
                **dict(job),
                "items": by_job.get(int(job["id"]), []),
                "can_retry": (
                    job["status"] in {"failed", "interrupted"}
                    and latest_by_batch.get(str(job["batch_name"])) == int(job["id"])
                ),
            }
            for job in jobs
        ]

    def create_pipeline_job(self, body: dict[str, object]) -> dict[str, object]:
        self._ensure_manual_work_allowed()
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
                    return max(1, min(int(values.get(key, str(default))), 32))
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
            pipeline_env = self._subprocess_env()
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
        self._ensure_manual_work_allowed()
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
        env = self.read_env()
        difficulty = self._author_difficulty_plan(
            env.get("CC_AUTHOR_DIFFICULTY_WEIGHTS", ""), env.get("CC_AUTHOR_DIFFICULTY", "中等")
        )
        prompt = self.author_prompt(batch, count, business, technology, notes, mode, task_type, mother, derived_notes, defect_tolerance, difficulty)
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
        value = re.sub(r"(?i)(?:gh[pousr]_\w+|github_pat_\w+)", "[REDACTED]", value)
        value = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", value)
        return re.sub(
            r"(?i)((?:anthropic_auth_token|github_token|gh_token|access_token|password)\s*=\s*)\S+",
            r"\1[REDACTED]",
            value,
        )

    def _subprocess_env(self) -> dict[str, str]:
        environment = os.environ.copy()
        github_token = self.read_env().get("CC_GITHUB_TOKEN", "").strip()
        if github_token:
            environment["GH_TOKEN"] = github_token
            environment["GITHUB_TOKEN"] = github_token
        return environment

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
            author_env = self._subprocess_env()
            process = subprocess.Popen(
                command, cwd=self.project_root, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, creationflags=flags, env=author_env,
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
    def _command_text(command: list[str], cwd: Path, timeout: int = 4) -> str:
        try:
            result = subprocess.run(
                command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _scheduler_resources(self) -> dict:
        disk = shutil.disk_usage(self.project_root)
        try:
            load = [round(value, 2) for value in os.getloadavg()]
        except (AttributeError, OSError):
            load = []
        memory_total = 0
        memory_available = 0
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            values = {}
            for line in meminfo.read_text(encoding="ascii", errors="ignore").splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                try:
                    values[key] = int(raw.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    continue
            memory_total = values.get("MemTotal", 0)
            memory_available = values.get("MemAvailable", 0)
        elif sys.platform == "darwin":
            raw_total = self._command_text(["sysctl", "-n", "hw.memsize"], self.project_root)
            memory_total = int(raw_total) if raw_total.isdigit() else 0
            raw_rss = self._command_text(["ps", "-A", "-o", "rss="], self.project_root)
            try:
                used = sum(int(value) for value in raw_rss.split()) * 1024
            except ValueError:
                used = 0
            memory_available = max(0, memory_total - used)
        return {
            "cpu_count": os.cpu_count() or 1,
            "load_average": load,
            "memory_total": memory_total,
            "memory_used": max(0, memory_total - memory_available),
            "memory_percent": round((memory_total - memory_available) * 100 / memory_total, 1) if memory_total else None,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_free": disk.free,
            "disk_percent": round(disk.used * 100 / disk.total, 1) if disk.total else None,
        }

    def _scheduler_git(self) -> dict:
        return {
            "branch": self._command_text(["git", "branch", "--show-current"], self.project_root),
            "commit": self._command_text(["git", "rev-parse", "--short", "HEAD"], self.project_root),
            "dirty": bool(self._command_text(["git", "status", "--porcelain"], self.project_root)),
        }

    def _scheduler_containers(self) -> list[dict]:
        raw = self._command_text(
            ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"],
            self.project_root,
        )
        containers = []
        for line in raw.splitlines():
            fields = line.split("\t", 3)
            if len(fields) == 4:
                containers.append(dict(zip(("id", "name", "image", "status"), fields)))
        return containers

    def _registered_run_artifact(self, raw: object, *, directory: bool = False) -> Path | None:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            path = Path(value).resolve(strict=True)
            root = (self.project_root / "runs").resolve(strict=True)
        except OSError:
            return None
        if root != path and root not in path.parents:
            return None
        if directory and not path.is_dir():
            return None
        if not directory and not path.is_file():
            return None
        return path

    def _scheduler_active_runs(self) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT r.id,r.batch_run_id,r.status,r.started_at,r.heartbeat_at,r.container_id,"
                "r.log_path,r.trajectory_root,q.task_id,q.question_no,q.difficulty,q.languages,b.name AS batch_name "
                "FROM runs r JOIN questions q ON q.id=r.question_id JOIN batches b ON b.id=q.batch_id "
                "WHERE r.status='running' ORDER BY r.started_at,r.id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            activity = 0.0
            log_path = self._registered_run_artifact(row["log_path"])
            if log_path:
                try:
                    stat = log_path.stat()
                    activity = max(activity, stat.st_mtime)
                    item["output_bytes"] = stat.st_size
                except OSError:
                    item["output_bytes"] = 0
            else:
                item["output_bytes"] = 0
            trajectory = self._registered_run_artifact(row["trajectory_root"], directory=True)
            if trajectory:
                try:
                    activity = max(
                        [activity, *(path.stat().st_mtime for path in trajectory.rglob("*.jsonl"))]
                    )
                except OSError:
                    pass
            item["activity_at"] = (
                datetime.fromtimestamp(activity).astimezone().isoformat(timespec="seconds")
                if activity else str(row["heartbeat_at"] or row["started_at"] or "")
            )
            item.pop("log_path", None)
            item.pop("trajectory_root", None)
            result.append(item)
        return result

    @staticmethod
    def _tool_event_text(block: dict) -> str:
        name = str(block.get("name") or "Tool")
        value = block.get("input")
        if isinstance(value, dict):
            detail = value.get("command") or value.get("file_path") or value.get("path")
            if not detail:
                detail = json.dumps(value, ensure_ascii=False)
        else:
            detail = value
        text = str(detail or "").strip()
        return f"{name}: {text}" if text else name

    def _claude_output_events(self, line: str, *, streaming: bool) -> list[dict]:
        redacted = self._redact_log(line.strip())
        if not redacted:
            return []
        try:
            payload = json.loads(redacted)
        except json.JSONDecodeError:
            return [{"kind": "output", "text": redacted}]
        if not isinstance(payload, dict):
            return [{"kind": "output", "text": redacted}]
        timestamp = str(payload.get("timestamp") or "")
        kind = str(payload.get("type") or "")
        events: list[dict] = []
        if kind == "stream_event":
            event = payload.get("event")
            if isinstance(event, dict) and event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta" and delta.get("text"):
                    events.append({"kind": "delta", "text": str(delta["text"]), "timestamp": timestamp})
            return events
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    events.append({"kind": "tool", "text": self._redact_log(self._tool_event_text(block))[:4000], "timestamp": timestamp})
                elif block.get("type") == "text" and block.get("text") and not streaming:
                    events.append({"kind": "output", "text": self._redact_log(str(block["text"]))[:8000], "timestamp": timestamp})
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                value = block.get("content")
                if isinstance(value, list):
                    value = "\n".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
                if value:
                    events.append({"kind": "result", "text": self._redact_log(str(value))[:8000], "timestamp": timestamp})
        elif kind == "result":
            value = payload.get("result") or payload.get("subtype") or "Claude 运行结束"
            events.append({"kind": "complete", "text": self._redact_log(str(value))[:8000], "timestamp": timestamp})
        elif kind == "system" and payload.get("subtype") == "init":
            events.append({"kind": "system", "text": "Claude 会话已启动", "timestamp": timestamp})
        return events

    def scheduler_run_output(self, run_id: object, query: dict[str, list[str]]) -> dict:
        try:
            value = int(run_id)
            after = max(0, int(query.get("after", ["0"])[0]))
        except (TypeError, ValueError):
            raise ValueError("运行编号或日志游标无效") from None
        requested_source = str(query.get("source", [""])[0])
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT r.id,r.status,r.log_path,r.trajectory_root,q.task_id,b.name AS batch_name "
                "FROM runs r JOIN questions q ON q.id=r.question_id JOIN batches b ON b.id=q.batch_id WHERE r.id=?",
                (value,),
            ).fetchone()
        if row is None:
            raise ValueError("运行记录不存在")
        path = self._registered_run_artifact(row["log_path"])
        source = f"log:{value}"
        streaming = True
        try:
            log_size = path.stat().st_size if path else 0
        except OSError:
            path = None
            log_size = 0
        if path is None or log_size == 0:
            root = self._registered_run_artifact(row["trajectory_root"], directory=True)
            try:
                candidates = sorted(root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime) if root else []
            except OSError:
                candidates = []
            path = candidates[-1] if candidates else None
            source = (
                "trajectory:" + path.relative_to(root).as_posix()
                if path and root else "pending"
            )
            streaming = False
        if path is None:
            return {"run_id": value, "source": source, "next_after": 0, "events": [], "status": row["status"]}
        if requested_source and requested_source != source:
            after = 0
        try:
            size = path.stat().st_size
        except OSError:
            return {"run_id": value, "source": "pending", "next_after": 0, "events": [], "status": row["status"]}
        truncated = False
        if after > size:
            after = 0
        if after == 0 and size > 524_288:
            after = size - 524_288
            truncated = True
        events: list[dict] = []
        with path.open("rb") as handle:
            handle.seek(after)
            if truncated:
                handle.readline()
            while len(events) < 500:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                # A running process can be observed between two writes. Keep the
                # cursor before an unfinished JSONL record so the next poll can
                # parse the complete line on every supported host OS.
                if not raw.endswith(b"\n") and row["status"] == "running":
                    handle.seek(line_start)
                    break
                for event in self._claude_output_events(raw.decode("utf-8", errors="replace"), streaming=streaming):
                    events.append(event)
                    if len(events) >= 500:
                        break
            next_after = handle.tell()
        return {
            "run_id": value, "task_id": row["task_id"], "batch_name": row["batch_name"],
            "source": source, "next_after": next_after, "events": events,
            "status": row["status"], "truncated": truncated,
        }

    def scheduler_snapshot(self) -> dict:
        state = self.scheduler_store.state()
        heartbeat_age = None
        heartbeat = str(state.get("heartbeat_at") or "")
        if heartbeat:
            try:
                heartbeat_age = max(0, int((datetime.now().astimezone() - datetime.fromisoformat(heartbeat)).total_seconds()))
            except ValueError:
                pass
        process_online = heartbeat_age is not None and heartbeat_age <= 15
        with closing(self.connect()) as connection:
            queue = {
                "news_ready": int(connection.execute("SELECT COUNT(*) FROM news_topics WHERE status='new'").fetchone()[0]),
                "batches_active": int(connection.execute(
                    "SELECT COUNT(*) FROM batches WHERE status NOT IN ('completed','partial','failed')"
                ).fetchone()[0]),
                "questions_ready": int(connection.execute("SELECT COUNT(*) FROM questions WHERE status='approved'").fetchone()[0]),
                "questions_running": int(connection.execute("SELECT COUNT(*) FROM questions WHERE status='running'").fetchone()[0]),
                "questions_completed": int(connection.execute("SELECT COUNT(*) FROM questions WHERE status='completed'").fetchone()[0]),
                "deliveries_passed": int(connection.execute("SELECT COUNT(*) FROM records WHERE delivery_qc_passed=1").fetchone()[0]),
            }
            batch_rows = connection.execute(
                "SELECT b.name,b.status,b.created_at,COUNT(DISTINCT q.id) AS total,"
                "COUNT(DISTINCT CASE WHEN q.status='completed' THEN q.id END) AS completed,"
                "COUNT(DISTINCT CASE WHEN x.status='running' THEN q.id END) AS running,"
                "COUNT(DISTINCT r.question_id) AS records,"
                "COUNT(DISTINCT CASE WHEN r.delivery_qc_passed=1 THEN r.question_id END) AS qc_passed "
                "FROM batches b LEFT JOIN questions q ON q.batch_id=b.id "
                "LEFT JOIN runs x ON x.question_id=q.id "
                "LEFT JOIN records r ON r.question_id=q.id "
                "GROUP BY b.id ORDER BY CASE WHEN b.status IN ('completed','partial','failed') THEN 1 ELSE 0 END,b.created_at DESC LIMIT 12"
            ).fetchall()
            batches = [dict(row) for row in batch_rows]
        config = self.env_config()
        return {
            "node": {"id": "local", "name": socket.gethostname(), "kind": "local", "base_url": ""},
            "state": {**state, "process_online": process_online, "heartbeat_age_seconds": heartbeat_age},
            "queue": queue,
            "batches": batches,
            "resources": self._scheduler_resources(),
            "git": self._scheduler_git(),
            "containers": self._scheduler_containers(),
            "active_runs": self._scheduler_active_runs(),
            "cycles": self.scheduler_store.cycles(20),
            "config": {
                "batch_size": config["author_batch_size"],
                "difficulty_weights": config["author_difficulty_weights"],
                "model_concurrency": config["model_concurrency"],
                "qc_concurrency": config["qc_concurrency"],
                "codex_concurrency": config["codex_concurrency"],
                "ready_target": config["ready_target"],
                "worker_cpus": config["worker_cpus"],
                "worker_memory": config["worker_memory"],
                "news_feeds": config["news_feeds"],
            },
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def scheduler_events(self, query: dict[str, list[str]]) -> dict:
        def first(name: str, default: str = "") -> str:
            return str(query.get(name, [default])[0])

        try:
            after_id = max(0, int(first("after", "0")))
            limit = max(1, min(1000, int(first("limit", "500"))))
        except ValueError:
            raise ValueError("日志游标或数量无效") from None
        events = self.scheduler_store.events(after_id=after_id, limit=limit)
        level = first("level")
        phase = first("phase")
        batch = first("batch")
        search = first("search").casefold()
        if level:
            events = [item for item in events if item["level"] == level]
        if phase:
            events = [item for item in events if item["phase"] == phase]
        if batch:
            events = [item for item in events if item["batch_name"] == batch]
        if search:
            events = [item for item in events if search in json.dumps(item, ensure_ascii=False).casefold()]
        return {"events": events, "next_after": events[-1]["id"] if events else after_id}

    def scheduler_raw_logs(self, query: dict[str, list[str]]) -> dict:
        try:
            limit = max(1, min(2000, int(query.get("limit", ["500"])[0])))
        except ValueError:
            raise ValueError("日志数量无效") from None
        search = str(query.get("search", [""])[0]).casefold()
        batch = str(query.get("batch", [""])[0])
        log_root = self.project_root / "runs" / "daemon"
        paths = sorted(log_root.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True) if log_root.is_dir() else []
        if batch:
            paths = [path for path in paths if batch in path.name or path.name == "delivery.log"]
        lines: list[dict] = []
        for path in paths[:20]:
            try:
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in content[-limit:]:
                redacted = self._redact_log(line)
                if search and search not in redacted.casefold():
                    continue
                lines.append({"source": path.name, "line": redacted})
        return {"lines": lines[-limit:]}

    def scheduler_control(self, action: object) -> dict:
        normalized = str(action or "").strip().lower()
        result = self.scheduler_store.request_control(normalized)
        state = self.scheduler_store.state()
        heartbeat = str(state.get("heartbeat_at") or "")
        online = False
        if heartbeat:
            try:
                online = (datetime.now().astimezone() - datetime.fromisoformat(heartbeat)).total_seconds() <= 15
            except ValueError:
                pass
        if normalized in {"start", "author_only", "resume", "retry", "restart"} and not online:
            result["started_process"] = self._start_scheduler_process()
            result["message"] = "调度器启动指令已发送"
        return result

    def _start_scheduler_process(self) -> bool:
        if sys.platform.startswith("linux") and shutil.which("systemctl"):
            try:
                service = subprocess.run(
                    ["sudo", "-n", "systemctl", "start", "ccusr-pipeline.service"],
                    cwd=self.project_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, check=False,
                )
                if service.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        if sys.platform == "darwin" and shutil.which("launchctl"):
            try:
                service = subprocess.run(
                    ["launchctl", "kickstart", f"gui/{os.getuid()}/com.ccusr.pipeline"],
                    cwd=self.project_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, check=False,
                )
                if service.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        if sys.platform == "win32" and shutil.which("schtasks"):
            try:
                task = subprocess.run(
                    ["schtasks", "/Run", "/TN", "CCUSR Scheduler"],
                    cwd=self.project_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=15, check=False,
                )
                if task.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        log_root = self.project_root / "runs" / "daemon"
        log_root.mkdir(parents=True, exist_ok=True)
        log = (log_root / "console-started-pipeline.log").open("a", encoding="utf-8")
        try:
            process_kwargs = {
                "creationflags": (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            } if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen(
                [sys.executable, str(self.project_root / "tools" / "pipeline_daemon.py"), "--loop"],
                cwd=self.project_root, stdout=log, stderr=subprocess.STDOUT,
                **process_kwargs,
            )
        except OSError:
            log.close()
            return False
        log.close()
        return True

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
        for line in read_portable_text(self.env_file).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw = stripped.split("=", 1)
            key = key.removeprefix("export ").strip()
            if key in values:
                values[key] = self._env_value(raw)
        return values

    @staticmethod
    def _author_batch_size(raw: object) -> int:
        try:
            value = int(str(raw or "10").strip())
        except (TypeError, ValueError):
            return 10
        return max(1, min(value, 20))

    @staticmethod
    def _author_difficulty_plan(raw: object, legacy: object = "中等") -> dict[str, int]:
        """Normalize selected authoring difficulties to positive integer weights."""
        parsed: object = None
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        elif isinstance(raw, dict):
            parsed = raw
        if not isinstance(parsed, dict):
            parsed = {str(legacy or "中等").strip() or "中等": 100}
        plan: dict[str, int] = {}
        for difficulty in AUTHOR_DIFFICULTIES:
            value = parsed.get(difficulty, 0)
            try:
                weight = int(value)
            except (TypeError, ValueError):
                weight = 0
            if weight > 0:
                plan[difficulty] = weight
        return plan or {"中等": 100}

    @staticmethod
    def _difficulty_counts(count: int, plan: dict[str, int]) -> dict[str, int]:
        total_weight = sum(plan.values())
        if count <= 0 or total_weight <= 0:
            return {}
        entries = []
        allocated = 0
        for order, difficulty in enumerate(AUTHOR_DIFFICULTIES):
            weight = plan.get(difficulty, 0)
            if weight <= 0:
                continue
            raw = count * weight / total_weight
            base = int(raw)
            allocated += base
            entries.append((raw - base, -order, difficulty, base))
        for _fraction, _order, difficulty, _base in sorted(entries, reverse=True)[: count - allocated]:
            index = next(index for index, item in enumerate(entries) if item[2] == difficulty)
            fraction, order, name, base = entries[index]
            entries[index] = (fraction, order, name, base + 1)
        return {difficulty: base for _fraction, _order, difficulty, base in entries if base > 0}

    @classmethod
    def _difficulty_prompt(cls, count: int, plan: dict[str, int]) -> str:
        total = sum(plan.values()) or 1
        counts = cls._difficulty_counts(count, plan)
        return "、".join(
            f"{difficulty} {counts.get(difficulty, 0)} 道（{plan[difficulty] / total:.0%}）"
            for difficulty in AUTHOR_DIFFICULTIES if difficulty in plan
        )

    def env_config(self) -> dict[str, object]:
        values = self.read_env()
        key = values["CC_SWITCH_API_KEY"]
        github_token = values.get("CC_GITHUB_TOKEN", "")
        difficulty_plan = self._author_difficulty_plan(
            values.get("CC_AUTHOR_DIFFICULTY_WEIGHTS", ""), values.get("CC_AUTHOR_DIFFICULTY", "中等")
        )
        def env_int(name: str, default: int) -> int:
            try:
                return max(1, min(int(values.get(name, str(default))), 32))
            except ValueError:
                return default
        def env_bounded(name: str, default: int, maximum: int = 3600) -> int:
            try:
                return max(1, min(int(values.get(name, str(default))), maximum))
            except ValueError:
                return default
        with closing(self.connect()) as connection:
            news_feeds = [
                {**dict(row), "enabled": bool(row["enabled"])}
                for row in connection.execute(
                    "SELECT id,url,enabled,created_at,updated_at FROM news_feeds ORDER BY id"
                )
            ]
        return {
            "base_url": values["CC_SWITCH_BASE_URL"],
            "model": values["CC_SWITCH_MODEL"],
            "submitter": values["CC_USR_SUBMITTER"],
            "api_key_configured": bool(key),
            "api_key_hint": f"已配置（末尾 {key[-4:]}）" if len(key) >= 4 else ("已配置" if key else "未配置"),
            "github_token_configured": bool(github_token),
            "github_token_hint": f"已配置（末尾 {github_token[-4:]}）" if len(github_token) >= 4 else ("已配置" if github_token else "未配置"),
            "author_difficulty_weights": difficulty_plan,
            "author_batch_size": self._author_batch_size(values.get("CC_AUTHOR_BATCH_SIZE", "")),
            **runtime_info(),
            "docker_image": values.get("CC_CLAUDE_DOCKER_IMAGE", "").strip() or "claude-cli:latest",
            "docker_command": values.get("CC_CLAUDE_DOCKER_COMMAND", "").strip() or "claude",
            "model_mode": values.get("CC_PIPELINE_MODEL_MODE", "local").strip().lower() or "local",
            "qc_concurrency": env_int("CC_PIPELINE_QC_CONCURRENCY", 2),
            "model_concurrency": env_int("CC_PIPELINE_MODEL_CONCURRENCY", 2),
            "codex_concurrency": env_int("CC_PIPELINE_CODEX_CONCURRENCY", 2),
            "ready_target": env_bounded("CC_PIPELINE_READY_TARGET", 40, 200),
            "worker_cpus": values.get("CC_CLAUDE_WORKER_CPUS", "1").strip() or "1",
            "worker_memory": values.get("CC_CLAUDE_WORKER_MEMORY", "2g").strip().lower() or "2g",
            "gateway_max_attempts": env_bounded("CC_GATEWAY_MAX_ATTEMPTS", 3, 10),
            "gateway_backoff_base": env_bounded("CC_GATEWAY_BACKOFF_BASE", 30),
            "gateway_backoff_max": env_bounded("CC_GATEWAY_BACKOFF_MAX", 300),
            "gateway_circuit_threshold": env_bounded("CC_GATEWAY_CIRCUIT_THRESHOLD", 2, 20),
            "gateway_circuit_window": env_bounded("CC_GATEWAY_CIRCUIT_WINDOW", 120),
            "gateway_circuit_cooldown": env_bounded("CC_GATEWAY_CIRCUIT_COOLDOWN", 180),
            "solo2_origin": values.get("CC_SOLO2_ORIGIN", "").strip() or "https://solo2.jzxhnh.com",
            "solo2_auto_submit": values.get("CC_SOLO2_AUTO_SUBMIT", "false").strip().lower() in {"1", "true", "yes", "on"},
            "solo2_concurrency": env_int("CC_SOLO2_CONCURRENCY", 1),
            "solo2_max_attempts": env_bounded("CC_SOLO2_MAX_ATTEMPTS", 3, 10),
            "news_feeds": news_feeds,
        }

    @staticmethod
    def concurrency_recommendation() -> dict[str, object]:
        return scheduler_capacity()

    @staticmethod
    def _validate_news_feeds(raw: object) -> list[tuple[str, int]]:
        if not isinstance(raw, list) or not raw or len(raw) > NEWS_URL_MAX:
            raise ValueError(f"新闻来源必须是 1-{NEWS_URL_MAX} 条")
        result: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("新闻来源配置格式无效")
            url = item.get("url", "")
            if not isinstance(url, str) or "\x00" in url or len(url.strip()) > 500:
                raise ValueError("新闻来源 URL 无效")
            url = url.strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("新闻来源必须是完整的 HTTP(S) 地址")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("新闻来源 URL 不能包含账号或片段")
            if url in seen:
                raise ValueError("新闻来源 URL 不能重复")
            seen.add(url)
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("新闻来源启用状态无效")
            result.append((url, int(enabled)))
        if not any(enabled for _url, enabled in result):
            raise ValueError("至少启用一条新闻来源")
        return result

    def update_news_feeds(self, raw: object) -> list[dict]:
        feeds = self._validate_news_feeds(raw)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(self._write_connection()) as connection:
            connection.execute("DELETE FROM news_feeds")
            connection.executemany(
                "INSERT INTO news_feeds(url,enabled,created_at,updated_at) VALUES(?,?,?,?)",
                [(url, enabled, timestamp, timestamp) for url, enabled in feeds],
            )
            connection.commit()
        return [{"url": url, "enabled": bool(enabled)} for url, enabled in feeds]

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
            "github_token": body.get("github_token", ""),
            "author_difficulty_weights": body.get(
                "author_difficulty_weights",
                self._author_difficulty_plan(
                    current.get("CC_AUTHOR_DIFFICULTY_WEIGHTS", ""), current.get("CC_AUTHOR_DIFFICULTY", "中等")
                ),
            ),
            "author_batch_size": body.get("author_batch_size", current.get("CC_AUTHOR_BATCH_SIZE", "10") or "10"),
            "submitter": body.get("submitter", current["CC_USR_SUBMITTER"]),
            "docker_image": body.get("docker_image", current.get("CC_CLAUDE_DOCKER_IMAGE", "claude-cli:latest")),
            "docker_command": body.get("docker_command", current.get("CC_CLAUDE_DOCKER_COMMAND", "claude")),
            "model_mode": body.get("model_mode", current.get("CC_PIPELINE_MODEL_MODE", "local")) or "local",
        }
        values = self._validate_env_input(incoming)
        github_token = incoming["github_token"]
        if not isinstance(github_token, str) or "\x00" in github_token or "\n" in github_token or "\r" in github_token:
            raise ValueError("github_token 配置无效")
        github_token = github_token.strip() or current.get("CC_GITHUB_TOKEN", "")
        raw_difficulty_plan = incoming["author_difficulty_weights"]
        if not isinstance(raw_difficulty_plan, dict):
            raise ValueError("出题难度比例配置无效")
        difficulty_plan: dict[str, int] = {}
        for difficulty in AUTHOR_DIFFICULTIES:
            raw_weight = raw_difficulty_plan.get(difficulty, 0)
            if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, str)):
                raise ValueError(f"{difficulty}难度比例无效")
            try:
                weight = int(raw_weight)
            except ValueError as exc:
                raise ValueError(f"{difficulty}难度比例无效") from exc
            if not 0 <= weight <= 100:
                raise ValueError("难度比例必须是 0-100")
            if weight:
                difficulty_plan[difficulty] = weight
        if not difficulty_plan:
            raise ValueError("至少启用一种出题难度")
        if sum(difficulty_plan.values()) != 100:
            raise ValueError("启用难度的比例合计必须等于 100%")
        raw_batch_size = incoming["author_batch_size"]
        if isinstance(raw_batch_size, bool) or not isinstance(raw_batch_size, (int, str)):
            raise ValueError("author_batch_size 配置无效")
        try:
            author_batch_size = int(raw_batch_size)
        except ValueError as exc:
            raise ValueError("author_batch_size 配置无效") from exc
        if not 1 <= author_batch_size <= 20:
            raise ValueError("每批题数必须是 1-20")
        news_feeds = body.get("news_feeds")
        if news_feeds is not None:
            self.update_news_feeds(news_feeds)
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
            if not 1 <= value <= 32:
                raise ValueError(f"{field} 必须是 1-32")
            concurrency_values[env_key] = str(value)
        raw_ready_target = body.get(
            "ready_target", current.get("CC_PIPELINE_READY_TARGET", "40") or "40"
        )
        try:
            ready_target = int(raw_ready_target)
        except (TypeError, ValueError) as exc:
            raise ValueError("题目缓冲水位配置无效") from exc
        if not 1 <= ready_target <= 200:
            raise ValueError("题目缓冲水位必须是 1-200")
        raw_worker_cpus = body.get(
            "worker_cpus", current.get("CC_CLAUDE_WORKER_CPUS", "1") or "1"
        )
        try:
            worker_cpus = float(raw_worker_cpus)
        except (TypeError, ValueError) as exc:
            raise ValueError("单容器 CPU 配置无效") from exc
        if not 0.25 <= worker_cpus <= 4:
            raise ValueError("单容器 CPU 必须是 0.25-4")
        worker_memory = str(body.get(
            "worker_memory", current.get("CC_CLAUDE_WORKER_MEMORY", "2g") or "2g"
        )).strip().lower()
        if not re.fullmatch(r"[1-9][0-9]*[mg]", worker_memory):
            raise ValueError("单容器内存必须使用正整数加 m 或 g，例如 2g")
        gateway_values = {}
        for field, env_key, default, maximum in (
            ("gateway_max_attempts", "CC_GATEWAY_MAX_ATTEMPTS", 3, 10),
            ("gateway_backoff_base", "CC_GATEWAY_BACKOFF_BASE", 30, 3600),
            ("gateway_backoff_max", "CC_GATEWAY_BACKOFF_MAX", 300, 3600),
            ("gateway_circuit_threshold", "CC_GATEWAY_CIRCUIT_THRESHOLD", 2, 20),
            ("gateway_circuit_window", "CC_GATEWAY_CIRCUIT_WINDOW", 120, 3600),
            ("gateway_circuit_cooldown", "CC_GATEWAY_CIRCUIT_COOLDOWN", 180, 3600),
        ):
            raw = body.get(field, current.get(env_key, str(default)))
            if raw in (None, ""):
                raw = str(default)
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                raise ValueError(f"{field} 配置无效")
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{field} 配置无效") from exc
            if not 1 <= value <= maximum:
                raise ValueError(f"{field} 必须是 1-{maximum}")
            gateway_values[env_key] = str(value)
        if int(gateway_values["CC_GATEWAY_BACKOFF_BASE"]) > int(gateway_values["CC_GATEWAY_BACKOFF_MAX"]):
            raise ValueError("网关退避初始秒数不能大于上限秒数")
        default_solo2_origin = (
            current.get("CC_SOLO2_ORIGIN", "").strip() or "https://solo2.jzxhnh.com"
        )
        solo2_origin = str(body.get("solo2_origin", default_solo2_origin)).strip()
        parsed_solo2 = urlparse(solo2_origin)
        if parsed_solo2.scheme not in {"http", "https"} or not parsed_solo2.hostname:
            raise ValueError("SOLO2 地址必须是完整的 http(s) 地址")
        if parsed_solo2.username or parsed_solo2.password or parsed_solo2.query or parsed_solo2.fragment:
            raise ValueError("SOLO2 地址不能包含账号、密码、查询参数或片段")
        solo2_auto_submit = body.get(
            "solo2_auto_submit",
            current.get("CC_SOLO2_AUTO_SUBMIT", "false").strip().lower() in {"1", "true", "yes", "on"},
        )
        if not isinstance(solo2_auto_submit, bool):
            raise ValueError("SOLO2 自动提交开关无效")
        solo2_numbers: dict[str, str] = {}
        for field, key, default, maximum in (
            ("solo2_concurrency", "CC_SOLO2_CONCURRENCY", 1, 8),
            ("solo2_max_attempts", "CC_SOLO2_MAX_ATTEMPTS", 3, 10),
        ):
            try:
                value = int(body.get(field, current.get(key, str(default))) or default)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} 配置无效") from exc
            if not 1 <= value <= maximum:
                raise ValueError(f"{field} 必须是 1-{maximum}")
            solo2_numbers[key] = str(value)
        if not values["api_key"]:
            values["api_key"] = current["CC_SWITCH_API_KEY"]
        if not values["api_key"]:
            raise ValueError("API Key 尚未配置")
        output_values = {
            "CC_SWITCH_BASE_URL": values["base_url"],
            "CC_SWITCH_MODEL": values["model"],
            "CC_SWITCH_API_KEY": values["api_key"],
            "CC_USR_SUBMITTER": values["submitter"],
            "CC_GITHUB_TOKEN": github_token,
            "CC_AUTHOR_DIFFICULTY": next(iter(difficulty_plan)),
            "CC_AUTHOR_DIFFICULTY_WEIGHTS": json.dumps(difficulty_plan, ensure_ascii=False, separators=(",", ":")),
            "CC_AUTHOR_BATCH_SIZE": str(author_batch_size),
            "CC_CLAUDE_DOCKER_IMAGE": values["docker_image"],
            "CC_CLAUDE_DOCKER_COMMAND": values["docker_command"],
            "CC_PIPELINE_MODEL_MODE": values["model_mode"],
            "CC_PIPELINE_READY_TARGET": str(ready_target),
            "CC_CLAUDE_WORKER_CPUS": str(worker_cpus).rstrip("0").rstrip("."),
            "CC_CLAUDE_WORKER_MEMORY": worker_memory,
            **concurrency_values,
            **gateway_values,
            "CC_SOLO2_ORIGIN": solo2_origin,
            "CC_SOLO2_AUTO_SUBMIT": "true" if solo2_auto_submit else "false",
            **solo2_numbers,
        }
        for key, default in (
            ("CC_CLAUDE_HEARTBEAT_SECONDS", "5"),
            ("CC_CLAUDE_START_TIMEOUT", "300"),
            ("CC_CLAUDE_STALLED_TIMEOUT", "900"),
            ("CC_PIPELINE_WORKER_TIMEOUT", "3600"),
        ):
            output_values[key] = current.get(key, "").strip() or default
        lines = read_portable_text(self.env_file).splitlines() if self.env_file.exists() else []
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
            " WHERE q.batch_id=b.id AND r.delivery_qc_passed=1) AS qc_record_count, "
            "COALESCE((SELECT d.download_count FROM delivery_downloads d WHERE d.batch_id=b.id),0) AS download_count, "
            "COALESCE((SELECT d.last_downloaded_at FROM delivery_downloads d WHERE d.batch_id=b.id),'') AS last_downloaded_at "
            "FROM batches b ORDER BY b.created_at DESC, b.name DESC"
        ).fetchall()
        return [
            {
                "name": row["name"],
                "brief": row["brief"],
                "question_count": row["actual_questions"],
                "record_count": row["record_count"],
                "qc_record_count": row["qc_record_count"],
                "download_count": row["download_count"],
                "last_downloaded_at": row["last_downloaded_at"],
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

    def delivery_package(self, batch: object) -> tuple[str, bytes]:
        """Build an in-memory ZIP containing only registered delivery artifacts."""
        batch_name = str(batch or "")
        if not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT folder_path FROM batches WHERE name=?", (batch_name,)
            ).fetchone()
        if row is None:
            raise ValueError("批次不存在")
        batch_folder = Path(str(row["folder_path"])).resolve(strict=True)
        if not batch_folder.is_dir():
            raise ValueError("批次目录不可用")
        files: list[Path] = []
        for path in (*batch_folder.glob("CC_Codex*.xlsx"), *batch_folder.glob("轨迹_*.jsonl")):
            if not path.is_file() or path.name.startswith(".~"):
                continue
            resolved = path.resolve()
            if resolved.parent != batch_folder:
                continue
            if path.suffix.lower() == ".xlsx" and not path.name.startswith("CC_Codex"):
                continue
            if path.suffix.lower() == ".jsonl" and not path.name.startswith("轨迹_"):
                continue
            files.append(resolved)
        files = sorted(set(files), key=lambda item: item.name)
        if not files:
            raise ValueError("该批次暂无可下载的 Excel 或 JSONL 交付文件")
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        downloaded_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(self._write_connection()) as connection:
            connection.execute(
                "INSERT INTO delivery_downloads(batch_id,download_count,last_downloaded_at) "
                "SELECT id,1,? FROM batches WHERE name=? "
                "ON CONFLICT(batch_id) DO UPDATE SET "
                "download_count=delivery_downloads.download_count+1,last_downloaded_at=excluded.last_downloaded_at",
                (downloaded_at, batch_name),
            )
            connection.commit()
        return f"ccusr-delivery-{batch_name}.zip", payload.getvalue()

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
                "(SELECT x.status FROM runs x WHERE x.question_id=q.id "
                " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS latest_run_status, "
                "(SELECT x.error_message FROM runs x WHERE x.question_id=q.id "
                " ORDER BY x.launched_at DESC, x.id DESC LIMIT 1) AS latest_run_error, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id) AS record_count, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id "
                " AND r.delivery_qc_passed=1) AS passed_record_count, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id "
                " AND r.human_qc_approved=1 AND r.evidence_gate_passed=1 "
                " AND r.history_gate_passed=1) AS human_approved_record_count, "
                "(SELECT AVG((r.delivery_score+r.instruction_score+r.planning_score+"
                " r.reasoning_score+r.execution_score)/5.0) FROM records r "
                " WHERE r.question_id=q.id) AS average_score "
                ",(SELECT COUNT(*) FROM records r JOIN solo2_submissions s "
                " ON s.record_id=r.record_id WHERE r.question_id=q.id "
                " AND s.status='succeeded') AS solo2_submitted_count, "
                "EXISTS(SELECT 1 FROM pipeline_items pi JOIN pipeline_jobs pj ON pj.id=pi.pipeline_job_id "
                " WHERE pi.question_id=q.id AND pj.status IN ('queued','running') "
                " AND pi.status IN ('queued','qc_running','qc_passed','model_running','model_completed',"
                "'producing','produced','finalizing','awaiting_review')) AS pipeline_active "
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
                run_status = str(row["latest_run_status"] or "")
                run_complete = run_status == "succeeded" or (
                    not run_status and row["status"] == "completed"
                )
                record_count = int(row["record_count"] or 0)
                passed_count = int(row["passed_record_count"] or 0)
                delivery_qc = record_count > 0 and passed_count == record_count
                human_approved_count = int(row["human_approved_record_count"] or 0)
                human_qc = record_count > 0 and human_approved_count == record_count
                exported = human_qc and int(row["question_no"]) in exported_numbers
                solo2_submitted_count = int(row["solo2_submitted_count"] or 0)
                solo2_submitted = record_count > 0 and solo2_submitted_count == record_count
                delivered = exported or solo2_submitted
                pipeline_active = bool(row["pipeline_active"])
                if delivered:
                    stage_index, stage_label = 7, "已交付"
                elif not question_qc:
                    stage_index, stage_label = 1, "题目待质检"
                elif not run_complete:
                    if run_status == "running":
                        stage_index, stage_label = 2, "模型跑题中"
                    elif run_status in {"failed", "timeout"}:
                        stage_index, stage_label = 2, "模型跑题待重试"
                    elif pipeline_active:
                        stage_index, stage_label = 2, "已加入流水线"
                    else:
                        stage_index, stage_label = 2, "待模型跑题"
                elif record_count == 0:
                    stage_index, stage_label = 3, "待交付生产"
                elif not delivery_qc:
                    stage_index, stage_label = 4, "待交付质检"
                elif not human_qc:
                    stage_index, stage_label = 5, "待交付复核"
                elif not exported:
                    stage_index, stage_label = 6, "待导出"
                else:
                    stage_index, stage_label = 7, "已交付"
                if bool(row["maintenance_mode"]):
                    stage_index, stage_label = 2, "人工接管中"
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
                    "run_status": run_status,
                    "run_error": row["latest_run_error"] or "",
                    "launched_at": row["launched_at"] or "",
                    "session_id": row["latest_session_id"] or "",
                    "harness_version": row["harness_version"] or "",
                    "record_count": record_count,
                    "passed_record_count": passed_count,
                    "delivery_qc": delivery_qc,
                    "human_approved_record_count": human_approved_count,
                    "human_qc": human_qc,
                    "average_score": round(float(row["average_score"]), 1)
                    if row["average_score"] is not None else None,
                    "exported": exported,
                    "delivered": delivered,
                    "solo2_submitted_count": solo2_submitted_count,
                    "solo2_submitted": solo2_submitted,
                    "pipeline_active": pipeline_active,
                    "maintenance_mode": bool(row["maintenance_mode"]),
                    "maintenance_note": row["maintenance_note"] or "",
                    "reset_count": int(row["reset_count"] or 0),
                    "stage_index": stage_index,
                    "stage_label": stage_label,
                    "can_launch": question_qc and row["status"] == "approved" and not run_complete and not pipeline_active and not bool(row["maintenance_mode"]),
                    "can_takeover": run_status in {"running", "failed", "timeout", "interrupted"}
                    and not solo2_submitted and not bool(row["maintenance_mode"]),
                    "can_finish_takeover": bool(row["maintenance_mode"]),
                    "can_reset": run_count > 0 and not solo2_submitted,
                    "can_solo2_submit": human_qc and not solo2_submitted,
                })

        total = len(questions)
        stages = [
            {"id": 1, "label": "题目质检", "complete": sum(q["question_qc"] for q in questions)},
            {"id": 2, "label": "模型跑题", "complete": sum(q["run_status"] == "succeeded" for q in questions)},
            {"id": 3, "label": "交付生产", "complete": sum(q["record_count"] > 0 for q in questions)},
            {"id": 4, "label": "交付质检", "complete": sum(q["delivery_qc"] for q in questions)},
            {"id": 5, "label": "交付复核", "complete": sum(q["human_qc"] for q in questions)},
            {"id": 6, "label": "Excel / SOLO2 交付", "complete": sum(q["delivered"] for q in questions)},
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
                "delivered": sum(q["delivered"] for q in questions),
                "solo2_submitted": sum(q["solo2_submitted"] for q in questions),
                "qc_passed": sum(q["delivery_qc"] for q in questions),
                "waiting": sum(q["stage_index"] < 7 for q in questions),
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
                "SELECT batch_run_id, launched_at, session_id, harness, harness_version,status,"
                "finished_at,error_message,container_id "
                "FROM runs WHERE question_id=? ORDER BY launched_at DESC, id DESC",
                (question_id,),
            ).fetchall()
            records = connection.execute(
                "SELECT record_id, turn_no, user_prompt, session_id, turn_id, trajectory_file, "
                "delivery_score, delivery_description, instruction_score, instruction_description, "
                "planning_score, planning_description, reasoning_score, reasoning_description, "
                "execution_score, execution_description, other_issues, submitted_at, "
                "delivery_qc_passed, delivery_qc_note, delivery_qc_checked_at,"
                "human_qc_approved,human_qc_reviewer,human_qc_approved_at,human_qc_note,"
                "evidence_gate_passed,history_gate_passed,evidence_ledger,requirement_coverage,"
                "raw_user_prompt,raw_turn_id,is_continuation,continuation_count "
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
            "maintenance_mode": bool(question["maintenance_mode"]),
            "maintenance_note": question["maintenance_note"],
            "reset_count": int(question["reset_count"] or 0),
            "runs": [dict(row) for row in runs],
            "records": [dict(row) for row in records],
        }

    def delivery_reviews(self, batch: object | None = None) -> dict:
        batch_name = str(batch or "").strip() or None
        if batch_name and not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        return review_queue(self.database, batch_name)

    def approve_delivery_review(self, body: dict) -> dict:
        return approve_delivery_record(
            self.database,
            str(body.get("record_id") or ""),
            str(body.get("reviewer") or ""),
            body.get("confirmations"),
            str(body.get("note") or ""),
        )

    def reject_delivery_review(self, body: dict) -> dict:
        return reject_delivery_record(
            self.database,
            str(body.get("record_id") or ""),
            str(body.get("reviewer") or ""),
            str(body.get("note") or ""),
        )

    def sync_description_history(self) -> dict:
        with closing(self.connect()) as connection:
            nodes = connection.execute(
                "SELECT name,base_url FROM vps_nodes WHERE enabled=1 ORDER BY name"
            ).fetchall()
        imported = 0
        failures = []
        for node in nodes:
            try:
                payload, _headers = self._vps_request(dict(node), "/api/reviews/history")
                parsed = json.loads(payload.decode("utf-8"))
                result = import_history(
                    self.database, str(node["name"]), parsed.get("descriptions")
                )
                imported += int(result["imported"])
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                failures.append(f"{node['name']}：{exc}")
        if failures:
            raise RuntimeError("部分节点历史同步失败：" + "；".join(failures))
        return {"ok": True, "imported": imported, "nodes": len(nodes)}

    def create_codex_delivery_review(self, body: dict) -> dict:
        record_id = str(body.get("record_id") or "").strip()
        if not record_id:
            raise ValueError("请选择交付记录")
        dossier = build_codex_review_dossier(self.database, record_id)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT r.record_id,r.human_qc_approved FROM records r WHERE r.record_id=?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise ValueError("交付记录不存在")
            if bool(row["human_qc_approved"]):
                raise ValueError("记录已经完成最终复核；如需重审，请先退回记录")
            running = connection.execute(
                "SELECT details FROM record_review_events WHERE record_id=? "
                "AND action='codex_review_started' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            completed = connection.execute(
                "SELECT id FROM record_review_events WHERE record_id=? "
                "AND action IN ('codex_review_completed','codex_review_failed') "
                "ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            started = connection.execute(
                "SELECT id FROM record_review_events WHERE record_id=? "
                "AND action='codex_review_started' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if running and started and (not completed or int(started["id"]) > int(completed["id"])):
                raise ValueError("这条记录的 Codex 辅助复核正在运行")
            connection.execute(
                "INSERT INTO record_review_events(record_id,action,reviewer,note,details,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    record_id, "codex_review_started", "Codex 自动逐维复核", "",
                    json.dumps({"record_id": dossier["record_id"]}, ensure_ascii=False),
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        self.author_executor.submit(self._run_codex_delivery_review, record_id, dossier)
        return {"ok": True, "record_id": record_id, "message": "Codex 自动逐维复核已启动"}

    def _run_codex_delivery_review(self, record_id: str, dossier: dict) -> None:
        prompt = (
            "只读检查当前目录中的 review.json。资料已经过来源哈希和历史相似度机械校验。"
            "你仍须逐句判断五维描述是否被所列证据原文直接支持，分数是否与事实一致，"
            "需求是否覆盖，是否存在夸大、张冠李戴、套用句式、普通英文过多，或把后续结果算给目标轮次。"
            "任何一句缺少直接依据、任何分数与依据冲突、任何维度无法确认，都必须拒绝。"
            "只输出一个 JSON 对象，不要 Markdown 和额外文字。结构必须严格为："
            '{"decision":"approved或rejected","summary":"中文总结",'
            '"dimensions":{"delivery":{"approved":true,"reason":"中文依据"},'
            '"instruction":{"approved":true,"reason":"中文依据"},'
            '"planning":{"approved":true,"reason":"中文依据"},'
            '"reasoning":{"approved":true,"reason":"中文依据"},'
            '"execution":{"approved":true,"reason":"中文依据"}},'
            '"issues":["具体阻断问题"]}。只有五维都可由资料直接验证且没有阻断问题时才可 approved。'
        )
        action = "codex_review_completed"
        report = ""
        review: dict | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="ccusr-review-") as directory:
                review_root = Path(directory)
                output = review_root / "report.json"
                schema = review_root / "review.schema.json"
                (review_root / "review.json").write_text(
                    json.dumps(dossier, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                schema.write_text(
                    json.dumps(codex_review_schema(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        "codex", "exec", "--sandbox", "read-only", "--ephemeral",
                        "--skip-git-repo-check", "-C", str(review_root),
                        "--output-schema", str(schema),
                        "--output-last-message", str(output), prompt,
                    ],
                    cwd=review_root,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                    check=False,
                )
                raw = output.read_text(encoding="utf-8").strip() if output.is_file() else ""
                if result.returncode or not raw:
                    raise RuntimeError((result.stdout or "Codex 未返回复核报告")[-4000:])
                review = parse_codex_review(raw)
                report = codex_review_report(review)
                if review["decision"] == "approved":
                    approve_codex_record(self.database, record_id, review)
        except Exception as exc:
            action = "codex_review_failed"
            report = str(exc)[:4000]
        try:
            with closing(self.connect()) as connection:
                connection.execute(
                    "INSERT INTO record_review_events(record_id,action,reviewer,note,details,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        record_id, action, "Codex 自动逐维复核", "",
                        json.dumps({
                            "report": report,
                            "decision": review.get("decision", "") if review and action == "codex_review_completed" else "",
                            "dimensions": review.get("dimensions", {}) if review and action == "codex_review_completed" else {},
                        }, ensure_ascii=False),
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                    ),
                )
                connection.commit()
        except sqlite3.Error:
            pass

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

    def _solo2_settings(self) -> tuple[str, int]:
        values = self.read_env()
        origin = values.get("CC_SOLO2_ORIGIN", "").strip() or "https://solo2.jzxhnh.com"
        try:
            attempts = max(1, min(10, int(values.get("CC_SOLO2_MAX_ATTEMPTS", "3"))))
        except ValueError:
            attempts = 3
        return origin, attempts

    def solo2_login(self, username: object, password: object) -> dict[str, object]:
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("请输入 SOLO2 账号和密码")
        origin, _attempts = self._solo2_settings()
        client = Solo2Client(self.project_root / ".local-auth" / "solo2.cookies", origin=origin)
        user = client.login(username, password)
        return {"ok": True, "message": "SOLO2 登录成功", "user": {
            "username": user.get("username") or user.get("name") or username,
        }}

    def solo2_status(self, batch: object | None = None) -> dict[str, object]:
        batch_name = str(batch or "").strip() or None
        if batch_name and not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        origin, _attempts = self._solo2_settings()
        authenticated = False
        user: dict[str, object] = {}
        error = ""
        try:
            value = Solo2Client(
                self.project_root / ".local-auth" / "solo2.cookies", origin=origin,
            ).me()
            authenticated = True
            user = {"username": value.get("username") or value.get("name") or "已登录账号"}
        except Solo2Error as exc:
            error = str(exc)
        return {
            "origin": origin, "authenticated": authenticated, "user": user,
            "auth_error": error, **submission_overview(self.database, batch_name),
        }

    def solo2_submit(
        self, batch: object, numbers: object | None, record_ids: object | None = None,
    ) -> dict[str, object]:
        batch_name = str(batch or "")
        selected: set[int] | None = None
        selected_records: set[str] | None = None
        if record_ids is not None:
            if not isinstance(record_ids, list) or not record_ids or len(record_ids) > 100:
                raise ValueError("交付记录选择无效")
            selected_records = set()
            for value in record_ids:
                record_id = str(value or "").strip()
                if not record_id or len(record_id) > 128:
                    raise ValueError("交付记录编号无效")
                selected_records.add(record_id)
            if not BATCH_RE.fullmatch(batch_name):
                raise ValueError("批次名无效")
            with closing(self.connect()) as connection:
                marks = ",".join("?" for _ in selected_records)
                found = connection.execute(
                    "SELECT r.record_id FROM records r JOIN questions q ON q.id=r.question_id "
                    "JOIN batches b ON b.id=q.batch_id "
                    f"WHERE b.name=? AND r.record_id IN ({marks})",
                    [batch_name, *sorted(selected_records)],
                ).fetchall()
            if {str(row["record_id"]) for row in found} != selected_records:
                raise ValueError("选择中包含不属于当前批次的交付记录")
        if numbers:
            if selected_records:
                raise ValueError("题号和交付记录不能同时选择")
            batch_name, normalized = self.validate_selection(batch_name, numbers)
            selected = set(normalized)
        elif not BATCH_RE.fullmatch(batch_name):
            raise ValueError("批次名无效")
        origin, attempts = self._solo2_settings()
        result = submit_records(
            self.database, self.project_root / ".local-auth" / "solo2.cookies", origin,
            batch=batch_name, numbers=selected, record_ids=selected_records,
            max_attempts=attempts,
        )
        result["ok"] = result["failed"] == 0
        result["message"] = f"SOLO2 提交完成：成功 {result['submitted']}，失败 {result['failed']}"
        if result["failed"]:
            details = "；".join(
                str(item.get("error") or "提交失败") for item in result["results"]
                if not item.get("ok")
            )
            raise RuntimeError(result["message"] + (f"。{details}" if details else ""))
        return result

    @staticmethod
    def _question_id(value: object) -> int:
        raw = str(value or "")
        if not QUESTION_ID_RE.fullmatch(raw):
            raise ValueError("题目编号无效")
        return int(raw)

    def takeover_question(self, question_id: object) -> dict[str, object]:
        values = self.read_env()
        try:
            cpus = float(values.get("CC_CLAUDE_WORKER_CPUS", "1") or "1")
        except ValueError:
            cpus = 1.0
        result = begin_takeover(
            self.database, self._question_id(question_id), self.env_file,
            values.get("CC_CLAUDE_DOCKER_IMAGE", "").strip() or "claude-cli:latest",
            cpus, values.get("CC_CLAUDE_WORKER_MEMORY", "").strip() or "2g",
        )
        return {"ok": True, "message": "已进入人工接管，命令已生成", **result}

    def finish_question_takeover(self, question_id: object) -> dict[str, object]:
        result = finish_takeover(self.database, self._question_id(question_id))
        return {"ok": True, "message": "有效轨迹门禁通过，题目已恢复自动流水线", **result}

    def reset_question(self, question_id: object, reason: object = "") -> dict[str, object]:
        result = reset_question(
            self.database, self._question_id(question_id), str(reason or "人工完整重置"),
        )
        return {"ok": True, "message": "题目已恢复到初始快照并清除本地运行数据", **result}


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "CCUSRConsole/1.0"

    @property
    def data(self) -> ConsoleData:
        return self.server.data  # type: ignore[attr-defined]

    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK, *, cookie: str = "") -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
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

    def send_scheduler_stream(self, query: dict[str, list[str]]) -> None:
        try:
            after_id = max(0, int(query.get("after", ["0"])[0]))
        except ValueError:
            after_id = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                events = self.data.scheduler_store.events(after_id=after_id, limit=100)
                for event in events:
                    after_id = int(event["id"])
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"id: {after_id}\nevent: scheduler\ndata: {payload}\n\n".encode("utf-8"))
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            return

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
            if parsed.path == "/api/solo2/status":
                batch = parse_qs(parsed.query).get("batch", [None])[0]
                self.send_json(self.data.solo2_status(batch))
                return
            if parsed.path == "/api/reviews":
                batch = parse_qs(parsed.query).get("batch", [None])[0]
                self.send_json(self.data.delivery_reviews(batch))
                return
            if parsed.path == "/api/reviews/history":
                self.send_json(approved_history(self.data.database))
                return
            if parsed.path == "/api/environment":
                self.send_json(self.data.environment_status())
                return
            if parsed.path == "/api/capacity-recommendation":
                self.send_json(self.data.concurrency_recommendation())
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
            if parsed.path == "/api/scheduler":
                self.send_json(self.data.scheduler_snapshot())
                return
            if parsed.path == "/api/scheduler/events":
                self.send_json(self.data.scheduler_events(parse_qs(parsed.query)))
                return
            if parsed.path == "/api/scheduler/logs":
                self.send_json(self.data.scheduler_raw_logs(parse_qs(parsed.query)))
                return
            if parsed.path == "/api/scheduler/events/stream":
                self.send_scheduler_stream(parse_qs(parsed.query))
                return
            match = re.fullmatch(r"/api/scheduler/runs/(\d+)/output", parsed.path)
            if match:
                self.send_json(self.data.scheduler_run_output(int(match.group(1)), parse_qs(parsed.query)))
                return
            if parsed.path == "/api/vps-nodes":
                self.send_json({"nodes": self.data.vps_nodes()})
                return
            match = re.fullmatch(r"/api/vps-nodes/(\d+)/dashboard", parsed.path)
            if match:
                self.send_json(self.data.vps_dashboard(int(match.group(1))))
                return
            match = re.fullmatch(r"/api/vps-nodes/(\d+)/scheduler", parsed.path)
            if match:
                self.send_json(self.data.vps_scheduler(int(match.group(1))))
                return
            match = re.fullmatch(r"/api/vps-nodes/(\d+)/scheduler/events", parsed.path)
            if match:
                self.send_json(self.data.vps_scheduler_events(int(match.group(1)), parsed.query))
                return
            match = re.fullmatch(r"/api/vps-nodes/(\d+)/scheduler/logs", parsed.path)
            if match:
                self.send_json(self.data.vps_scheduler_logs(int(match.group(1)), parsed.query))
                return
            match = re.fullmatch(r"/api/vps-nodes/(\d+)/scheduler/runs/(\d+)/output", parsed.path)
            if match:
                self.send_json(self.data.vps_scheduler_run_output(int(match.group(1)), int(match.group(2)), parsed.query))
                return
            match = re.fullmatch(r"/api/vps-nodes/(\d+)/delivery-package", parsed.path)
            if match:
                batch = parse_qs(parsed.query).get("batch", [None])[0]
                filename, payload = self.data.vps_delivery_package(int(match.group(1)), batch)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/api/delivery-package":
                batch = parse_qs(parsed.query).get("batch", [None])[0]
                filename, payload = self.data.delivery_package(batch)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"',
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
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
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        if not self.post_allowed():
            self.send_error_json("拒绝跨站请求", HTTPStatus.FORBIDDEN)
            return
        try:
            body = self.read_json()
            if self.path == "/api/vps-nodes":
                result = self.data.save_vps_node(body)
            elif self.path == "/api/vps-nodes/delete":
                self.data.delete_vps_node(body.get("id"))
                result = {"ok": True, "message": "VPS 节点已删除"}
            elif self.path == "/api/scheduler/control":
                result = self.data.scheduler_control(body.get("action"))
            elif re.fullmatch(r"/api/vps-nodes/\d+/scheduler/control", self.path):
                node_id = int(self.path.split("/")[3])
                result = self.data.vps_scheduler_control(node_id, body.get("action"))
            elif self.path == "/api/actions/open":
                target = self.data.resolve_open_target(str(body.get("kind", "")), body.get("id"))
                open_local_path(target, self.data.project_root)
                result = {"ok": True, "message": f"已打开 {target.name}"}
            elif self.path == "/api/actions/launch":
                result = self.data.launch(body.get("batch"), body.get("numbers"))
            elif self.path == "/api/actions/qc-check":
                result = self.data.qc_check(body.get("batch"))
            elif self.path == "/api/actions/export":
                result = self.data.export(body.get("batch"), body.get("numbers"))
            elif self.path == "/api/solo2/login":
                result = self.data.solo2_login(body.get("username"), body.get("password"))
            elif self.path == "/api/actions/solo2-submit":
                result = self.data.solo2_submit(
                    body.get("batch"), body.get("numbers"), body.get("record_ids")
                )
            elif self.path == "/api/reviews/approve":
                result = self.data.approve_delivery_review(body)
            elif self.path == "/api/reviews/reject":
                result = self.data.reject_delivery_review(body)
            elif self.path == "/api/reviews/codex":
                result = self.data.create_codex_delivery_review(body)
            elif self.path == "/api/reviews/history/import":
                result = import_history(
                    self.data.database,
                    str(body.get("source_node") or ""),
                    body.get("descriptions"),
                )
            elif self.path == "/api/reviews/history/sync":
                result = self.data.sync_description_history()
            elif self.path == "/api/actions/takeover-question":
                result = self.data.takeover_question(body.get("question_id"))
            elif self.path == "/api/actions/finish-takeover":
                result = self.data.finish_question_takeover(body.get("question_id"))
            elif self.path == "/api/actions/reset-question":
                result = self.data.reset_question(body.get("question_id"), body.get("reason"))
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
    parser.add_argument(
        "--allow-public", action="store_true",
        help="明确允许绑定非回环地址；仅用于已配置防火墙白名单的 VPS",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_public:
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
