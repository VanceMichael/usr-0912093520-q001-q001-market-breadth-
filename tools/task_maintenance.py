#!/usr/bin/env python3
"""Per-question Docker takeover and destructive reset operations."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from tools.batch_pipeline import SNAPSHOT_RE, connect, now
from tools.text_encoding import read_portable_text
from tools.trajectory_gate import validate_effective_trajectory


def _question(connection: sqlite3.Connection, question_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT q.*,b.name AS batch_name,b.folder_path AS batch_folder "
        "FROM questions q JOIN batches b ON b.id=q.batch_id WHERE q.id=?",
        (question_id,),
    ).fetchone()
    if row is None:
        raise ValueError("题目不存在")
    return row


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in read_portable_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _session_id(root: Path, expected_prompt: str) -> str:
    for path in sorted(root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                message = event.get("message") if isinstance(event, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    content = "".join(
                        str(block.get("text") or "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                if event.get("type") == "user" and content == expected_prompt:
                    value = event.get("sessionId") or event.get("session_id") or path.stem
                    if value:
                        return str(value)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    raise ValueError("现有轨迹中找不到可恢复的 Claude SessionID")


def _user_text(event: dict) -> str:
    message = event.get("message")
    if event.get("type") != "user" or not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _session_user_turn_count(root: Path, session_id: str) -> int:
    count = 0
    for path in root.rglob("*.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                event_session = str(event.get("sessionId") or event.get("session_id") or path.stem)
                prompt_id = event.get("promptId") or event.get("prompt_id") or event.get("uuid")
                if event_session == session_id and prompt_id and _user_text(event):
                    count += 1
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return count


def _render_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _secure_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run(
        ["whoami"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False, creationflags=flags,
    )
    account = identity.stdout.strip()
    if identity.returncode or not account:
        raise OSError("无法确定当前 Windows 用户，未生成接管命令")
    acl = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False, creationflags=flags,
    )
    if acl.returncode:
        raise OSError("无法限制接管凭据文件权限，未生成接管命令")


def _stop_containers(containers: list[str]) -> None:
    for container in containers:
        try:
            subprocess.run(
                ["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False, timeout=30,
            )
            state = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False, timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"停止容器超时：{container}") from exc
        if state.returncode == 0 and state.stdout.strip().lower() == "true":
            raise RuntimeError(f"容器仍在运行：{container}")


def begin_takeover(
    database: Path,
    question_id: int,
    env_file: Path,
    image: str,
    cpus: float,
    memory: str,
) -> dict[str, object]:
    """Reserve one question and prepare an isolated interactive resume command."""
    values = _dotenv(env_file)
    required = ("CC_SWITCH_BASE_URL", "CC_SWITCH_API_KEY", "CC_SWITCH_MODEL")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError("运行配置缺少：" + "、".join(missing))
    worker_env: Path | None = None
    active_containers: list[str] = []
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _question(connection, question_id)
        if bool(row["maintenance_mode"]):
            raise ValueError("题目已处于维护状态，请先完成接管或执行完整重置")
        if connection.execute(
            "SELECT 1 FROM solo2_submissions s JOIN records r ON r.record_id=s.record_id "
            "WHERE r.question_id=? AND s.status='succeeded' LIMIT 1", (question_id,),
        ).fetchone():
            raise ValueError("题目已经提交到 SOLO2，不能再人工接管")
        run = connection.execute(
            "SELECT * FROM runs WHERE question_id=? AND trajectory_root!='' "
            "ORDER BY launched_at DESC,id DESC LIMIT 1", (question_id,),
        ).fetchone()
        if run is None:
            raise ValueError("题目还没有可恢复的 Docker 运行状态")
        if run["status"] == "succeeded":
            raise ValueError("题目模型运行已经成功，不需要人工恢复中断会话")
        trajectory_root = Path(str(run["trajectory_root"])).resolve()
        if not trajectory_root.is_dir():
            raise ValueError("Claude 私有状态目录已经不存在，无法继续")
        session_id = str(run["session_id"] or "").strip() or _session_id(
            trajectory_root, str(row["prompt"]),
        )
        baseline_turns = _session_user_turn_count(trajectory_root, session_id)
        if baseline_turns < 1:
            raise ValueError("现有轨迹中没有可恢复的有效用户轮次")
        timestamp = now()
        container_name = f"ccusr-takeover-{row['task_id']}-{uuid.uuid4().hex[:4]}"
        run_id = f"manual-{uuid.uuid4().hex[:12]}"
        active_containers = [
            str(item["container_id"]).strip()
            for item in connection.execute(
                "SELECT container_id FROM runs WHERE question_id=? AND status='running' "
                "AND container_id!=''", (question_id,),
            ).fetchall()
            if str(item["container_id"] or "").strip()
        ]
        state_root = trajectory_root.parent
        home_root = state_root / "home"
        home_root.mkdir(parents=True, exist_ok=True)
        worker_env = state_root / f".{run_id}.env"
        worker_env.write_text(
            "".join(
                f"{target}={values[source]}\n" for target, source in (
                    ("ANTHROPIC_BASE_URL", "CC_SWITCH_BASE_URL"),
                    ("ANTHROPIC_AUTH_TOKEN", "CC_SWITCH_API_KEY"),
                    ("ANTHROPIC_MODEL", "CC_SWITCH_MODEL"),
                )
            ) + "CLAUDE_CONFIG_DIR=/state/claude\nHOME=/state/home\n",
            encoding="utf-8",
        )
        _secure_file(worker_env)
        connection.execute(
            "UPDATE questions SET maintenance_mode=1,maintenance_note=?,"
            "model_lease_owner='',model_lease_expires_at='',delivery_lease_owner='',"
            "delivery_lease_expires_at='',updated_at=? WHERE id=?",
            ("等待人工在隔离容器中继续", timestamp, question_id),
        )
        connection.execute(
            "INSERT INTO runs(question_id,batch_run_id,launched_at,model,harness,harness_version,"
            "session_id,container_cwd,trajectory_root,operating_system,status,started_at,"
            "container_id,heartbeat_at,manual_baseline_turns,manual_env_file) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                question_id, run_id, timestamp, run["model"], "Claude Code",
                run["harness_version"], session_id, "/workspace", str(trajectory_root),
                f"Linux (Docker takeover on {sys.platform})", "prepared", timestamp,
                container_name, timestamp, baseline_turns, str(worker_env),
            ),
        )
        connection.commit()

    try:
        _stop_containers(active_containers)
    except RuntimeError:
        with connect(database) as connection:
            connection.execute(
                "UPDATE runs SET status='failed',finished_at=?,error_message=? WHERE batch_run_id=?",
                (now(), "旧 worker 未在 30 秒内退出", run_id),
            )
            connection.execute(
                "UPDATE questions SET maintenance_note='人工接管暂停：旧 worker 未在 30 秒内退出',"
                "updated_at=? WHERE id=?", (now(), question_id),
            )
            connection.commit()
        raise
    with connect(database) as connection:
        connection.execute(
            "UPDATE runs SET status='interrupted',finished_at=?,error_message=? "
            "WHERE question_id=? AND status='running' AND batch_run_id!=?",
            (now(), "已切换到人工接管", question_id, run_id),
        )
        connection.commit()
    command = [
        "docker", "run", "--rm", "-it", "--name", container_name,
        "--cpus", str(cpus), "--memory", memory, "--pids-limit", "512",
        "--env-file", str(worker_env),
        "-v", f"{Path(str(row['folder_path'])).resolve()}:/workspace",
        "-v", f"{trajectory_root}:/state/claude",
        "-v", f"{home_root}:/state/home",
        "-w", "/workspace", image,
        "claude", "--safe-mode", "--disable-slash-commands",
        "--dangerously-skip-permissions", "--permission-mode", "bypassPermissions",
        "--permission-prompts", "none", "--resume", session_id,
    ]
    return {
        "question_id": question_id,
        "task_id": row["task_id"],
        "run_id": run_id,
        "session_id": session_id,
        "command": _render_command(command),
        "container_name": container_name,
    }


def finish_takeover(database: Path, question_id: int) -> dict[str, object]:
    with connect(database) as connection:
        row = _question(connection, question_id)
        run = connection.execute(
            "SELECT * FROM runs WHERE question_id=? AND batch_run_id LIKE 'manual-%' "
            "ORDER BY launched_at DESC,id DESC LIMIT 1", (question_id,),
        ).fetchone()
        if run is None:
            raise ValueError("找不到人工接管运行记录")
        if run["status"] != "prepared":
            raise ValueError("最近一次人工接管不处于待确认状态")
    current_turns = _session_user_turn_count(
        Path(str(run["trajectory_root"])), str(run["session_id"]),
    )
    if current_turns <= int(run["manual_baseline_turns"] or 0):
        raise ValueError("人工接管尚未产生新的用户轮次，不能标记完成")
    evidence = validate_effective_trajectory(
        Path(str(run["trajectory_root"])), str(row["prompt"]),
        Path(str(row["folder_path"])).resolve(strict=True), str(row["local_initial_sha"]),
    )
    timestamp = now()
    with connect(database) as connection:
        connection.execute(
            "UPDATE runs SET status='succeeded',session_id=?,finished_at=?,exit_code=0,"
            "error_message='',heartbeat_at=? WHERE id=?",
            (evidence.session_id, timestamp, timestamp, run["id"]),
        )
        connection.execute(
            "UPDATE questions SET status='completed',maintenance_mode=0,maintenance_note='',"
            "updated_at=? WHERE id=?", (timestamp, question_id),
        )
        connection.commit()
    manual_env_text = str(run["manual_env_file"] or "").strip()
    if manual_env_text:
        Path(manual_env_text).unlink(missing_ok=True)
    return {"task_id": row["task_id"], "session_id": evidence.session_id}


def reset_question(database: Path, question_id: int, reason: str = "人工完整重置") -> dict[str, object]:
    """Restore the initial git snapshot and irreversibly remove local run/delivery data."""
    with connect(database) as connection:
        initial = _question(connection, question_id)
    folder = Path(str(initial["folder_path"])).resolve(strict=True)
    baseline = str(initial["local_initial_sha"] or "").strip()
    snapshot = str(initial["initial_snapshot"] or "").strip()
    if not baseline or not SNAPSHOT_RE.fullmatch(snapshot) or not snapshot.endswith(baseline):
        raise ValueError("初始 SHA 与 GitHub 快照不一致，拒绝重置")
    preflight = subprocess.run(
        ["git", "-C", str(folder), "cat-file", "-e", f"{baseline}^{{commit}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if preflight.returncode:
        raise ValueError("本地不存在登记的初始 Git 快照，拒绝重置")

    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _question(connection, question_id)
        if connection.execute(
            "SELECT 1 FROM solo2_submissions s JOIN records r ON r.record_id=s.record_id "
            "WHERE r.question_id=? AND s.status='succeeded' LIMIT 1", (question_id,),
        ).fetchone():
            raise ValueError("题目已经提交到 SOLO2，远端记录不可撤销，拒绝本地重置")
        runs = connection.execute(
            "SELECT id,container_id,trajectory_root FROM runs WHERE question_id=?", (question_id,),
        ).fetchall()
        records = connection.execute(
            "SELECT record_id,trajectory_file FROM records WHERE question_id=?", (question_id,),
        ).fetchall()
        connection.execute(
            "UPDATE questions SET maintenance_mode=1,maintenance_note='正在完整重置',"
            "model_lease_owner='',model_lease_expires_at='',delivery_lease_owner='',"
            "delivery_lease_expires_at='' WHERE id=?", (question_id,),
        )
        connection.commit()

    containers = [
        str(run["container_id"]).strip() for run in runs
        if str(run["container_id"] or "").strip()
    ]
    try:
        _stop_containers(containers)
    except RuntimeError:
        with connect(database) as connection:
            connection.execute(
                "UPDATE questions SET maintenance_note='完整重置暂停：旧 worker 容器仍在运行',"
                "updated_at=? WHERE id=?", (now(), question_id),
            )
            connection.commit()
        raise
    with connect(database) as connection:
        connection.execute(
            "UPDATE runs SET status='interrupted',finished_at=?,error_message=? "
            "WHERE question_id=? AND status='running'",
            (now(), "人工完整重置", question_id),
        )
        connection.commit()

    for git_args in (("reset", "--hard", baseline), ("clean", "-ffdx")):
        result = subprocess.run(
            ["git", "-C", str(folder), *git_args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if result.returncode:
            raise RuntimeError("恢复初始快照失败：" + result.stdout.strip()[-500:])
    status = subprocess.run(
        ["git", "-C", str(folder), "status", "--porcelain", "--untracked-files=all"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if status.returncode or status.stdout.strip():
        raise RuntimeError("恢复后工作区仍不干净，停止删除数据库记录")

    removed_roots: set[Path] = set()
    for run in runs:
        raw = str(run["trajectory_root"] or "").strip()
        if raw:
            trajectory = Path(raw).resolve()
            if trajectory.is_dir() and trajectory.name == "claude":
                attempt = trajectory.parent
                if attempt not in removed_roots:
                    shutil.rmtree(attempt)
                    removed_roots.add(attempt)
    batch_folder = Path(str(row["batch_folder"])).resolve()
    for record in records:
        name = str(record["trajectory_file"] or "")
        for candidate in batch_folder.glob(f"*{name}") if name else ():
            candidate.unlink(missing_ok=True)
    for workbook in batch_folder.glob("CC_Codex*.xlsx"):
        workbook.unlink(missing_ok=True)

    timestamp = now()
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM solo2_submissions WHERE record_id IN "
            "(SELECT record_id FROM records WHERE question_id=?)", (question_id,),
        )
        connection.execute("DELETE FROM records WHERE question_id=?", (question_id,))
        connection.execute("DELETE FROM runs WHERE question_id=?", (question_id,))
        connection.execute(
            "UPDATE questions SET status='approved',maintenance_mode=0,maintenance_note='',"
            "reset_count=reset_count+1,updated_at=? WHERE id=?", (timestamp, question_id),
        )
        connection.execute(
            "UPDATE batches SET status='approved',updated_at=? WHERE id=?",
            (timestamp, row["batch_id"]),
        )
        connection.execute(
            "INSERT INTO question_reset_audit(question_id,task_id,reason,removed_runs,"
            "removed_records,reset_at) VALUES(?,?,?,?,?,?)",
            (question_id, row["task_id"], reason.strip()[:500], len(runs), len(records), timestamp),
        )
        connection.commit()
    return {
        "task_id": row["task_id"], "removed_runs": len(runs),
        "removed_records": len(records), "removed_exports": True,
    }
