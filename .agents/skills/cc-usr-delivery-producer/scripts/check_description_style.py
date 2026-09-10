#!/usr/bin/env python3
"""Block formulaic or visibly machine-assembled delivery descriptions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path


PREFIXES = ("delivery", "instruction", "planning", "reasoning", "execution")
FORBIDDEN = (
    "最终工作区包含",
    "逐条核对后没有发现",
    "核心取舍是清楚的",
    "本轮主要是",
    "本轮按",
    "顺序基本合理",
    "这些修正体现了",
    "无法证明",
    "根据轨迹",
    "综合来看",
    "经检查",
    "唯一问题",
    "过程：",
    "产物：",
    "事实：",
    "结论：",
    "When:",
    "What:",
    "Impact:",
)
TURN_OPENING_RE = re.compile(r"^(?:本轮|这轮|这一轮)")
SELF_REFERENCE_RE = re.compile(
    r"(?:AI|Codex|Claude|ChatGPT)\s*(?:认为|分析|自动|生成|评分|撰写)"
    r"|(?:作为|身为)\s*(?:AI|Codex|Claude|ChatGPT|模型|智能体)"
    r"|(?:由|使用|借助)\s*(?:AI|Codex|Claude|ChatGPT|模型)\s*(?:生成|撰写|创建)"
    r"|(?:自动生成|基于轨迹生成|根据轨迹生成|本评分由)"
    r"|(?:评分|打分|评测|质检|评价者|审核人员|生成过程)"
    r"|(?:轨迹|日志)(?:显示|表明|可见)"
    r"|(?:模型|智能体|助手)(?:的)?(?:表现|回答|输出|生成过程)",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]|=>|→|->")
UNNECESSARY_ENGLISH_RE = re.compile(
    r"(?<![/\\._'\"\w])(?:rationale|overall|generally|basically|summary|conclusion)"
    r"\b(?!\s*[:=])",
    re.IGNORECASE,
)
STYLE_FIELDS = tuple(f"{prefix}_description" for prefix in PREFIXES)


def load_records(paths: list[Path]) -> list[tuple[Path, dict]]:
    records: list[tuple[Path, dict]] = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: cannot read JSON: {exc}") from exc
        values = value if isinstance(value, list) else [value]
        for index, item in enumerate(values, 1):
            if not isinstance(item, dict):
                raise ValueError(f"{path}: item {index} is not an object")
            records.append((path, item))
    return records


def first_words(text: str) -> str:
    normalized = re.sub(r"^[\s\"'“”‘’：:，。；;、]+", "", text)
    return normalized[:12]


def check_record(path: Path, record: dict, record_index: int) -> list[str]:
    errors: list[str] = []
    descriptions: list[str] = []
    for field in STYLE_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path} record {record_index}: {field} is empty")
            continue
        text = value.strip()
        descriptions.append(text)
        for phrase in FORBIDDEN:
            if phrase in text:
                errors.append(f"{path} record {record_index}: {field} contains forbidden phrase {phrase!r}")
        if TURN_OPENING_RE.search(text):
            errors.append(f"{path} record {record_index}: {field} uses a turn-recap opening")
        if SELF_REFERENCE_RE.search(text):
            errors.append(f"{path} record {record_index}: {field} exposes generation or evaluator language")
        if PLACEHOLDER_RE.search(text):
            errors.append(f"{path} record {record_index}: {field} contains scaffolding or an arrow")
        prose = re.sub(r"```.*?```|`[^`]*`|https?://\S+", " ", text, flags=re.DOTALL)
        if UNNECESSARY_ENGLISH_RE.search(prose):
            errors.append(f"{path} record {record_index}: {field} contains unnecessary English evaluator wording")
        if text.count("；") + text.count(";") > 1:
            errors.append(f"{path} record {record_index}: {field} has repeated semicolon joins")
        if text.count("，") >= 10 and text.count("、") >= 3:
            errors.append(f"{path} record {record_index}: {field} reads as a dense inventory")

    openers = [first_words(text) for text in descriptions]
    for left in range(len(openers)):
        for right in range(left + 1, len(openers)):
            if openers[left] and openers[left] == openers[right]:
                errors.append(f"{path} record {record_index}: descriptions {left + 1} and {right + 1} share an opener")
    for left in range(len(descriptions)):
        for right in range(left + 1, len(descriptions)):
            similarity = SequenceMatcher(None, descriptions[left], descriptions[right]).ratio()
            if similarity >= 0.86:
                errors.append(f"{path} record {record_index}: descriptions {left + 1} and {right + 1} are mechanically similar ({similarity:.2f})")

    other_issues = record.get("other_issues")
    if isinstance(other_issues, str) and other_issues.strip():
        text = other_issues.strip()
        for phrase in FORBIDDEN:
            if phrase in text:
                errors.append(f"{path} record {record_index}: other_issues contains forbidden phrase {phrase!r}")
        if TURN_OPENING_RE.search(text):
            errors.append(f"{path} record {record_index}: other_issues uses a turn-recap opening")
        if SELF_REFERENCE_RE.search(text):
            errors.append(f"{path} record {record_index}: other_issues exposes generation or evaluator language")
        if PLACEHOLDER_RE.search(text):
            errors.append(f"{path} record {record_index}: other_issues contains scaffolding or an arrow")
        prose = re.sub(r"```.*?```|`[^`]*`|https?://\S+", " ", text, flags=re.DOTALL)
        if UNNECESSARY_ENGLISH_RE.search(prose):
            errors.append(f"{path} record {record_index}: other_issues contains unnecessary English evaluator wording")
        if len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)) < 12:
            errors.append(f"{path} record {record_index}: other_issues must use complete Chinese prose")
        if text.count("；") + text.count(";") > 1:
            errors.append(f"{path} record {record_index}: other_issues has repeated semicolon joins")
        if text.count("，") >= 10 and text.count("、") >= 3:
            errors.append(f"{path} record {record_index}: other_issues reads as a dense inventory")
        normalized_other = re.sub(r"\s+", "", text)
        for description in descriptions:
            normalized_description = re.sub(r"\s+", "", description)
            if normalized_other == normalized_description or SequenceMatcher(
                None, normalized_other, normalized_description,
            ).ratio() >= 0.9:
                errors.append(
                    f"{path} record {record_index}: other_issues repeats a five-dimension description"
                )
                break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject formulaic delivery descriptions before SQLite insertion")
    parser.add_argument("--input", nargs="+", type=Path, required=True, help="one or more temporary record JSON files")
    args = parser.parse_args()
    try:
        records = load_records(args.input)
    except ValueError as exc:
        print(f"STYLE GATE BLOCKED: {exc}", file=sys.stderr)
        return 1
    errors: list[str] = []
    opener_by_field: dict[str, dict[str, list[str]]] = {}
    for record_index, (path, record) in enumerate(records, 1):
        errors.extend(check_record(path, record, record_index))
        for field in STYLE_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                opener_by_field.setdefault(field, {}).setdefault(first_words(value), []).append(str(path))
    for field, openers in opener_by_field.items():
        for opener, paths in openers.items():
            if opener and len(paths) > 1:
                errors.append(f"{field}: opener {opener!r} is repeated across {len(paths)} pending record(s)")
    if errors:
        print("STYLE GATE BLOCKED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"STYLE GATE PASSED: checked {len(records)} record(s), five descriptions and non-empty other_issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
