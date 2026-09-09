#!/usr/bin/env python3
"""List and launch QC-passed SQLite-backed questions in Claude Code cross-platform."""

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
    candidates = [
        Path.home() / ".local/bin/claude",
        Path.home() / ".local/bin/claude.exe",
        Path.home() / ".local/bin/claude.cmd",
        Path.home() / "AppData/Roaming/npm/claude.cmd",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


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


def powershell_executable() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_windows_launcher(
    launcher: Path,
    launch_helper: Path,
    env_file: Path,
    database: Path,
    question_id: int,
    claude: str,
) -> None:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        (
            f"& {powershell_quote(sys.executable)} {powershell_quote(str(launch_helper))} "
            f"--env-file {powershell_quote(str(env_file))} "
            f"--db {powershell_quote(str(database))} "
            f"--question-id {question_id} --claude {powershell_quote(claude)} --headless"
        ),
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
    ]
    # Windows PowerShell 5.1 needs a BOM to decode non-ASCII paths as UTF-8.
    launcher.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def open_windows(launcher: Path) -> subprocess.Popen[str]:
    shell = powershell_executable()
    if not shell:
        raise RuntimeError("PowerShell is not available")
    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(
        [
            shell,
            "-NoLogo",
            "-NoExit",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
        ],
        creationflags=creation_flags,
        cwd=str(launcher.parent),
    )


def select_launch_mode(requested: str) -> str:
    """Resolve launch mode; auto is always unattended, with explicit overrides."""
    if requested in {"iterm", "server"}:
        return requested
    # Auto launches must not stop on Claude Code's workspace-trust prompt.
    return "server"


def open_server(launcher: Path, folder: Path) -> subprocess.Popen[bytes]:
    """Start an unattended Claude process detached from the launching terminal."""
    return subprocess.Popen(
        [str(launcher), "--headless"],
        cwd=folder,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "production.sqlite3")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--select")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument(
        "--max-open", type=int, default=100,
        help="maximum questions to launch in one batch (default: 100)",
    )
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument(
        "--mode",
        choices=("auto", "iterm", "server"),
        default="auto",
        help="use unattended mode by default; choose iterm only for a local interactive window",
    )
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

    mode = select_launch_mode(args.mode)
    print(f"Claude Code: {version}")
    print("配置: 已读取根目录 .env（URL、模型和 Key 不显示）")
    preview_command = (
        "claude --print --dangerously-skip-permissions "
        "--permission-mode bypassPermissions --permission-prompts none "
        "<SQLite 原始 prompt>"
        if mode == "server"
        else "claude --dangerously-skip-permissions <SQLite 原始 prompt>"
    )
    for row, folder in prepared:
        print(
            f"PREVIEW {row['question_no']}: cd {shlex.quote(str(folder))} && "
            + preview_command
        )

    if not args.launch:
        print("Preview only. Add --launch to start Claude Code sessions.")
        connection.close()
        return 0
    if args.mode == "iterm" and (platform.system() != "Darwin" or not iterm_available()):
        print("iTerm2 mode requires macOS with iTerm2 and osascript.", file=sys.stderr)
        connection.close()
        return 1
    system = platform.system()
    if args.mode == "auto" and system == "Windows" and not powershell_executable():
        print("PowerShell is not available.", file=sys.stderr)
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
            ("#!/bin/zsh\nset -eu\n" if mode == "iterm" else "#!/usr/bin/env sh\nset -eu\n")
            + f"echo {shlex.quote('Task: ' + row['task_id'])}\n"
            + f"exec {shlex.join(command)} \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        if mode == "iterm":
            result = open_iterm(launcher)
            if result.returncode:
                print(
                    f"Failed to open iTerm2 for {row['task_id']}: {result.stdout.strip()}",
                    file=sys.stderr,
                )
                connection.close()
                return 1
        elif system == "Windows" and args.mode == "auto":
            launcher = run_dir / "launch.ps1"
            write_windows_launcher(
                launcher, launch_helper, env_file, args.db.resolve(),
                int(row["id"]), claude,
            )
            try:
                open_windows(launcher)
            except (OSError, RuntimeError) as exc:
                print(
                    f"Failed to open PowerShell for {row['task_id']}: {exc}",
                    file=sys.stderr,
                )
        else:
            launcher = run_dir / "launch.command"
            command = [
                sys.executable, str(launch_helper),
                "--env-file", str(env_file),
                "--db", str(args.db.resolve()),
                "--question-id", str(row["id"]),
                "--claude", claude,
            ]
            launcher.write_text(
                "#!/usr/bin/env sh\nset -eu\n"
                f"echo {shlex.quote('Task: ' + row['task_id'])}\n"
                f"exec {shlex.join(command)} \"$@\"\n",
                encoding="utf-8",
            )
            launcher.chmod(0o700)
            try:
                process = open_server(launcher, Path(row["folder_path"]))
                (run_dir / "process.pid").write_text(f"{process.pid}\n", encoding="ascii")
            except OSError as exc:
                print(f"Failed to start server process for {row['task_id']}: {exc}", file=sys.stderr)
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
        print(f"LAUNCHED {row['task_id']} ({mode}): {row['folder_path']}")
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
