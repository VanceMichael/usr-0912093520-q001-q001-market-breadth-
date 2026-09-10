#!/usr/bin/env python3
"""Validate that a Claude run produced an attributable, useful code trajectory."""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
SKILL_PATH_RE = re.compile(
    r"(?i)(?:\.agents[/\\]+skills[/\\]+|\.claude[/\\]+skills[/\\]+|"
    r"\.codex[/\\]+skills[/\\]+|[/\\]skills[/\\][^\s'\"`]+[/\\]SKILL\.md)"
)
CONFIG_PATH_RE = re.compile(
    r"(?i)(?:CLAUDE\.md|AGENTS\.md|\.claude[/\\]+settings(?:\.local)?\.json|"
    r"\.codex[/\\]+config\.toml)"
)


class TrajectoryGateError(RuntimeError):
    """Raised when a zero-exit Claude process did not produce useful work."""


@dataclass(frozen=True)
class TrajectoryEvidence:
    path: Path
    session_id: str
    prompt_id: str
    assistant_messages: int
    tool_uses: int
    changed_files: int


def _message_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return str(event.get("prompt") or "")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(event.get("prompt") or "")
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _event_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _event_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _event_strings(nested)


def _path_is_within_workspace(path_text: str, workspace: Path) -> bool:
    normalized = path_text.replace("\\", "/")
    if "://" in normalized:
        return False
    if normalized == "/workspace" or normalized.startswith("/workspace/"):
        return True
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/"):
        root = str(workspace.resolve()).replace("\\", "/").rstrip("/")
        return normalized.casefold() == root.casefold() or normalized.casefold().startswith(root.casefold() + "/")
    try:
        candidate = (workspace / normalized).resolve()
    except OSError:
        return False
    return candidate == workspace.resolve() or workspace.resolve() in candidate.parents


def _referenced_path(text: str, marker_start: int) -> str:
    start = marker_start
    while start > 0 and text[start - 1] not in " \t\r\n'\"`()[]{}=:;":
        start -= 1
    return text[start:]


def _trajectory_metrics(
    path: Path, prompt: str, workspace: Path,
) -> tuple[str, str, int, int, bool] | None:
    matched_indexes: list[int] = []
    assistant_messages = 0
    tool_uses = 0
    last_tool_order = -1
    last_text_order = -1
    order = 0
    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    call_positions: dict[str, list[int]] = {}
    result_positions: dict[str, list[int]] = {}
    external_accesses: set[str] = set()
    events: list[dict[str, Any]] = []
    invalid_lines: list[str] = []
    handle = path.open(encoding="utf-8")
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                invalid_lines.append(f"{path.name}:{line_number} 不是有效 JSONL：{exc}")
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
    matched_indexes = [
        index for index, event in enumerate(events)
        if event.get("type") == "user" and _message_text(event) == prompt
    ]
    if not matched_indexes:
        return None
    if invalid_lines:
        raise TrajectoryGateError(invalid_lines[0])
    if len(matched_indexes) != 1:
        raise TrajectoryGateError(f"{path.name} 必须且只能包含一次原始 Prompt，实际为 {len(matched_indexes)} 次")
    matched_event = events[matched_indexes[0]]
    session_id = str(matched_event.get("sessionId") or matched_event.get("session_id") or "")
    prompt_id = str(
        matched_event.get("promptId") or matched_event.get("prompt_id")
        or matched_event.get("uuid") or ""
    )
    for event_index, event in enumerate(events):
        event_session = str(event.get("sessionId") or event.get("session_id") or "")
        if event_session not in {"", session_id}:
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                identifier = str(block.get("id") or "")
                if not identifier:
                    raise TrajectoryGateError(f"{path.name} 包含没有 id 的工具调用")
                calls[identifier] += 1
                call_positions.setdefault(identifier, []).append(event_index)
                if str(block.get("name") or "").casefold() in {"skill", "slashcommand"}:
                    external_accesses.add("调用了 Skill/SlashCommand 工具")
                for text in _event_strings(block.get("input")):
                    match = SKILL_PATH_RE.search(text) or CONFIG_PATH_RE.search(text)
                    if match and not _path_is_within_workspace(
                        _referenced_path(text, match.start()), workspace,
                    ):
                        kind = "Skill" if SKILL_PATH_RE.search(text) else "指令/配置"
                        external_accesses.add(f"读取了工作区外的{kind}文件")
            elif block.get("type") == "tool_result":
                identifier = str(block.get("tool_use_id") or "")
                if not identifier:
                    raise TrajectoryGateError(f"{path.name} 包含没有 tool_use_id 的工具结果")
                results[identifier] += 1
                result_positions.setdefault(identifier, []).append(event_index)
        if event.get("type") != "assistant":
            continue
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            continue
        meaningful = False
        for block in blocks:
            order += 1
            if isinstance(block, str) and block.strip():
                meaningful = True
                last_text_order = order
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                meaningful = True
                tool_uses += 1
                last_tool_order = order
            elif (
                isinstance(block, dict)
                and block.get("type") == "text"
                and str(block.get("text") or "").strip()
            ):
                meaningful = True
                last_text_order = order
        if meaningful:
            assistant_messages += 1
    if not session_id:
        raise TrajectoryGateError(f"{path.name} 的原始 Prompt 缺少 SessionID")
    if not prompt_id:
        raise TrajectoryGateError(f"{path.name} 的原始 Prompt 缺少 PromptID")
    duplicate_calls = sorted(identifier for identifier, count in calls.items() if count > 1)
    duplicate_results = sorted(identifier for identifier, count in results.items() if count > 1)
    if duplicate_calls or duplicate_results:
        raise TrajectoryGateError("轨迹重复使用工具调用标识")
    missing_results = sorted((calls - results).elements())
    orphan_results = sorted((results - calls).elements())
    if missing_results:
        raise TrajectoryGateError("轨迹存在没有匹配结果的工具调用：" + "、".join(missing_results[:10]))
    if orphan_results:
        raise TrajectoryGateError("轨迹存在没有匹配调用的工具结果：" + "、".join(orphan_results[:10]))
    out_of_order = sorted(
        identifier for identifier in calls.keys() & results.keys()
        if min(result_positions[identifier]) <= min(call_positions[identifier])
    )
    if out_of_order:
        raise TrajectoryGateError("轨迹存在先于调用返回的工具结果：" + "、".join(out_of_order[:10]))
    if external_accesses:
        raise TrajectoryGateError("；".join(sorted(external_accesses)))
    return (
        session_id,
        prompt_id,
        assistant_messages,
        tool_uses,
        last_tool_order >= 0 and last_text_order > last_tool_order,
    )


def _workspace_changes(workspace: Path, baseline_sha: str) -> int:
    if not SHA_RE.fullmatch(baseline_sha):
        raise TrajectoryGateError("缺少有效的初始快照 SHA")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(workspace), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    root = git("rev-parse", "--show-toplevel")
    baseline = git("cat-file", "-e", f"{baseline_sha}^{{commit}}")
    if (
        root.returncode
        or Path(root.stdout.strip()).resolve() != workspace.resolve()
        or baseline.returncode
    ):
        raise TrajectoryGateError("无法验证题目工作区及其初始快照")
    changed = git("diff", "--name-only", "--no-ext-diff", baseline_sha, "--")
    untracked = git("ls-files", "--others", "--exclude-standard")
    if changed.returncode or untracked.returncode:
        detail = (changed.stdout + untracked.stdout).strip()[-300:]
        raise TrajectoryGateError(f"无法检查题目工作区变化：{detail}")
    files = {
        value.strip()
        for value in (changed.stdout + "\n" + untracked.stdout).splitlines()
        if value.strip()
    }
    if not files:
        raise TrajectoryGateError("题目工作区相对初始快照没有任何代码变化")
    return len(files)


def validate_effective_trajectory(
    trajectory_root: Path,
    prompt: str,
    workspace: Path,
    baseline_sha: str,
) -> TrajectoryEvidence:
    """Require a prompt-matched session, real tool work, final text, and code changes."""
    try:
        paths = sorted(trajectory_root.rglob("*.jsonl"))
    except OSError as exc:
        raise TrajectoryGateError(f"无法读取 Claude 轨迹目录：{exc}") from exc
    if not paths:
        raise TrajectoryGateError("Claude 没有生成 JSONL 轨迹")

    candidates = []
    for path in paths:
        try:
            metrics = _trajectory_metrics(path, prompt, workspace)
        except (OSError, UnicodeError, TrajectoryGateError) as exc:
            raise TrajectoryGateError(f"无法验证 Claude 轨迹 {path}：{exc}") from exc
        if metrics is not None:
            candidates.append((path, *metrics))
    if not candidates:
        raise TrajectoryGateError("没有找到与本题原始 Prompt 精确匹配的 Claude 轨迹")

    path, session_id, prompt_id, assistant_messages, tool_uses, has_final_text = max(
        candidates, key=lambda item: (item[5], item[4], item[3])
    )
    if assistant_messages == 0:
        raise TrajectoryGateError("轨迹中没有真实的 assistant 响应")
    if tool_uses == 0:
        raise TrajectoryGateError("轨迹中没有任何 Claude 工具调用")
    if not has_final_text:
        raise TrajectoryGateError("轨迹中没有工具调用后的最终文字回复")
    changed_files = _workspace_changes(workspace, baseline_sha)
    return TrajectoryEvidence(
        path=path,
        session_id=session_id,
        prompt_id=prompt_id,
        assistant_messages=assistant_messages,
        tool_uses=tool_uses,
        changed_files=changed_files,
    )
