#!/usr/bin/env python3
"""Load one QC-passed SQLite question and exec Claude Code in its workspace."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect, prompt_hash  # noqa: E402


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
        lines = path.read_text(encoding="utf-8").splitlines()
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
    """Build the Claude Code command for an isolated task workspace.

    Interactive launches keep the native session for local macOS runs.  Headless
    launches use print mode, which skips the workspace-trust UI and never waits
    for a permission answer on servers without a terminal.
    """
    if headless:
        return [
            claude,
            "--print",
            "--dangerously-skip-permissions",
            "--permission-mode",
            "bypassPermissions",
            "--permission-prompts",
            "none",
            prompt,
        ]
    return [claude, "--dangerously-skip-permissions", prompt]


def build_claude_environment(config: ClaudeConfig, *, headless: bool = False) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("ANTHROPIC_API_KEY", None)
    environment["ANTHROPIC_BASE_URL"] = config.base_url
    environment["ANTHROPIC_AUTH_TOKEN"] = config.api_key
    environment["ANTHROPIC_MODEL"] = config.model
    if headless:
        environment["CI"] = "1"
    return environment


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
    os.execvpe(
        args.claude,
        command,
        build_claude_environment(config, headless=args.headless),
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
