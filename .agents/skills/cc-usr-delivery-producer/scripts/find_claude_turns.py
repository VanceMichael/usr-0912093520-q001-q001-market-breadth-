#!/usr/bin/env python3
"""Locate the Claude Code transcript corresponding to one registered run."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def user_text(event: dict) -> str:
    if event.get("type") != "user" or event.get("isMeta") is True:
        return ""
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = [
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(text for text in texts if text)


def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if isinstance(value, dict):
                events.append(value)
    return events


def same_folder(raw: object, folder: Path, aliases: tuple[Path, ...] | str = ()) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    if isinstance(aliases, str):
        aliases = (Path(aliases),)
    try:
        resolved = Path(raw).resolve()
        return resolved == folder.resolve() or any(resolved == alias.resolve() for alias in aliases)
    except OSError:
        return False


def prompt_id(event: dict) -> tuple[str, str]:
    for key in ("promptId", "prompt_id", "uuid"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value, key
    return "", ""


def resolve_claude_root(
    question: sqlite3.Row, run: sqlite3.Row, explicit_root: Path | None,
) -> Path:
    if explicit_root is not None:
        root = explicit_root.expanduser().resolve()
    else:
        configured = str(run["trajectory_root"] or "").strip()
        if configured:
            root = Path(configured).expanduser().resolve()
        else:
            batch_folder = Path(question["folder_path"]).resolve().parent
            root = (
                batch_folder / ".runs" / str(run["batch_run_id"])
                / str(question["task_id"]) / "claude-home" / "projects"
            )
    if not root.is_dir():
        raise ValueError(
            "Claude trajectory root does not exist; pass --claude-root explicitly: "
            f"{root}"
        )
    return root


def locate(
    claude_root: Path, folder: Path, prompt: str, launched_at: datetime,
    folder_aliases: tuple[Path, ...] = (),
) -> dict:
    candidates: list[tuple[float, Path, list[dict], int]] = []
    earliest = launched_at - timedelta(minutes=5)
    for path in claude_root.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, launched_at.tzinfo) < earliest:
                continue
            events = read_events(path)
        except (OSError, ValueError):
            continue
        for index, event in enumerate(events):
            timestamp = parse_time(str(event.get("timestamp", "")))
            if (
                user_text(event).strip() == prompt.strip()
                and same_folder(event.get("cwd"), folder, folder_aliases)
                and timestamp is not None
                and timestamp >= earliest
            ):
                candidates.append(
                    (abs((timestamp - launched_at).total_seconds()), path, events, index)
                )
                break
    if not candidates:
        raise ValueError("no Claude Code session matches the registered run, folder, and prompt")
    candidates.sort(key=lambda item: (item[0], str(item[1])))
    _distance, path, events, first_index = candidates[0]
    matched = events[first_index]
    session_id = str(matched.get("sessionId") or path.stem)
    turns = []
    for event in events[first_index:]:
        text = user_text(event)
        identifier, source = prompt_id(event)
        event_session = str(event.get("sessionId") or session_id)
        if text and identifier and event_session == session_id:
            turns.append({
                "turn_no": len(turns) + 1,
                "prompt_id": identifier,
                "prompt_id_source": source,
                "timestamp": str(event.get("timestamp", "")),
                "user_prompt": text,
            })
    if not session_id or not turns:
        raise ValueError("matching Claude Code session lacks SessionID or user PromptID")
    return {
        "trajectory_path": str(path),
        "trajectory_file": path.name,
        "session_id": session_id,
        "candidate_count": len(candidates),
        "turns": turns,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--question", type=int, required=True)
    parser.add_argument("--claude-root", type=Path)
    args = parser.parse_args()
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(args.db.resolve())
        question = connection.execute(
            "SELECT q.* FROM questions q JOIN batches b ON b.id=q.batch_id "
            "WHERE b.name=? AND q.question_no=?",
            (args.batch, args.question),
        ).fetchone()
        if question is None:
            raise ValueError("question does not exist")
        run = connection.execute(
            "SELECT * FROM runs WHERE question_id=? ORDER BY launched_at DESC, id DESC LIMIT 1",
            (question["id"],),
        ).fetchone()
        if run is None:
            raise ValueError("question has no registered Claude Code run")
        launched_at = parse_time(run["launched_at"])
        if launched_at is None or launched_at.tzinfo is None:
            raise ValueError("registered launch timestamp is invalid")
        claude_root = resolve_claude_root(question, run, args.claude_root)
        folder_aliases: tuple[Path, ...] = ()
        if str(run["batch_run_id"]).startswith("docker-"):
            aliases = [Path("/workspace")]
            if run["container_cwd"]:
                aliases.append(Path(str(run["container_cwd"])))
            folder_aliases = tuple(aliases)
        result = locate(
            claude_root, Path(question["folder_path"]), question["prompt"],
            launched_at, folder_aliases,
        )
        result["trajectory_root"] = str(claude_root)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Claude session lookup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
