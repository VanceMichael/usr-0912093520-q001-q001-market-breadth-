#!/usr/bin/env python3
"""List and launch QC-passed SQLite-backed questions in Claude Code through iTerm2."""

from __future__ import annotations

import argparse
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect, parse_selection, prompt_hash, question_rows  # noqa: E402
from launch_task import load_claude_config  # noqa: E402


def find_claude() -> str | None:
    discovered = shutil.which("claude")
    if discovered:
        return discovered
    local_install = Path.home() / ".local/bin/claude"
    return str(local_install) if local_install.is_file() else None


def claude_version(claude: str) -> str:
    result = subprocess.run(
        [claude, "--version"], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or "claude --version failed")
    match = re.search(r"\b\d+(?:\.\d+)+\b", result.stdout)
    if not match:
        raise RuntimeError(
            "claude --version did not contain a numeric version: "
            + (result.stdout.strip() or "<empty output>")
        )
    return match.group(0)


def is_ready(row: sqlite3.Row) -> bool:
    return bool(
        row["mechanical_qc"] == "pass"
        and row["qc_decision"] == "pass"
        and row["qc_prompt_sha256"] == prompt_hash(row["prompt"])
        and row["status"] == "approved"
    )


def validate_question(row: sqlite3.Row) -> Path:
    if not is_ready(row):
        raise ValueError(
            f"{row['task_id']}: question is not QC-passed with a current prompt fingerprint"
        )
    folder = Path(row["folder_path"])
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError(f"{row['task_id']}: workspace folder does not exist")
    if not row["prompt"].strip() or "\x00" in row["prompt"]:
        raise ValueError(f"{row['task_id']}: prompt is empty or invalid")
    return folder


def open_iterm(launcher: Path) -> subprocess.CompletedProcess[str]:
    script = """
on run argv
    set launcherPath to item 1 of argv
    tell application "iTerm"
        activate
        set newWindow to (create window with default profile)
        tell current session of newWindow to write text (quoted form of launcherPath)
    end tell
end run
"""
    return subprocess.run(
        ["osascript", "-e", script, str(launcher)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )


def iterm_available() -> bool:
    return Path("/Applications/iTerm.app").is_dir() and bool(shutil.which("osascript"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "production.sqlite3")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--select")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--max-open", type=int, default=4)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args()

    try:
        connection = connect(args.db.resolve())
        rows = question_rows(connection, args.batch)
    except (sqlite3.Error, ValueError) as exc:
        print(f"Cannot read batch: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print(f"No questions found in batch {args.batch}.", file=sys.stderr)
        connection.close()
        return 1

    for row in rows:
        mark = "READY" if is_ready(row) else "BLOCKED"
        print(
            f"{row['question_no']:>3}  {mark:<7}  {row['task_id']:<14}  "
            f"{row['title']}  [{row['folder_name']}]"
        )
    if args.list and not args.select:
        connection.close()
        return 0
    if not args.select:
        print("Use --select with question numbers, ranges, or task IDs.", file=sys.stderr)
        connection.close()
        return 2

    try:
        selected = parse_selection(args.select, rows)
        if len(selected) > args.max_open:
            raise ValueError(
                f"selected {len(selected)} questions, exceeding --max-open {args.max_open}"
            )
        prepared = [(row, validate_question(row)) for row in selected]
        env_file = args.env_file.resolve(strict=True)
        load_claude_config(env_file)
        claude = find_claude()
        if not claude:
            raise RuntimeError("Claude Code is not available")
        version = claude_version(claude)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        connection.close()
        return 1

    print(f"Claude Code: {version}")
    print("配置: 已读取根目录 .env（URL、模型和 Key 不显示）")
    for row, folder in prepared:
        print(
            f"PREVIEW {row['question_no']}: cd {shlex.quote(str(folder))} && "
            "claude --dangerously-skip-permissions <SQLite 原始 prompt>"
        )

    if not args.launch:
        print("Preview only. Add --launch to open iTerm2 sessions.")
        connection.close()
        return 0
    if platform.system() != "Darwin":
        print("Automatic launch currently supports macOS only.", file=sys.stderr)
        connection.close()
        return 1
    if not iterm_available():
        print("iTerm2 or osascript is not available.", file=sys.stderr)
        connection.close()
        return 1

    batch_run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    batch_folder = Path(prepared[0][0]["folder_path"]).parent
    launch_root = batch_folder / ".runs" / batch_run_id
    launch_root.mkdir(parents=True, exist_ok=False)
    launch_helper = Path(__file__).with_name("launch_task.py").resolve()
    for row, _folder in prepared:
        run_dir = launch_root / row["task_id"]
        run_dir.mkdir()
        launcher = run_dir / "launch.command"
        command = [
            sys.executable, str(launch_helper),
            "--env-file", str(env_file),
            "--db", str(args.db.resolve()),
            "--question-id", str(row["id"]),
            "--claude", claude,
        ]
        launcher.write_text(
            "#!/bin/zsh\nset -eu\n"
            f"echo {shlex.quote('Task: ' + row['task_id'])}\n"
            f"exec {shlex.join(command)}\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        result = open_iterm(launcher)
        if result.returncode:
            print(
                f"Failed to open iTerm2 for {row['task_id']}: {result.stdout.strip()}",
                file=sys.stderr,
            )
            connection.close()
            return 1
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO runs(question_id, batch_run_id, launched_at, codex_version, "
            "relay_provider, relay_host, relay_wire_api, model, harness, harness_version) "
            "VALUES(?, ?, ?, ?, '', '', '', '', 'Claude Code', ?)",
            (row["id"], batch_run_id, timestamp, version, version),
        )
        connection.execute(
            "UPDATE questions SET status='running', updated_at=? WHERE id=?",
            (timestamp, row["id"]),
        )
        connection.commit()
        print(f"LAUNCHED {row['task_id']}: {row['folder_path']}")
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
