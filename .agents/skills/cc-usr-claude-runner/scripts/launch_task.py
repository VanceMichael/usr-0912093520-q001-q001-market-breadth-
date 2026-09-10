#!/usr/bin/env python3
"""Load one QC-passed SQLite question and exec Claude Code in its workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect, prompt_hash  # noqa: E402
from tools.text_encoding import read_portable_text  # noqa: E402


KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class ClaudeConfig:
    base_url: str
    model: str
    api_key: str
    host: str


def _parse_value(raw: str, line_number: int) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in {"'", '"'}:
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"line {line_number}: invalid quoted value: {exc}") from exc
        if len(parts) != 1:
            raise ValueError(f"line {line_number}: unexpected text after quoted value")
        return parts[0]
    comment = re.search(r"\s+#", raw)
    value = raw[: comment.start()].rstrip() if comment else raw
    if any(character.isspace() for character in value):
        raise ValueError(f"line {line_number}: values containing spaces must be quoted")
    return value


def parse_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = read_portable_text(path).splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read env file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"line {line_number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"line {line_number}: invalid variable name")
        if key in values:
            raise ValueError(f"line {line_number}: duplicate variable {key}")
        values[key] = _parse_value(raw_value, line_number)
    return values


def load_claude_config(path: Path) -> ClaudeConfig:
    values = parse_dotenv(path)
    base_url = values.get("CC_SWITCH_BASE_URL", "").strip()
    model = values.get("CC_SWITCH_MODEL", "").strip()
    api_key = values.get("CC_SWITCH_API_KEY", "")
    missing = [
        key for key, value in (
            ("CC_SWITCH_BASE_URL", base_url),
            ("CC_SWITCH_MODEL", model),
            ("CC_SWITCH_API_KEY", api_key),
        ) if not value
    ]
    if missing:
        raise ValueError(f"missing required value(s) in {path}: {', '.join(missing)}")
    parsed_url = urlparse(base_url)
    if not parsed_url.hostname or parsed_url.scheme not in {"http", "https"}:
        raise ValueError("CC_SWITCH_BASE_URL must be an absolute http(s) URL")
    if parsed_url.scheme != "https" and parsed_url.hostname not in LOCAL_HOSTS:
        raise ValueError("CC_SWITCH_BASE_URL must use https unless it is localhost")
    if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise ValueError("CC_SWITCH_BASE_URL must not contain credentials, a query, or a fragment")
    return ClaudeConfig(base_url.rstrip("/"), model, api_key, parsed_url.hostname)


def build_claude_command(claude: str, prompt: str, *, headless: bool = False) -> list[str]:
    """Build the clean Claude Code command for an isolated task workspace.

    Interactive launches keep the native session for local macOS runs.  Headless
    launches use print mode, which skips the workspace-trust UI and never waits
    for a permission answer on servers without a terminal. Safe mode and
    disabled slash commands keep project skills, plugins, hooks, MCP, memory,
    and custom instructions out of the model context.
    """
    if headless:
        return [
            claude,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--safe-mode",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--permission-mode",
            "bypassPermissions",
            "--permission-prompts",
            "none",
            prompt,
        ]
    return [
        claude,
        "--safe-mode",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        prompt,
    ]


def build_claude_environment(config: ClaudeConfig, *, headless: bool = False) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("ANTHROPIC_API_KEY", None)
    environment["ANTHROPIC_BASE_URL"] = config.base_url
    environment["ANTHROPIC_AUTH_TOKEN"] = config.api_key
    environment["ANTHROPIC_MODEL"] = config.model
    if headless:
        environment["CI"] = "1"
    return environment


def run_claude_on_windows(command: list[str], environment: dict[str, str]) -> int:
    """Run Windows command shims while preserving the exact Claude argv."""
    completed = subprocess.run(command, env=environment, check=False)
    return completed.returncode


def _shorten(value: object, limit: int = 1200) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n...（输出已截断）"


class StreamDisplay:
    """Render Claude stream-json events as concise, readable terminal progress."""

    def __init__(self, redactions: tuple[str, ...] = ()) -> None:
        self.tools: dict[str, str] = {}
        self.redactions = tuple(value for value in redactions if value)

    def _safe(self, value: object, limit: int = 1200) -> str:
        text = _shorten(value, limit)
        for secret in self.redactions:
            text = text.replace(secret, "[已隐藏]")
        return text

    def _tool_summary(self, name: str, tool_input: object) -> str:
        if not isinstance(tool_input, dict):
            return ""
        if name in {"Read", "Write", "Edit", "NotebookEdit"}:
            return self._safe(
                tool_input.get("file_path") or tool_input.get("notebook_path") or "", 500
            )
        if name == "Bash":
            return self._safe(tool_input.get("command", ""), 500)
        if name in {"Glob", "Grep"}:
            pattern = tool_input.get("pattern", "")
            path = tool_input.get("path", "")
            return self._safe(f"{pattern} {path}".strip(), 500)
        if name in {"WebFetch", "WebSearch"}:
            return self._safe(tool_input.get("url") or tool_input.get("query") or "", 500)
        return ""

    def render(self, raw_line: str) -> list[str]:
        line = raw_line.rstrip("\r\n")
        if not line:
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return [self._safe(line)]
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            return [f"Claude Code 已启动，会话 {event.get('session_id', '未知')}"]
        if event_type == "assistant":
            output: list[str] = []
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    output.append(self._safe(block["text"]))
                elif block.get("type") == "tool_use":
                    tool_id = str(block.get("id", ""))
                    name = str(block.get("name", "工具"))
                    self.tools[tool_id] = name
                    summary = self._tool_summary(name, block.get("input") or {})
                    output.append(f"\n▶ {name}" + (f"  {summary}" if summary else ""))
            return output
        if event_type == "user":
            output = []
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                name = self.tools.get(str(block.get("tool_use_id", "")), "工具")
                marker = "失败" if block.get("is_error") else "完成"
                content = block.get("content", "")
                detail = self._safe(content) if content else ""
                output.append(f"  {marker} {name}" + (f"\n{detail}" if detail else ""))
            return output
        if event_type == "result":
            if event.get("is_error"):
                return ["\nClaude Code 运行失败：" + self._safe(event.get("result") or event)]
            return ["\nClaude Code 运行完成"]
        return []


def run_headless_stream(command: list[str], environment: dict[str, str]) -> int:
    """Run headless Claude while displaying its structured events live."""
    process_command = command
    if sys.platform == "win32" and Path(command[0]).suffix.lower() in {".cmd", ".bat"}:
        process_command = [
            environment.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(command),
        ]
    process = subprocess.Popen(
        process_command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    display = StreamDisplay((
        environment.get("ANTHROPIC_AUTH_TOKEN", ""),
        environment.get("ANTHROPIC_BASE_URL", ""),
        environment.get("ANTHROPIC_MODEL", ""),
        command[-1] if command else "",
    ))
    assert process.stdout is not None
    for line in process.stdout:
        for rendered in display.render(line):
            print(rendered, flush=True)
    return process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--question-id", type=int, required=True)
    parser.add_argument("--claude", required=True)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run one unattended print-mode turn (for Linux/servers without iTerm2)",
    )
    args = parser.parse_args()
    try:
        config = load_claude_config(args.env_file.resolve(strict=True))
        connection = connect(args.db.resolve())
        question = connection.execute(
            "SELECT * FROM questions WHERE id=?", (args.question_id,)
        ).fetchone()
        connection.close()
        if question is None:
            raise ValueError("question does not exist")
        if not (
            question["mechanical_qc"] == "pass"
            and question["qc_decision"] == "pass"
            and question["status"] in {"approved", "running"}
            and question["qc_prompt_sha256"] == prompt_hash(question["prompt"])
        ):
            raise ValueError("question is no longer QC-passed with a current prompt fingerprint")
        folder = Path(question["folder_path"]).resolve(strict=True)
        if not folder.is_dir():
            raise ValueError("question workspace is not a directory")
        prompt = question["prompt"]
        if not prompt.strip() or "\x00" in prompt:
            raise ValueError("question prompt is empty or invalid")
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Launch failed: {exc}", file=sys.stderr)
        return 1

    os.chdir(folder)
    command = build_claude_command(args.claude, prompt, headless=args.headless)
    environment = build_claude_environment(config, headless=args.headless)
    if args.headless:
        return run_headless_stream(command, environment)
    if sys.platform == "win32" and Path(args.claude).suffix.lower() in {".cmd", ".bat"}:
        return run_claude_on_windows(command, environment)
    os.execvpe(args.claude, command, environment)
    return 1


if __name__ == "__main__":
    sys.exit(main())
