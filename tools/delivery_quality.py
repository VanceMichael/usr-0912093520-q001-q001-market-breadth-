#!/usr/bin/env python3
"""Deterministic evidence and anti-template gates for delivery descriptions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path


DIMENSIONS = ("delivery", "instruction", "planning", "reasoning", "execution")
EVIDENCE_SOURCES = {"trajectory", "workspace", "prompt"}
EVIDENCE_KINDS = {"requirement", "process", "product", "runtime"}
EVIDENCE_KIND_SOURCES = {
    "requirement": {"prompt"},
    "process": {"trajectory"},
    "product": {"workspace"},
    "runtime": {"trajectory"},
}
COVERAGE_STATUSES = {"met", "unmet", "uncertain"}
TECHNICAL_SPAN_RE = re.compile(
    r"`[^`]*`|https?://\S+|(?:[A-Za-z]:)?(?:[/\\][^\s，。；！？]+)+|"
    r"\b[A-Za-z_][A-Za-z0-9_.:/\\-]*\b|\b\d+(?:\.\d+)*\b"
)
PROTECTED_PROSE_RE = re.compile(
    r"`[^`]*`|https?://\S+|(?:[A-Za-z]:)?(?:[/\\][^\s，。；！？]+)+"
)
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/\\-]*")
TECHNICAL_WORDS = {
    "api", "http", "https", "json", "jsonl", "sql", "sqlite", "docker",
    "dockerfile", "node", "node.js", "python", "go", "java", "git", "github",
    "linux", "macos", "windows", "redis", "mysql", "postgresql", "grpc", "tcp",
}
LATIN_RE = re.compile(r"[A-Za-z]")
SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]?")
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
NON_MODEL_ENVIRONMENT_RE = re.compile(
    r"(?i)(?:"
    r"(?:当前|本地|宿主机|运行|执行|测试|目标|容器)?环境[^，。；]{0,36}"
    r"(?:没有|缺少|缺失|未安装|不可用|不具备|无法提供)|"
    r"(?:docker|docker\s+compose|compose|pytest|maven|java|git)[^，。；]{0,30}"
    r"(?:command\s+not\s+found|not\s+found|no\s+docker|unavailable|不存在|不可用|未安装|缺少)|"
    r"(?:没有安装|未安装|缺少|找不到)[^，。；]{0,18}"
    r"(?:docker|docker\s+compose|compose|pytest|maven|java|git)(?:\s*命令)?|"
    r"(?:command\s+not\s+found|no[_ -]?docker|docker[_ -]?unavailable|daemon[_ -]?unavailable|"
    r"守护进程不可用|没有可用的?\s*docker\s*守护进程|找不到\s*docker\s*命令)|"
    r"(?:网络|网关|镜像仓库|软件源|依赖下载)[^，。；]{0,36}(?:波动|超时|失败|不可达|中断)|"
    r"(?:dubious\s+ownership|unsafe\s+repository|trustanchors|证书链|目录属主|权限限制)"
    r")"
)


def non_model_environment_issues(text: str) -> list[str]:
    """Reject exported evaluation prose that treats infrastructure as capability."""
    if NON_MODEL_ENVIRONMENT_RE.search(str(text or "")):
        return [
            "不得把环境、网络、预装工具或权限故障写入评价描述；"
            "只评价模型自身可控且有证据的行为"
        ]
    return []


def evidence_ledger_sha256(value: object) -> str:
    """Hash the canonical evidence ledger representation.

    The function accepts either the SQLite JSON text or its decoded array so
    callers can use the same binding before and after serialization.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ""
    try:
        rendered = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def trajectory_sha256(path: Path) -> str:
    """Return a stable hash of the authoritative JSONL bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence_qc_receipt(
    record: dict, trajectory_path: Path, *, errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Build a compact, verifiable receipt for a successful evidence QC pass."""
    return {
        "version": 2,
        "record_id": str(record.get("record_id") or ""),
        "evidence_ledger_sha256": evidence_ledger_sha256(record.get("evidence_ledger")),
        "trajectory_sha256": trajectory_sha256(trajectory_path),
        "zero_errors": not errors,
        "zero_warnings": not warnings,
    }


def json_array(value: object, field: str) -> tuple[list[dict], list[str]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [], [f"{field} 必须是有效的 JSON 数组"]
    if not isinstance(value, list):
        return [], [f"{field} 必须是数组"]
    if any(not isinstance(item, dict) for item in value):
        return [], [f"{field} 中的每一项都必须是对象"]
    return value, []


def prose_english_issues(text: str) -> list[str]:
    """Allow exact technical spans, but keep the explanatory prose in Chinese."""
    prose = PROTECTED_PROSE_RE.sub("", text)
    latin_count = 0
    for token in ASCII_TOKEN_RE.findall(prose):
        lowered = token.casefold().rstrip(".,:;")
        looks_like_identifier = (
            any(marker in token for marker in ("_", ".", "/", "\\"))
            or bool(re.search(r"[a-z][A-Z]", token))
        )
        looks_like_acronym = token.isupper() and len(token) <= 8
        if lowered in TECHNICAL_WORDS or looks_like_identifier or looks_like_acronym:
            continue
        latin_count += len(LATIN_RE.findall(token))
    visible_count = len(re.sub(r"\s", "", prose))
    if latin_count > max(8, int(visible_count * 0.04)):
        return ["说明文字中的英文过多；只保留不可翻译的技术标识，其余内容改用中文"]
    return []


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def _sentences(value: str) -> list[str]:
    return [
        _compact(match.group(0).rstrip("。！？!?"))
        for match in SENTENCE_RE.finditer(value)
        if _compact(match.group(0).rstrip("。！？!?"))
    ]


def validate_evidence_structure(record: dict) -> tuple[list[str], list[dict], list[dict]]:
    record_id = str(record.get("record_id") or "<unknown>")
    evidence, errors = json_array(record.get("evidence_ledger"), "evidence_ledger")
    coverage, coverage_errors = json_array(
        record.get("requirement_coverage"), "requirement_coverage"
    )
    errors.extend(coverage_errors)
    uses_evidence_kinds = any("kind" in item for item in evidence)
    ids: set[str] = set()
    by_dimension = {dimension: [] for dimension in DIMENSIONS}
    for index, item in enumerate(evidence, 1):
        prefix = f"{record_id}: 第 {index} 条证据"
        evidence_id = str(item.get("id") or "").strip()
        dimension = str(item.get("dimension") or "").strip()
        source_type = str(item.get("source_type") or "").strip()
        kind = str(item.get("kind") or "").strip()
        claim = str(item.get("claim") or "").strip()
        fact = str(item.get("fact") or "").strip()
        excerpt = str(item.get("excerpt") or "").strip()
        if not evidence_id or evidence_id in ids:
            errors.append(f"{prefix}的 id 为空或重复")
        else:
            ids.add(evidence_id)
        if dimension not in DIMENSIONS:
            errors.append(f"{prefix}的维度无效")
        else:
            by_dimension[dimension].append(item)
        if source_type not in EVIDENCE_SOURCES:
            errors.append(f"{prefix}的来源类型无效")
        if uses_evidence_kinds:
            if kind not in EVIDENCE_KINDS:
                errors.append(f"{prefix}必须提供有效的 kind")
            elif source_type in EVIDENCE_SOURCES and source_type not in EVIDENCE_KIND_SOURCES[kind]:
                errors.append(f"{prefix}的 {source_type} 来源不能证明 {kind} 类型事实")
            if kind == "runtime":
                related_line = item.get("related_line")
                if (
                    not isinstance(related_line, int)
                    or isinstance(related_line, bool)
                    or related_line < 1
                ):
                    errors.append(f"{prefix}的运行结果必须提供 related_line 关联工具调用")
        if not claim or not fact or not excerpt:
            errors.append(f"{prefix}必须包含 claim、fact 和 excerpt")
        if len(CHINESE_RE.findall(fact)) < 6:
            errors.append(f"{prefix}的事实说明必须使用清楚的中文")
        if len(excerpt) > 1200:
            errors.append(f"{prefix}的原文摘录过长")
        if source_type == "trajectory":
            if not isinstance(item.get("line"), int) or isinstance(item.get("line"), bool) or int(item.get("line", 0)) < 1:
                errors.append(f"{prefix}必须提供轨迹中的一开始行号")
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_sha256") or "")):
                errors.append(f"{prefix}必须提供轨迹原始行的校验值")
        elif source_type == "workspace":
            path = str(item.get("path") or "").strip()
            if not path or Path(path).is_absolute() or ".." in Path(path).parts:
                errors.append(f"{prefix}必须提供工作区内的相对文件路径")
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_sha256") or "")):
                errors.append(f"{prefix}必须提供文件校验值")
        elif source_type == "prompt":
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_sha256") or "")):
                errors.append(f"{prefix}必须提供原始要求校验值")

    for dimension in DIMENSIONS:
        description = str(record.get(f"{dimension}_description") or "")
        dimension_claims = {_compact(str(item.get("claim") or "")) for item in by_dimension[dimension]}
        if not by_dimension[dimension]:
            errors.append(f"{record_id}: {dimension} 缺少独立证据")
            continue
        for sentence in _sentences(description):
            if sentence not in dimension_claims:
                errors.append(
                    f"{record_id}: {dimension} 描述中的句子没有绑定证据：{sentence[:48]}"
                )
        if dimension in {"delivery", "planning", "reasoning", "execution"} and not any(
            str(item.get("source_type")) in {"trajectory", "workspace"}
            for item in by_dimension[dimension]
        ):
            errors.append(f"{record_id}: {dimension} 不能只用原始要求作为事实证据")

    prompt = str(record.get("user_prompt") or "")
    for index, item in enumerate(coverage, 1):
        prefix = f"{record_id}: 第 {index} 条需求覆盖"
        requirement = str(item.get("requirement") or "").strip()
        status = str(item.get("status") or "").strip()
        evidence_ids = item.get("evidence_ids")
        if not requirement or _compact(requirement) not in _compact(prompt):
            errors.append(f"{prefix}必须逐字引用本轮原始要求")
        if status not in COVERAGE_STATUSES:
            errors.append(f"{prefix}的状态必须是 met、unmet 或 uncertain")
        if not isinstance(evidence_ids, list) or any(not isinstance(value, str) for value in evidence_ids):
            errors.append(f"{prefix}的 evidence_ids 必须是字符串数组")
            continue
        unknown = sorted(set(evidence_ids) - ids)
        if unknown:
            errors.append(f"{prefix}引用了不存在的证据：{', '.join(unknown)}")
        if status != "uncertain" and not evidence_ids:
            errors.append(f"{prefix}必须引用证据")
    if not coverage:
        errors.append(f"{record_id}: 缺少需求覆盖表")
    if any(str(item.get("status")) != "met" for item in coverage):
        for dimension in ("delivery", "instruction"):
            if record.get(f"{dimension}_score") == 5:
                errors.append(f"{record_id}: 需求未全部确认满足时，{dimension} 不能为 5 分")
    return errors, evidence, coverage


def _find_trajectory(root: Path, name: str) -> Path:
    if not root.is_dir():
        raise ValueError(f"权威轨迹目录不存在：{root}")
    matches = sorted(
        path.resolve() for path in root.rglob("*.jsonl")
        if path.is_file() and (path.name == name or path.name.endswith("_" + name))
    )
    if len(matches) != 1:
        raise ValueError("找不到唯一的权威轨迹文件")
    return matches[0]


def find_authoritative_trajectory(root: Path, name: str) -> Path:
    """Public wrapper used when a QC pass records the trajectory receipt."""
    return _find_trajectory(root, name)


def _event_prompt_id(event: dict) -> str:
    return str(event.get("promptId") or event.get("prompt_id") or "")


def _event_session_id(event: dict) -> str:
    return str(event.get("sessionId") or event.get("session_id") or "")


def _event_user_text(event: dict) -> str:
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _turn_line_range(lines: list[bytes], record: dict) -> tuple[int, int] | None:
    session_id = str(record.get("session_id") or "")
    raw_turn_id = str(record.get("raw_turn_id") or record.get("turn_id") or "")
    raw_prompt = str(record.get("raw_user_prompt") or record.get("user_prompt") or "")
    user_events: list[tuple[int, dict]] = []
    for line_no, raw in enumerate(lines, 1):
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if _event_session_id(event) not in {"", session_id}:
            continue
        if _event_prompt_id(event) and _event_user_text(event):
            user_events.append((line_no, event))
    matches = [
        index for index, (_line_no, event) in enumerate(user_events)
        if _event_prompt_id(event) == raw_turn_id and _event_user_text(event) == raw_prompt
    ]
    if len(matches) != 1:
        return None
    event_index = matches[0]
    start = user_events[event_index][0]
    end = user_events[event_index + 1][0] - 1 if event_index + 1 < len(user_events) else len(lines)
    return start, end


def verify_evidence_sources(
    record: dict, *, question_folder: Path, trajectory_root: Path
) -> list[str]:
    errors, evidence, _coverage = validate_evidence_structure(record)
    if errors:
        return errors
    record_id = str(record.get("record_id") or "<unknown>")
    trajectory_lines: list[bytes] | None = None
    turn_range: tuple[int, int] | None = None
    prompt = str(record.get("user_prompt") or "")
    for item in evidence:
        evidence_id = str(item["id"])
        source_type = str(item["source_type"])
        excerpt = str(item["excerpt"])
        expected_hash = str(item["source_sha256"])
        if source_type == "prompt":
            if excerpt not in prompt:
                errors.append(f"{record_id}: 证据 {evidence_id} 的要求原文不存在")
            if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != expected_hash:
                errors.append(f"{record_id}: 证据 {evidence_id} 的要求校验值不一致")
            continue
        if source_type == "workspace":
            root = question_folder.resolve()
            path = (root / str(item["path"])).resolve()
            if path != root and root not in path.parents:
                errors.append(f"{record_id}: 证据 {evidence_id} 越出题目工作区")
                continue
            if not path.is_file():
                errors.append(f"{record_id}: 证据 {evidence_id} 的文件不存在")
                continue
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                errors.append(f"{record_id}: 证据 {evidence_id} 的文件校验值不一致")
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                content = ""
            if excerpt not in content:
                errors.append(f"{record_id}: 证据 {evidence_id} 的文件摘录不存在")
            continue
        if trajectory_lines is None:
            try:
                trajectory = _find_trajectory(
                    trajectory_root.resolve(), str(record.get("trajectory_file") or "")
                )
                trajectory_lines = trajectory.read_bytes().splitlines()
                turn_range = _turn_line_range(trajectory_lines, record)
                if turn_range is None:
                    errors.append(f"{record_id}: 无法在权威轨迹中唯一定位当前轮次边界")
            except (OSError, ValueError) as exc:
                errors.append(f"{record_id}: {exc}")
                trajectory_lines = []
        line_no = int(item["line"])
        if line_no > len(trajectory_lines):
            errors.append(f"{record_id}: 证据 {evidence_id} 的轨迹行号越界")
            continue
        if turn_range is None or not turn_range[0] <= line_no <= turn_range[1]:
            errors.append(f"{record_id}: 证据 {evidence_id} 不属于当前轮次范围")
            continue
        raw = trajectory_lines[line_no - 1]
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            errors.append(f"{record_id}: 证据 {evidence_id} 的轨迹校验值不一致")
        if excerpt not in raw.decode("utf-8", errors="replace"):
            errors.append(f"{record_id}: 证据 {evidence_id} 的轨迹摘录不存在")
        kind = str(item.get("kind") or "")
        if kind not in {"process", "runtime"}:
            continue
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"{record_id}: 证据 {evidence_id} 的轨迹行不是有效事件")
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        if kind == "process":
            if not any(
                isinstance(block, dict) and block.get("type") in {"tool_use", "thinking"}
                for block in blocks
            ):
                errors.append(f"{record_id}: 过程证据 {evidence_id} 必须引用工具调用或思考事件")
            continue
        result_ids = {
            str(block.get("tool_use_id") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_result"
            and str(block.get("tool_use_id") or "")
        }
        if not result_ids:
            errors.append(f"{record_id}: 运行证据 {evidence_id} 必须引用工具结果事件")
            continue
        related_line = item.get("related_line")
        if not isinstance(related_line, int) or isinstance(related_line, bool):
            continue
        if (
            related_line >= line_no
            or related_line > len(trajectory_lines)
            or turn_range is None
            or not turn_range[0] <= related_line <= turn_range[1]
        ):
            errors.append(f"{record_id}: 运行证据 {evidence_id} 关联的工具调用不在当前结果之前")
            continue
        try:
            related_event = json.loads(trajectory_lines[related_line - 1])
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"{record_id}: 运行证据 {evidence_id} 关联行不是有效事件")
            continue
        related_message = related_event.get("message") if isinstance(related_event, dict) else None
        related_content = related_message.get("content") if isinstance(related_message, dict) else None
        related_blocks = related_content if isinstance(related_content, list) else []
        call_ids = {
            str(block.get("id") or "")
            for block in related_blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
            and str(block.get("id") or "")
        }
        if not call_ids or not (call_ids & result_ids):
            errors.append(f"{record_id}: 运行证据 {evidence_id} 的工具结果与关联调用不匹配")
    return errors


def mask_technical_text(value: str) -> str:
    value = TECHNICAL_SPAN_RE.sub("", value)
    return "".join(CHINESE_RE.findall(value))


def _longest_match(left: str, right: str) -> int:
    return SequenceMatcher(None, left, right).find_longest_match().size


def history_matches(
    connection: sqlite3.Connection, record: dict, *, exclude_record_id: str | None = None
) -> list[dict]:
    columns = ",".join(f"{dimension}_description" for dimension in DIMENSIONS)
    rows = connection.execute(
        f"SELECT record_id,{columns} FROM records WHERE record_id!=?",
        (exclude_record_id or str(record.get("record_id") or ""),),
    ).fetchall()
    imported: list[dict] = []
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='description_history'"
    ).fetchone()
    if table:
        imported = [dict(row) for row in connection.execute(
            "SELECT source_record_id AS record_id,dimension,description,source_node "
            "FROM description_history"
        ).fetchall()]
    matches: list[dict] = []
    for dimension in DIMENSIONS:
        current = str(record.get(f"{dimension}_description") or "")
        normalized = mask_technical_text(current)
        if len(normalized) < 24:
            continue
        candidates = [
            {
                "record_id": str(row["record_id"]),
                "description": str(row[f"{dimension}_description"] or ""),
                "source_node": "本机",
            }
            for row in rows
        ]
        candidates.extend(
            {
                "record_id": str(row["record_id"]),
                "description": str(row["description"]),
                "source_node": str(row.get("source_node") or "远程节点"),
            }
            for row in imported if row["dimension"] == dimension
        )
        for candidate in candidates:
            if candidate["record_id"] == (exclude_record_id or str(record.get("record_id") or "")):
                continue
            other = mask_technical_text(candidate["description"])
            if len(other) < 24:
                continue
            ratio = SequenceMatcher(None, normalized, other).ratio()
            longest = _longest_match(normalized, other)
            same_opening = normalized[:12] == other[:12]
            if longest >= 18 or (ratio >= 0.82 and min(len(normalized), len(other)) >= 32) or (same_opening and ratio >= 0.68):
                matches.append({
                    "dimension": dimension,
                    "record_id": candidate["record_id"],
                    "source_node": candidate["source_node"],
                    "similarity": round(ratio, 3),
                    "longest_fragment": longest,
                    "description": candidate["description"],
                })
    return sorted(matches, key=lambda item: (-item["similarity"], -item["longest_fragment"]))


def evidence_fingerprint(value: str) -> str:
    return hashlib.sha256(mask_technical_text(value).encode("utf-8")).hexdigest()
