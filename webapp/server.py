#!/usr/bin/env python3
"""Serve the local CC USR production console without external dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import webbrowser
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
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_KEYS = ("CC_SWITCH_BASE_URL", "CC_SWITCH_MODEL", "CC_SWITCH_API_KEY", "CC_USR_SUBMITTER")


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


class ConsoleData:
    def __init__(self, database: Path, project_root: Path = PROJECT_ROOT) -> None:
        self.database = database.resolve()
        self.project_root = project_root.resolve()
        self.env_file = self.project_root / ".env"

    @staticmethod
    def _env_value(raw: str) -> str:
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            return value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        if len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1]
        return value.split(" #", 1)[0].rstrip()

    def read_env(self) -> dict[str, str]:
        values = {key: "" for key in ENV_KEYS}
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
        return {
            "base_url": values["CC_SWITCH_BASE_URL"],
            "model": values["CC_SWITCH_MODEL"],
            "submitter": values["CC_USR_SUBMITTER"],
            "api_key_configured": bool(key),
            "api_key_hint": f"已配置（末尾 {key[-4:]}）" if len(key) >= 4 else ("已配置" if key else "未配置"),
        }

    @staticmethod
    def _validate_env_input(values: dict[str, object]) -> dict[str, str]:
        result = {}
        for field in ("base_url", "model", "api_key", "submitter"):
            value = values.get(field, "")
            if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError(f"{field} 配置无效")
            result[field] = value.strip()
        if not result["base_url"] or not result["model"] or not result["submitter"]:
            raise ValueError("中转 URL、模型名和提交人不能为空")
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
        }
        values = self._validate_env_input(incoming)
        if not values["api_key"]:
            values["api_key"] = current["CC_SWITCH_API_KEY"]
        if not values["api_key"]:
            raise ValueError("API Key 尚未配置")
        output_values = {
            "CC_SWITCH_BASE_URL": values["base_url"],
            "CC_SWITCH_MODEL": values["model"],
            "CC_SWITCH_API_KEY": values["api_key"],
            "CC_USR_SUBMITTER": values["submitter"],
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
        for key in ENV_KEYS:
            if key not in replaced:
                output.append(f'{key}={json.dumps(output_values[key], ensure_ascii=False)}')
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".env.", dir=self.env_file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(output).rstrip() + "\n")
            os.chmod(temp_name, 0o600)
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
                "folder_path": row["folder_path"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

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
        with self.connect() as connection:
            batches = self.batches(connection)
            if not batches:
                return {"batches": [], "batch": None, "questions": [], "stages": []}
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
                run_status = str(row["latest_run_status"] or "")
                run_complete = run_status == "succeeded" or (
                    not run_status and row["status"] == "completed"
                )
                record_count = int(row["record_count"] or 0)
                passed_count = int(row["passed_record_count"] or 0)
                delivery_qc = record_count > 0 and passed_count == record_count
                exported = delivery_qc and int(row["question_no"]) in exported_numbers
                if not question_qc:
                    stage_index, stage_label = 1, "题目待质检"
                elif not run_complete:
                    if run_status == "running":
                        stage_index, stage_label = 2, "模型跑题中"
                    elif run_status in {"failed", "timeout"}:
                        stage_index, stage_label = 2, "模型跑题待重试"
                    else:
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
                    "repo_url": row["repo_url"],
                    "folder_name": row["folder_name"],
                    "task_type": row["task_type"],
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
                    "average_score": round(float(row["average_score"]), 1)
                    if row["average_score"] is not None else None,
                    "exported": exported,
                    "stage_index": stage_index,
                    "stage_label": stage_label,
                    "can_launch": question_qc and row["status"] == "approved" and not run_complete,
                })

        total = len(questions)
        stages = [
            {"id": 1, "label": "题目质检", "complete": sum(q["question_qc"] for q in questions)},
            {"id": 2, "label": "模型跑题", "complete": sum(q["run_status"] == "succeeded" for q in questions)},
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
        with self.connect() as connection:
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
        with self.connect() as connection:
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
        with self.connect() as connection:
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
        script = self.project_root / ".agents/skills/cc-usr-excel-exporter/scripts/export_xlsx.py"
        command = [
            sys.executable, str(script), "--db", str(self.database), "--batch", batch_name
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
                if sys.platform != "darwin":
                    raise RuntimeError("打开目录功能当前仅支持 macOS")
                subprocess.Popen(["open", str(target)], cwd=self.data.project_root)
                result = {"ok": True, "message": f"已打开 {target.name}"}
            elif self.path == "/api/actions/launch":
                result = self.data.launch(body.get("batch"), body.get("numbers"))
            elif self.path == "/api/actions/qc-check":
                result = self.data.qc_check(body.get("batch"))
            elif self.path == "/api/actions/export":
                result = self.data.export(body.get("batch"), body.get("numbers"))
            elif self.path == "/api/config":
                result = self.data.update_env(body)
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
