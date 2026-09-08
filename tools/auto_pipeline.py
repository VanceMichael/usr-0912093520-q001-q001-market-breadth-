#!/usr/bin/env python3
"""Run the complete question -> Claude Docker -> delivery pipeline.

This orchestrator keeps the existing manual skills intact. It only coordinates
their documented commands and stores progress outside the question workspaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from queue import Empty, Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime
from pathlib import Path

OUTPUT_LIMIT = 200_000
MODEL_HEALTH_INTERVAL_SECONDS = 15
MODEL_IDLE_WARNING_SECONDS = 10 * 60
MODEL_STALL_TIMEOUT_SECONDS = 30 * 60
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import SNAPSHOT_RE, connect, prompt_hash, question_rows  # noqa: E402
from tools.runtime_environment import docker_info, repair_docker_engine  # noqa: E402


class PipelineInterrupted(RuntimeError):
    """Raised when the console has marked this persisted job as interrupted."""


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f".env 第 {line_number} 行格式无效")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        values[key.strip()] = value
    return values


def version_text(value: str) -> str:
    match = re.search(r"\d+(?:\.\d+)+", value)
    return match.group(0) if match else "0.0.0"


def build_docker_claude_command(
    docker: str, image: str, claude_command: str, workspace: Path,
    trajectory_root: Path, container_name: str, pipeline_job_id: int, prompt: str,
) -> list[str]:
    """Build argv where the only Claude user content is the stored prompt."""
    return [
        docker, "run", "--rm", "--name", container_name,
        "--label", f"ccusr.pipeline_job={pipeline_job_id}",
        "--user", "1000:1000",
        "-v", f"{workspace}:/workspace",
        "-v", f"{trajectory_root}:/home/node/.claude",
        "-w", "/workspace",
        "-e", "HOME=/home/node",
        "-e", "ANTHROPIC_BASE_URL", "-e", "ANTHROPIC_AUTH_TOKEN", "-e", "ANTHROPIC_MODEL",
        image, claude_command, "--dangerously-skip-permissions", prompt,
    ]


def build_local_claude_command(claude: str, prompt: str) -> list[str]:
    return [
        claude, "--print", "--dangerously-skip-permissions",
        "--permission-mode", "bypassPermissions", "--permission-prompts", "none", prompt,
    ]


class Pipeline:
    def __init__(self, database: Path, batch: str, job_id: int, emit) -> None:
        self.database = database.resolve()
        self.batch = batch
        self.job_id = job_id
        self.emit = emit
        self.project_root = PROJECT_ROOT
        self.pipeline_root: Path | None = None
        self.trajectory_search_root: Path | None = None
        self.redacted_values: set[str] = set()

    def db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def log(self, message: str) -> None:
        self.ensure_active()
        message = str(message).strip()
        for value in self.redacted_values:
            if value:
                message = message.replace(value, "[REDACTED]")
        message = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", message)
        message = re.sub(r"(?i)(?:api[_ -]?key|auth[_ -]?token)\s*[:=]\s*\S+", "[REDACTED]", message)
        if not message:
            return
        self.emit(message)
        with closing(self.db()) as connection:
            connection.execute(
                "UPDATE pipeline_jobs SET output=substr(output || ?, -?), last_message=? WHERE id=?",
                (message + "\n", OUTPUT_LIMIT, message[:1000], self.job_id),
            )
            connection.commit()

    def ensure_active(self) -> None:
        with closing(self.db()) as connection:
            row = connection.execute(
                "SELECT status FROM pipeline_jobs WHERE id=?", (self.job_id,)
            ).fetchone()
        if row is None or row["status"] not in {"queued", "running"}:
            status = "missing" if row is None else row["status"]
            raise PipelineInterrupted(f"流水线任务已停止（状态：{status}）")

    def set_job(self, **values: object) -> None:
        if not values:
            return
        assignments = ", ".join(f"{key}=?" for key in values)
        with closing(self.db()) as connection:
            connection.execute(
                f"UPDATE pipeline_jobs SET {assignments} "
                "WHERE id=? AND status IN ('queued','running')",
                [*values.values(), self.job_id],
            )
            connection.commit()

    def item(self, question_id: int, **values: object) -> None:
        if not values:
            return
        self.ensure_active()
        assignments = ", ".join(f"{key}=?" for key in values)
        with closing(self.db()) as connection:
            connection.execute(
                f"UPDATE pipeline_items SET {assignments} "
                "WHERE pipeline_job_id=? AND question_id=? "
                "AND EXISTS(SELECT 1 FROM pipeline_jobs j WHERE j.id=pipeline_items.pipeline_job_id "
                "AND j.status IN ('queued','running'))",
                [*values.values(), self.job_id, question_id],
            )
            connection.commit()

    def run_command(
        self, command: list[str], *, cwd: Path | None = None,
        timeout: int = 7200, stdin_text: str | None = None,
    ) -> tuple[int, str]:
        self.log("$ " + " ".join(self._safe_arg(value) for value in command[:5]) + (" ..." if len(command) > 5 else ""))
        process = subprocess.Popen(
            command, cwd=cwd or self.project_root, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if stdin_text is not None:
            assert process.stdin is not None
            process.stdin.write(stdin_text)
            process.stdin.close()
        assert process.stdout is not None
        chunks: list[str] = []
        for line in process.stdout:
            clean = line.rstrip()
            if clean:
                chunks.append(clean)
                self.log(self.codex_event(clean))
        returncode = process.wait(timeout=timeout)
        return returncode, "\n".join(chunks)[-OUTPUT_LIMIT:]

    @staticmethod
    def _safe_arg(value: str) -> str:
        return "[prompt]" if len(value) > 200 else value

    @staticmethod
    def codex_event(line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return json.dumps(event, ensure_ascii=False)
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        text = item.get("text") or item.get("command") or event.get("message") or event.get("error")
        if isinstance(text, (dict, list)):
            text = json.dumps(text, ensure_ascii=False)
        return f"[{event.get('type', 'event')}] {text}" if text else f"[{event.get('type', 'event')}]"

    def codex(self, prompt: str, timeout: int = 7200) -> int:
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("未找到 Codex CLI")
        command = [
            binary, "exec", "--json", "--cd", str(self.project_root),
            "--sandbox", "danger-full-access", "--skip-git-repo-check", "-",
        ]
        return self.run_command(command, timeout=timeout, stdin_text=prompt)[0]

    def question_qc(self, concurrency: int, rows: list[sqlite3.Row]) -> None:
        self.log("阶段 1/4：先执行快照与 Prompt 机械门禁")
        for row in rows:
            self.item(int(row["id"]), status="qc_running", started_at=timestamp(), error="")
        mechanical_command = [
            sys.executable, str(self.project_root / "tools" / "batch_pipeline.py"),
            "--db", str(self.database), "qc-check", "--batch", self.batch,
            "--select", ",".join(str(row["question_no"]) for row in rows),
        ]
        returncode, _output = self.run_command(mechanical_command)
        with closing(self.db()) as connection:
            checked_rows = [
                row for row in question_rows(connection, self.batch)
                if int(row["id"]) in {int(selected["id"]) for selected in rows}
            ]
        rejected = [row for row in checked_rows if row["mechanical_qc"] != "pass"]
        if rejected or returncode:
            for row in checked_rows:
                if row["mechanical_qc"] != "pass":
                    self.item(
                        int(row["id"]), status="failed", error="机械质检未通过",
                        finished_at=timestamp(),
                    )
            raise RuntimeError(
                "题目机械质检未全部通过：" + ", ".join(row["task_id"] for row in rejected)
            )
        self.log(f"Codex CLI 并行进行重复性与自然度语义质检，并发数 {concurrency}")
        def check(row: sqlite3.Row) -> int:
            question_id = int(row["id"])
            prompt = (
                f"使用 cc-usr-question-qc 处理批次 {self.batch} 第 {row['question_no']} 题。先完整读取项目规范.md、"
                ".agents/skills/cc-usr-question-qc/SKILL.md、.agents/skills/cc-usr-question-qc/references/qc-rubric.md 和 "
                ".agents/skills/cc-usr-question-author/references/content-quality.md。"
                f"执行 tools/batch_pipeline.py duplicate-check --batch {self.batch} --select {row['question_no']}；"
                "只检查 SQLite 中的题目 Prompt 和元数据，不得读取题目仓库、目标模型、轨迹或产物。"
                "除重复与换名套模板外，还要判断文字是否像真实中文书面需求，是否存在固定开头、标签堆砌、"
                "统一句式、评测语境或只替换业务名词的痕迹。依据实际比较结果执行 qc-set："
                "无重复、表达自然且机械质检通过才写入精确的‘质检通过’，"
                "发现重复必须记录具体任务编号和证据并保持阻塞状态。不得启动任何目标模型。"
                "过程说明和结论使用自然中文，每项判断都引用实际比较结果，不写空泛套话。"
            )
            returncode = self.codex(prompt)
            if returncode:
                error = f"题目质检 Codex CLI 退出码：{returncode}"
                self.item(question_id, status="failed", error=error, finished_at=timestamp())
                return returncode
            self.item(question_id, status="qc_passed")
            return 0
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="codex-question-qc") as pool:
            futures = {pool.submit(check, row): row for row in rows}
            errors = []
            for future in as_completed(futures):
                row = futures[future]
                try:
                    returncode = future.result()
                except PipelineInterrupted:
                    raise
                except Exception as exc:
                    error = f"题目质检异常：{exc}"
                    self.item(
                        int(row["id"]), status="failed", error=error,
                        finished_at=timestamp(),
                    )
                    errors.append(f"第 {row['question_no']} 题：{exc}")
                    continue
                if returncode:
                    errors.append(f"第 {row['question_no']} 题退出码 {returncode}")
        if errors:
            raise RuntimeError("Codex 题目质检失败：" + "; ".join(errors))
        with closing(self.db()) as connection:
            selected_ids = {int(selected["id"]) for selected in rows}
            rows = [
                row for row in question_rows(connection, self.batch)
                if int(row["id"]) in selected_ids
            ]
        blocked = [row["task_id"] for row in rows if not (
            row["mechanical_qc"] == "pass" and row["qc_decision"] == "pass"
            and row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        )]
        if blocked:
            with closing(self.db()) as connection:
                blocked_ids = [
                    int(row["id"]) for row in rows if row["task_id"] in blocked
                ]
            for question_id in blocked_ids:
                self.item(
                    question_id, status="failed", error="题目质检未通过",
                    finished_at=timestamp(),
                )
            raise RuntimeError("题目质检未全部通过：" + ", ".join(blocked))

    def runtime_config(self) -> dict[str, str]:
        env_path = self.project_root / ".env"
        if not env_path.is_file():
            raise RuntimeError("根目录缺少 .env，请先在运行配置中填写模型、中转地址、Key 和提交人")
        config = parse_env(env_path)
        required = {
            "CC_SWITCH_BASE_URL": "中转 URL",
            "CC_SWITCH_MODEL": "模型名称",
            "CC_SWITCH_API_KEY": "API Key",
            "CC_USR_SUBMITTER": "提交人",
        }
        missing = [label for key, label in required.items() if not config.get(key, "").strip()]
        if missing:
            raise RuntimeError(".env 缺少必填配置：" + "、".join(missing))
        self.redacted_values.update(
            config[key] for key in (
                "CC_SWITCH_BASE_URL", "CC_SWITCH_MODEL", "CC_SWITCH_API_KEY"
            )
        )
        return config

    def codex_preflight(self) -> str:
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("环境不可用：未找到 Codex CLI，请安装后加入 PATH")
        result = subprocess.run(
            [binary, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("环境不可用：Codex CLI 无法运行：" + result.stdout.strip()[-400:])
        version = version_text(result.stdout)
        if version == "0.0.0":
            raise RuntimeError("环境不可用：Codex CLI 未返回有效版本")
        self.log(f"环境检测通过：Codex CLI {version}")
        return version

    def docker_preflight(self, image: str, command: str) -> str:
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("未找到 Docker CLI")
        result = docker_info(docker)
        if result.returncode:
            self.log("环境检测：Docker 引擎不可用，准备启动 Docker Desktop")
            repaired, detail = repair_docker_engine(docker)
            self.log("环境修复：" + detail)
            if not repaired:
                raise RuntimeError("Docker 引擎未运行：" + result.stdout.strip()[-500:])
        inspect = subprocess.run([docker, "image", "inspect", image], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        rebuild = inspect.returncode != 0
        if inspect.returncode == 0:
            try:
                image_config = json.loads(inspect.stdout)[0].get("Config", {})
                image_user = str(image_config.get("User") or "").strip().lower()
                labels = image_config.get("Labels") or {}
                rebuild = image == "claude-cli:latest" and (
                    image_user in {"", "root", "0", "0:0"}
                    or labels.get("ccusr.claude.nonroot") != "true"
                )
                if rebuild:
                    self.log("环境检测：默认 Claude 镜像仍以 root 运行，准备重建非 root 镜像")
            except (ValueError, TypeError, IndexError):
                rebuild = image == "claude-cli:latest"
        if rebuild:
            dockerfile = self.project_root / "docker" / "claude-cli" / "Dockerfile"
            if image != "claude-cli:latest" or not dockerfile.is_file():
                raise RuntimeError(f"Docker 镜像不存在：{image}")
            self.log("环境修复：开始构建非 root Claude CLI 镜像")
            build = subprocess.run(
                [docker, "build", "-t", image, str(dockerfile.parent)],
                cwd=self.project_root, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False,
            )
            if build.returncode:
                raise RuntimeError("Claude CLI 镜像构建失败：" + build.stdout.strip()[-1000:])
        user_probe = subprocess.run(
            [docker, "run", "--rm", "--user", "1000:1000", image, "id", "-u"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if user_probe.returncode or user_probe.stdout.strip() == "0":
            raise RuntimeError("Docker 中 Claude 环境仍以 root 运行：" + user_probe.stdout.strip()[-500:])
        result = subprocess.run(
            [docker, "run", "--rm", "--user", "1000:1000", "-e", "HOME=/home/node", image, command, "--version"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if result.returncode:
            raise RuntimeError("Docker 中无法运行 claude-cli：" + result.stdout.strip()[-500:])
        self.log("环境检测通过：Docker 引擎、非 root Claude 用户和 Claude CLI 均可用")
        return version_text(result.stdout)

    def local_preflight(self, command: str) -> str:
        claude = shutil.which(command) or (command if Path(command).is_file() else None)
        if not claude:
            raise RuntimeError("未找到本地 Claude CLI")
        result = subprocess.run(
            [claude, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=False, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("本地 Claude CLI 无法运行：" + result.stdout.strip()[-500:])
        version = version_text(result.stdout)
        if version == "0.0.0":
            raise RuntimeError("本地 Claude CLI 未返回有效版本")
        self.log(f"环境检测通过：本地 Claude CLI {version}")
        return version

    def run_model(
        self, row: sqlite3.Row, image: str, command: str, model_version: str,
        config: dict[str, str], model_mode: str = "local",
    ) -> None:
        assert self.pipeline_root is not None
        question_id = int(row["id"])
        folder = Path(row["folder_path"]).resolve(strict=True)
        trajectory_root = self.pipeline_root / "claude-home"
        trajectory_root.mkdir(parents=True, exist_ok=True)
        batch_run_id = self.pipeline_root.name
        launch_time = timestamp()
        with closing(self.db()) as connection:
            connection.execute(
                "INSERT INTO runs(question_id,batch_run_id,launched_at,codex_version,model,harness,harness_version,container_cwd,trajectory_root,operating_system) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (question_id, batch_run_id, launch_time, "", config["CC_SWITCH_MODEL"], "Claude Code", model_version, "/workspace", str(trajectory_root), "MacOS/Linux"),
            )
            connection.execute("UPDATE questions SET status='running', updated_at=? WHERE id=?", (launch_time, question_id))
            connection.commit()
        self.item(
            question_id, status="model_running", started_at=launch_time,
            heartbeat_at=launch_time, activity_at=launch_time,
            health_status="starting", health_detail="正在启动本地 Claude CLI" if model_mode == "local" else "正在启动 Claude 容器",
        )
        if model_mode == "local":
            return self.run_local_model(row, command, trajectory_root, launch_time, config)
        docker = shutil.which("docker") or "docker"
        host_workspace = str(folder)
        host_home = str(trajectory_root)
        container_name = f"ccusr-{self.job_id}-{row['question_no']}"
        command_line = build_docker_claude_command(
            docker, image, command, Path(host_workspace), Path(host_home),
            container_name, self.job_id, row["prompt"],
        )
        env = os.environ.copy()
        env.update({"ANTHROPIC_BASE_URL": config["CC_SWITCH_BASE_URL"], "ANTHROPIC_AUTH_TOKEN": config["CC_SWITCH_API_KEY"], "ANTHROPIC_MODEL": config["CC_SWITCH_MODEL"]})
        self.log(f"题目 {row['task_id']}：启动 Docker Claude CLI（仅传 SQLite 原始 Prompt）")
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        process = subprocess.Popen(command_line, cwd=self.project_root, env=env, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert process.stdout is not None
        lines: Queue[str | None] = Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for value in process.stdout:
                lines.put(value.rstrip())
            lines.put(None)

        reader = threading.Thread(
            target=read_output, name=f"claude-output-{question_id}", daemon=True,
        )
        reader.start()
        output = ""
        last_activity = launch_time
        stalled = False
        stalled_detail = ""
        while True:
            try:
                line = lines.get(timeout=MODEL_HEALTH_INTERVAL_SECONDS)
            except Empty:
                health_status, health_detail, activity_at = self.docker_run_health(
                    docker, container_name, trajectory_root, last_activity,
                )
                last_activity = activity_at
                self.item(
                    question_id, heartbeat_at=timestamp(), activity_at=activity_at,
                    health_status=health_status, health_detail=health_detail,
                )
                if health_status == "stalled":
                    stalled = True
                    stalled_detail = health_detail
                    self.log(f"[{row['task_id']}] {health_detail}，停止容器并保留现有轨迹")
                    subprocess.run(
                        [docker, "rm", "-f", container_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False, timeout=30,
                    )
                continue
            if line is None:
                break
            if line:
                now = timestamp()
                last_activity = now
                output = (output + line + "\n")[-OUTPUT_LIMIT:]
                self.item(
                    question_id, output=output.rstrip(), heartbeat_at=now,
                    activity_at=now, health_status="healthy",
                    health_detail="Claude 容器运行正常，刚刚产生新输出",
                )
                self.log(f"[{row['task_id']}] {line}")
        returncode = process.wait(timeout=86400)
        finished = timestamp()
        if returncode:
            error = stalled_detail or f"Docker Claude CLI 退出码：{returncode}"
            self.item(
                question_id, status="failed", error=error,
                heartbeat_at=finished, health_status="stalled" if stalled else "failed",
                health_detail=error if stalled else f"Claude 容器已异常退出，退出码 {returncode}",
                finished_at=finished,
            )
            raise RuntimeError(f"{row['task_id']} 模型运行失败：{error}")
        with closing(self.db()) as connection:
            connection.execute("UPDATE questions SET status='completed', updated_at=? WHERE id=?", (finished, question_id))
            connection.commit()
        self.item(
            question_id, status="model_completed", heartbeat_at=finished,
            activity_at=finished, health_status="completed",
            health_detail="Claude 容器已正常完成", finished_at=finished,
        )

    def run_local_model(
        self, row: sqlite3.Row, command: str, trajectory_root: Path,
        launch_time: str, config: dict[str, str],
    ) -> None:
        question_id = int(row["id"])
        claude = shutil.which(command) or command
        command_line = build_local_claude_command(claude, row["prompt"])
        env = os.environ.copy()
        env.update({
            "ANTHROPIC_BASE_URL": config["CC_SWITCH_BASE_URL"],
            "ANTHROPIC_AUTH_TOKEN": config["CC_SWITCH_API_KEY"],
            "ANTHROPIC_MODEL": config["CC_SWITCH_MODEL"],
            "CLAUDE_CONFIG_DIR": str(trajectory_root),
            "CI": "1",
        })
        self.log(f"题目 {row['task_id']}：启动本地 Claude CLI（仅传 SQLite 原始 Prompt）")
        process = subprocess.Popen(
            command_line, cwd=Path(row["folder_path"]), env=env, text=True,
            encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        output = ""
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            output = (output + line + "\n")[-OUTPUT_LIMIT:]
            now = timestamp()
            self.item(
                question_id, output=output.rstrip(), heartbeat_at=now,
                activity_at=now, health_status="healthy",
                health_detail="本地 Claude CLI 运行正常，刚刚产生新输出",
            )
            self.log(f"[{row['task_id']}] {line}")
        returncode = process.wait(timeout=86400)
        finished = timestamp()
        if returncode:
            error = f"本地 Claude CLI 退出码：{returncode}"
            self.item(question_id, status="failed", error=error, finished_at=finished, health_status="failed", health_detail=error)
            raise RuntimeError(f"{row['task_id']} 模型运行失败：{error}")
        with closing(self.db()) as connection:
            connection.execute("UPDATE questions SET status='completed', updated_at=? WHERE id=?", (finished, question_id))
            connection.commit()
        self.item(question_id, status="model_completed", heartbeat_at=finished, activity_at=finished, health_status="completed", health_detail="本地 Claude CLI 已正常完成", finished_at=finished)

    @staticmethod
    def docker_run_health(
        docker: str, container_name: str, trajectory_root: Path, fallback_activity: str,
    ) -> tuple[str, str, str]:
        heartbeat = datetime.now().astimezone()
        activity = fallback_activity
        try:
            latest_mtime = datetime.fromisoformat(fallback_activity).timestamp()
        except ValueError:
            latest_mtime = 0.0
        try:
            for path in trajectory_root.rglob("*.jsonl"):
                latest_mtime = max(latest_mtime, path.stat().st_mtime)
        except OSError:
            pass
        if latest_mtime:
            activity = datetime.fromtimestamp(
                latest_mtime, heartbeat.tzinfo,
            ).isoformat(timespec="seconds")
        try:
            state = subprocess.run(
                [docker, "inspect", "--format", "{{.State.Running}}|{{.State.Status}}", container_name],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "unavailable", f"无法确认容器状态：{exc}", activity
        if state.returncode or not state.stdout.strip().lower().startswith("true|running"):
            detail = state.stdout.strip()[-300:] or "容器不存在或已经停止"
            return "unavailable", detail, activity
        try:
            activity_time = datetime.fromisoformat(activity)
            idle_seconds = max(0, int((heartbeat - activity_time).total_seconds()))
        except ValueError:
            idle_seconds = 0
        if idle_seconds >= MODEL_STALL_TIMEOUT_SECONDS:
            return "stalled", f"容器仍在运行，但轨迹已 {idle_seconds // 60} 分钟没有更新，判定为停滞", activity
        if idle_seconds >= MODEL_IDLE_WARNING_SECONDS:
            return "idle", f"容器仍在运行，但轨迹已 {idle_seconds // 60} 分钟没有更新", activity
        return "healthy", f"容器运行正常，轨迹在 {idle_seconds // 60} 分钟内有更新", activity

    def model_stage(
        self, image: str, command: str, concurrency: int, model_version: str,
        rows: list[sqlite3.Row], config: dict[str, str], model_mode: str = "local",
    ) -> None:
        label = "本地 Claude CLI" if model_mode == "local" else "Docker Claude CLI"
        self.log(f"阶段 2/4：{label}并行跑题，并发数 {concurrency}")
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="claude-model") as pool:
            futures = {
                pool.submit(self.run_model, row, image, command, model_version, config, model_mode): row
                for row in rows
            }
            errors = []
            for future in as_completed(futures):
                row = futures[future]
                try:
                    future.result()
                except PipelineInterrupted:
                    raise
                except Exception as exc:
                    self.item(
                        int(row["id"]), status="failed", error=str(exc)[:1000],
                        finished_at=timestamp(),
                    )
                    errors.append(str(exc))
            if errors:
                raise RuntimeError("模型跑题阶段失败：" + "; ".join(errors))

    def producer_stage(self, concurrency: int, rows: list[sqlite3.Row]) -> None:
        assert self.trajectory_search_root is not None
        self.log(f"阶段 3/4：Codex CLI 并行执行交付生产，并发数 {concurrency}")
        root = self.trajectory_search_root
        def produce(row: sqlite3.Row) -> int:
            question_id = int(row["id"])
            self.item(question_id, status="producing", error="")
            prompt = (
                f"使用 cc-usr-delivery-producer 处理批次 {self.batch} 第 {row['question_no']} 题。"
                "先读取项目规范.md、.agents/skills/cc-usr-delivery-producer/SKILL.md、"
                ".agents/skills/cc-usr-delivery-producer/references/record-contract.md 和 "
                ".agents/skills/cc-usr-delivery-producer/references/scoring-rubric.md。"
                f"执行 find_claude_turns.py --db {self.database} --batch {self.batch} --question {row['question_no']} --claude-root {root}，"
                "只依据匹配的原始 Claude JSONL、SQLite、初始快照、Git diff 和真实产物评分；"
                "为每个有效轮次生成临时 JSON 并使用 collect_record.py --from-json 写入 SQLite。"
                "27 个交付字段中的五项评分描述和非空的其他问题必须使用自然中文书面语，"
                "直接写文件、函数、命令、报错、测试结果、明确需求或原始轨迹动作等可核验证据，"
                "不得出现评价者自述、评分质检、模型表现、生成过程、固定标签、套话开头或统一句式。"
                "不得修改题目代码、轨迹或仓库，不得编造 SessionID、PromptID、评分证据。完成后不要执行交付质检或导出。"
            )
            returncode = self.codex(prompt)
            if returncode:
                error = f"交付生产 Codex CLI 退出码：{returncode}"
                self.item(question_id, status="failed", error=error, finished_at=timestamp())
                return returncode
            self.item(question_id, status="produced")
            return 0
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="codex-producer") as pool:
            futures = {pool.submit(produce, row): row for row in rows}
            errors = []
            for future in as_completed(futures):
                row = futures[future]
                try:
                    returncode = future.result()
                except PipelineInterrupted:
                    raise
                except Exception as exc:
                    error = f"交付生产异常：{exc}"
                    self.item(
                        int(row["id"]), status="failed", error=error,
                        finished_at=timestamp(),
                    )
                    errors.append(f"第 {row['question_no']} 题：{exc}")
                    continue
                if returncode:
                    errors.append(f"第 {row['question_no']} 题退出码 {returncode}")
        if errors:
            raise RuntimeError("交付生产失败：" + "; ".join(errors))
        with closing(self.db()) as connection:
            produced = {
                int(row["question_id"]): int(row["record_count"])
                for row in connection.execute(
                    "SELECT pi.question_id, COUNT(r.id) AS record_count "
                    "FROM pipeline_items pi LEFT JOIN records r ON r.question_id=pi.question_id "
                    "WHERE pi.pipeline_job_id=? GROUP BY pi.question_id",
                    (self.job_id,),
                )
            }
        missing = [
            str(row["task_id"]) for row in rows if produced.get(int(row["id"]), 0) == 0
        ]
        if missing:
            for row in rows:
                if str(row["task_id"]) in missing:
                    self.item(
                        int(row["id"]), status="failed", error="交付生产未写入记录",
                        finished_at=timestamp(),
                    )
            raise RuntimeError("交付生产未写入 SQLite 记录：" + ", ".join(missing))

    def qc_export_stage(self) -> None:
        assert self.trajectory_search_root is not None
        self.log("阶段 4/4：Codex CLI 执行交付质检并导出 Excel")
        with closing(self.db()) as connection:
            question_ids = [
                int(row["question_id"]) for row in connection.execute(
                    "SELECT question_id FROM pipeline_items WHERE pipeline_job_id=?",
                    (self.job_id,),
                )
            ]
            question_numbers = [
                int(row["question_no"]) for row in connection.execute(
                    "SELECT question_no FROM pipeline_items WHERE pipeline_job_id=? ORDER BY question_no",
                    (self.job_id,),
                )
            ]
        selection = ",".join(str(number) for number in question_numbers)
        for question_id in question_ids:
            self.item(question_id, status="finalizing")
        root = self.trajectory_search_root
        with closing(self.db()) as connection:
            batch_folder = Path(connection.execute(
                "SELECT folder_path FROM batches WHERE name=?", (self.batch,)
            ).fetchone()[0])
        existing_workbooks = {path.resolve() for path in batch_folder.glob("CC_Codex*.xlsx")}
        existing_trajectories = {path.resolve() for path in batch_folder.glob("轨迹_*.jsonl")}
        prompt = (
            f"使用 cc-usr-delivery-qc 处理批次 {self.batch} 中第 {selection} 题的交付记录。读取项目规范.md、"
            ".agents/skills/cc-usr-delivery-qc/SKILL.md、references/delivery-qc-checklist.md 以及 producer 的记录合同和评分规则，执行 validate_records.py，依据原始 JSONL、SQLite、"
            f"初始快照和实际产物修正所有有证据支持的不合规字段；本批次原始 JSONL 根目录是 {root}。"
            "重新验证无错误后执行 --finalize。"
            "逐项复核五项评分描述和非空的其他问题是否为自然中文书面语、是否各自引用可核验证据，"
            "并拒绝评价者自述、评分质检、模型表现、生成过程、固定标签、套话开头和机械重复句式。"
            "无法从证据恢复的字段必须保持未通过，不得猜测或修改目标模型产物。"
            f"全部通过后再使用 cc-usr-excel-exporter 导出批次 {self.batch} 的第 {selection} 题，"
            ".agents/skills/cc-usr-delivery-qc/scripts/validate_records.py 和 "
            ".agents/skills/cc-usr-excel-exporter/scripts/export_xlsx.py 都必须显式使用 "
            f"--select {selection}；export_xlsx.py 还必须使用 --claude-root {root}，原始 JSONL 必须逐字节复制。"
        )
        if self.codex(prompt) != 0:
            for question_id in question_ids:
                self.item(
                    question_id, status="failed", error="交付质检或 Excel 导出失败",
                    finished_at=timestamp(),
                )
            raise RuntimeError("交付质检或 Excel 导出失败")
        with closing(self.db()) as connection:
            record_state = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过' THEN 1 ELSE 0 END) AS passed "
                "FROM records r JOIN pipeline_items pi ON pi.question_id=r.question_id "
                "WHERE pi.pipeline_job_id=?", (self.job_id,),
            ).fetchone()
        total_records = int(record_state["total"] or 0)
        passed_records = int(record_state["passed"] or 0)
        new_workbooks = {
            path.resolve() for path in batch_folder.glob("CC_Codex*.xlsx")
        } - existing_workbooks
        new_trajectories = {
            path.resolve() for path in batch_folder.glob("轨迹_*.jsonl")
        } - existing_trajectories
        if not total_records or passed_records != total_records or not new_workbooks or not new_trajectories:
            error = (
                f"交付后置检查失败：记录 {passed_records}/{total_records} 通过，"
                f"新增 Excel {len(new_workbooks)} 个，新增轨迹 {len(new_trajectories)} 个"
            )
            for question_id in question_ids:
                self.item(question_id, status="failed", error=error, finished_at=timestamp())
            raise RuntimeError(error)
        finished = timestamp()
        for question_id in question_ids:
            self.item(question_id, status="completed", finished_at=finished)

    @staticmethod
    def question_qc_passed(row: sqlite3.Row) -> bool:
        return bool(
            row["mechanical_qc"] == "pass"
            and row["qc_decision"] == "pass"
            and row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        )

    def question_progress(self, rows: list[sqlite3.Row]) -> dict[int, dict[str, object]]:
        question_ids = [int(row["id"]) for row in rows]
        if not question_ids:
            return {}
        placeholders = ",".join("?" for _ in question_ids)
        with closing(self.db()) as connection:
            states = connection.execute(
                "SELECT q.id, q.status, "
                "(SELECT COUNT(*) FROM runs x WHERE x.question_id=q.id) AS run_count, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id) AS record_count, "
                "(SELECT COUNT(*) FROM records r WHERE r.question_id=q.id "
                "AND r.delivery_qc_passed=1 AND r.delivery_qc_note='质检通过') AS passed_count "
                f"FROM questions q WHERE q.id IN ({placeholders})",
                question_ids,
            ).fetchall()
        return {
            int(state["id"]): {
                "model_done": bool(
                    int(state["record_count"] or 0) > 0
                    or (state["status"] == "completed" and int(state["run_count"] or 0) > 0)
                ),
                "record_count": int(state["record_count"] or 0),
                "delivery_passed": bool(
                    int(state["record_count"] or 0) > 0
                    and int(state["record_count"] or 0) == int(state["passed_count"] or 0)
                ),
            }
            for state in states
        }

    def restore_question_workspace(self, row: sqlite3.Row | dict[str, object]) -> None:
        """Restore a retried question to its registered Git snapshot.

        A retry gets a fresh Claude session, so its workspace must also start
        from the same clean baseline.  ``git clean -ffdx`` intentionally drops
        untracked and ignored files produced by the failed model attempt.
        """
        folder = Path(str(row["folder_path"])).resolve(strict=True)
        baseline = str(row["local_initial_sha"] or "").strip()
        snapshot = str(row["initial_snapshot"] or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", baseline):
            raise RuntimeError(
                f"{row['task_id']} 缺少有效的 local_initial_sha，无法安全恢复工作区"
            )
        if (
            not SNAPSHOT_RE.fullmatch(snapshot)
            or snapshot.rsplit("/", 1)[-1].lower() != baseline.lower()
        ):
            raise RuntimeError(
                f"{row['task_id']} 的初始快照 SHA 与 local_initial_sha 不一致，拒绝恢复"
            )

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(folder), *args],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False,
            )

        root = git("rev-parse", "--show-toplevel")
        if root.returncode or Path(root.stdout.strip()).resolve() != folder:
            raise RuntimeError(f"{row['task_id']} 工作区不是独立 Git 仓库，拒绝恢复")
        exists = git("cat-file", "-e", f"{baseline}^{{commit}}")
        if exists.returncode:
            raise RuntimeError(f"{row['task_id']} 找不到初始快照提交 {baseline}")

        reset = git("reset", "--hard", baseline)
        if reset.returncode:
            raise RuntimeError(
                f"{row['task_id']} 恢复 Git 跟踪文件失败：{reset.stdout.strip()[-500:]}"
            )
        clean = git("clean", "-ffdx")
        if clean.returncode:
            raise RuntimeError(
                f"{row['task_id']} 清理工作区文件失败：{clean.stdout.strip()[-500:]}"
            )

        head = git("rev-parse", "HEAD")
        status = git("status", "--porcelain", "--untracked-files=all")
        if (
            head.returncode
            or head.stdout.strip().lower() != baseline.lower()
            or status.returncode
            or status.stdout.strip()
        ):
            raise RuntimeError(f"{row['task_id']} 恢复后工作区仍不是干净初始快照")
        self.log(f"题目 {row['task_id']}：重试前已恢复到初始快照 {baseline}")

    def execute(self, image: str, command: str, qc_concurrency: int, model_concurrency: int, codex_concurrency: int, model_mode: str = "local") -> None:
        with closing(self.db()) as connection:
            batch = connection.execute("SELECT * FROM batches WHERE name=?", (self.batch,)).fetchone()
            all_rows = question_rows(connection, self.batch)
            selected_ids = {
                int(item["question_id"])
                for item in connection.execute(
                    "SELECT question_id FROM pipeline_items WHERE pipeline_job_id=?",
                    (self.job_id,),
                )
            }
            rows = [row for row in all_rows if int(row["id"]) in selected_ids]
            job = connection.execute(
                "SELECT retry_of_job_id FROM pipeline_jobs WHERE id=?", (self.job_id,)
            ).fetchone()
        if batch is None or not rows:
            raise ValueError(f"批次不存在或没有题目：{self.batch}")
        if job is None:
            raise ValueError(f"流水线任务不存在：{self.job_id}")
        retry_of_job_id = job["retry_of_job_id"]
        with closing(self.db()) as connection:
            placeholders = ",".join("?" for _ in rows)
            existing = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE question_id IN (" + placeholders + ")",
                [int(row["id"]) for row in rows],
            ).fetchone()[0]
        if existing and retry_of_job_id is None:
            raise RuntimeError("批次已有目标模型运行记录；为保护原始工作区和轨迹，一键全流程只允许运行尚未跑题的新批次")
        runs_root = Path(batch["folder_path"]) / ".runs"
        self.pipeline_root = runs_root / f"auto-{self.job_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.pipeline_root.mkdir(parents=True, exist_ok=False)
        self.trajectory_search_root = runs_root if retry_of_job_id is not None else self.pipeline_root / "claude-home"
        config = self.runtime_config()
        self.log("阶段 0/4：检查并修复运行环境")
        self.codex_preflight()
        if model_mode not in {"local", "docker"}:
            raise ValueError("模型运行方式必须是 local 或 docker")
        model_version = self.local_preflight(command) if model_mode == "local" else self.docker_preflight(image, command)
        self.ensure_active()
        qc_pending = [row for row in rows if not self.question_qc_passed(row)]
        qc_pending_ids = {int(row["id"]) for row in qc_pending}
        if retry_of_job_id is not None:
            progress = self.question_progress(rows)
            if qc_pending:
                resume_stage = "题目质检"
            elif any(not progress[int(row["id"])]["model_done"] for row in rows):
                resume_stage = "模型跑题"
            elif any(progress[int(row["id"])]["record_count"] == 0 for row in rows):
                resume_stage = "交付生产"
            else:
                resume_stage = "交付质检与导出"
            self.log(f"重试任务：原任务 #{retry_of_job_id}，从{resume_stage}阶段继续")
        for row in rows:
            if int(row["id"]) not in qc_pending_ids:
                self.item(int(row["id"]), status="qc_passed", error="")
        if qc_pending:
            self.question_qc(qc_concurrency, qc_pending)
        else:
            self.log("阶段 1/4：题目质检已有有效通过记录，跳过")
        self.ensure_active()
        with closing(self.db()) as connection:
            rows = question_rows(connection, self.batch)
        progress = self.question_progress(rows)
        model_pending = [row for row in rows if not progress[int(row["id"])]["model_done"]]
        model_pending_ids = {int(row["id"]) for row in model_pending}
        for row in rows:
            if int(row["id"]) not in model_pending_ids:
                self.item(int(row["id"]), status="model_completed", error="")
        if model_pending:
            if retry_of_job_id is not None:
                for row in model_pending:
                    self.restore_question_workspace(row)
            self.model_stage(image, command, model_concurrency, model_version, model_pending, config, model_mode)
        else:
            self.log("阶段 2/4：模型跑题已有成功结果，跳过")
        self.ensure_active()
        progress = self.question_progress(rows)
        producer_pending = [row for row in rows if progress[int(row["id"])]["record_count"] == 0]
        producer_pending_ids = {int(row["id"]) for row in producer_pending}
        for row in rows:
            if int(row["id"]) not in producer_pending_ids:
                self.item(int(row["id"]), status="produced", error="")
        if producer_pending:
            self.producer_stage(codex_concurrency, producer_pending)
        else:
            self.log("阶段 3/4：交付记录已经生成，跳过")
        self.ensure_active()
        progress = self.question_progress(rows)
        batch_folder = Path(batch["folder_path"])
        delivery_complete = bool(rows) and all(
            progress[int(row["id"])]["delivery_passed"] for row in rows
        )
        exported = bool(list(batch_folder.glob("CC_Codex*.xlsx"))) and bool(
            list(batch_folder.glob("轨迹_*.jsonl"))
        )
        if retry_of_job_id is not None and delivery_complete and exported:
            self.log("阶段 4/4：交付质检、Excel 和原始轨迹均已存在，跳过")
            finished = timestamp()
            for row in rows:
                self.item(int(row["id"]), status="completed", error="", finished_at=finished)
        else:
            self.qc_export_stage()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--model-mode", choices=("local", "docker"), default="local")
    parser.add_argument("--model-concurrency", type=int, default=2)
    parser.add_argument("--qc-concurrency", type=int, default=2)
    parser.add_argument("--codex-concurrency", type=int, default=2)
    args = parser.parse_args()
    try:
        with closing(sqlite3.connect(args.db)) as connection:
            cursor = connection.execute(
                "UPDATE pipeline_jobs SET status='running', started_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (timestamp(), args.job_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                print("流水线任务已不再处于可运行状态", file=sys.stderr)
                return 130
        runner = Pipeline(args.db, args.batch, args.job_id, print)
        runner.execute(args.image, args.claude_command, max(1, min(args.qc_concurrency, 8)), max(1, min(args.model_concurrency, 8)), max(1, min(args.codex_concurrency, 8)), args.model_mode)
        runner.set_job(status="completed", finished_at=timestamp(), last_message="全流程完成")
        return 0
    except PipelineInterrupted as exc:
        print(str(exc), file=sys.stderr)
        return 130
    except Exception as exc:
        try:
            with closing(sqlite3.connect(args.db)) as connection:
                connection.execute(
                    "UPDATE pipeline_jobs SET status='failed', error=?, finished_at=?, last_message=? "
                    "WHERE id=? AND status IN ('queued','running')",
                    (str(exc), timestamp(), str(exc)[:1000], args.job_id),
                )
                connection.commit()
        except sqlite3.Error:
            pass
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
