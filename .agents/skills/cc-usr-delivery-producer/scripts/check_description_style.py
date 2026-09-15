#!/usr/bin/env python3
"""Block formulaic or visibly machine-assembled delivery descriptions."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from difflib import SequenceMatcher
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402
from tools.delivery_quality import (  # noqa: E402
    history_matches, non_model_environment_issues, prose_english_issues,
)


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
ENGLISH_EVALUATOR_LABEL_RE = re.compile(
    r"(?:^|[。；;\s])(?:Rationale|Reasoning|Summary|Overall|Conclusion)\s*[:：]",
    re.IGNORECASE,
)
CHINESE_ORDINAL_RE = re.compile(
    r"第[零〇一二两三四五六七八九十百千万]+"
    r"(?=(?:至第?[零〇一二两三四五六七八九十百千万]+)?"
    r"(?:行|步|次|轮|个|处|阶段|条|项|章|节|题|页|列|点))"
)
STYLE_FIELDS = tuple(f"{prefix}_description" for prefix in PREFIXES)
MIN_DESCRIPTION_CHINESE = 45
MAX_DESCRIPTION_CHARS = 420
NON_MAX_EVIDENCE_RE = re.compile(
    r"(?:第\d+(?:至第?\d+)?(?:步|次|轮|行)|"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z][A-Za-z0-9_.-]+\.(?:go|java|py|ts|tsx|js|jsx|sql|md)|"
    r"文件|模块|函数|方法|类|配置|命令|脚本|测试|用例|接口|页面|"
    r"数据库|数据表|仓储|服务|构建|编译|安装|迁移|事务|路由|规则|存储|查询|发布|调度|回执|状态机|依赖)"
)
NON_MAX_IMPACT_RE = re.compile(
    r"(?:导致|使得|造成|增加|延长|无法|不能|未能|风险|返工|中断|冲突|偏差|"
    r"遗漏|落空|暴露|限制|不一致|重复|仍需|影响|缺少|缺失|不足|成本|代价|耗时|误判|重做|多轮|不存在|未完成|没有落地)"
)
PERFECT_SCORE_CONTRADICTION_RE = re.compile(
    r"(?:仍然?|依然|还)(?:存在|有|未|没有)|"
    r"(?:未实现|未完成|未覆盖|未遵守|违反|遗漏了|漏掉了|缺少|缺失)"
    r"[^，。；]{0,18}(?:要求|约束|功能|场景|验证|处理|能力)"
)
PERFECT_ACTION_RE = re.compile(r"(?:核对|比对|断言|检查|读取|查询|执行|运行|复验|验证|构建|编译|测试)")
PERFECT_RESULT_RE = re.compile(r"(?:通过|成功|正常|一致|返回|结束|拒绝|保留|生效|恢复|完成|符合|零失败|零错误)")
PLANNING_EVIDENCE_RE = re.compile(r"(?:计划|规划|拆解|步骤|阶段|顺序|状态追踪|进度|收尾|歧义|节点|安排)")
REASONING_PREMISE_RE = re.compile(
    r"(?:假设|前提|推断|推导|误判|误以为|错误地(?:认为|认定|假设)|没有解释|未说明|"
    r"没有区分|未区分|遗漏[^，。；]{0,12}(?:分支|条件|先后关系|边界)|(?:把|将)[^，。；]{0,48}(?:当作|当成))"
)
EXECUTION_TOOL_ACTION_RE = re.compile(
    r"(?:(?:使用|调用)[^，。；]{0,20}(?:工具|命令|脚本)|"
    r"(?:读取|写入|修改|编辑|检索|搜索|执行|运行|重试|重跑|复验|验证|启动)"
    r"[^，。；]{0,16}(?:工具|命令|脚本|文件|构建|测试))"
)
EXECUTION_FOLLOWUP_RE = re.compile(
    r"(?:随后|之后|紧接着|又|再|直到|才|连续|重复|重试|重跑|复验|补充|修正|修改|覆盖|作废|转入后台)"
)
PRECISE_LOCATION_RE = re.compile(
    r"(?:第\d+(?:至第?\d+)?(?:步|次|轮|行)|(?:目录|子目录)[^，。；]{0,30}(?:文件|模块)|"
    r"名为[“\"'][^”\"']+[”\"']?(?:的)?(?:文件|源文件|测试|用例)|"
    r"[^，。；]{1,24}(?:函数|方法|用例|断言|命令)(?:中|处|返回|报告|失败|通过|验证|检查))"
)
GENERIC_LOCATION_RE = re.compile(
    r"(?:启动文件|存储文件|网页接口文件|接口文件|服务文件|接口测试文件|服务测试文件|某次测试|某个文件|相关文件|命令目录|服务目录|主入口源文件|内部目录|存储源文件|结算目录)"
)
VAGUE_LIST_RE = re.compile(r"(?:测试|检查|任务|步骤|验证)(?:项)?清单")
LIST_ITEM_RE = re.compile(r"[^，。；]{2,}(?:、|，)[^，。；]{2,}(?:、|，)[^，。；]{2,}")
VAGUE_FAILURE_RE = re.compile(r"(?:对象|服务|流程|构建|测试|命令)(?:创建|执行|运行|启动)?(?:失败|中断)")
EXACT_FAILURE_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_.]*(?:Exception|Error)|TS\d{3,5}|exit(?:ed)?\s+(?:code\s+)?\d+|"
    r"tests?\s+\d+|fail(?:ed)?\s+\d+|mvn(?:\s+-[A-Za-z]+)*\s+test|node\s+--test|tsc\s+--noEmit)"
)
OBJECTIVE_OUTCOME_RE = re.compile(
    r"(?:构建|编译|测试|验证|运行|启动|请求|响应|接口|状态|数据|记录|功能|交付|断言|发布|迁移|事务|回执|任务)"
    r"[^，。；]{0,24}(?:失败|中断|异常|错误|冲突|重复|丢失|倒退|未通过|未完成|未执行|无法|不能|缺失|遗漏|不一致|被覆盖|被取消|仍未)|"
    r"(?:导致|使得|造成)[^，。；]{0,24}(?:失败|中断|异常|错误|冲突|重复|丢失|倒退|未通过|未完成|无法|不能|缺失|遗漏|不一致|被覆盖|被取消)"
)
VAGUE_PERFECT_CLAIM_RE = re.compile(
    r"(?:测试(?:最终)?(?:全部)?通过|均有(?:具体)?断言覆盖|没有遗漏(?:主要)?交付项|(?:明确|全部|所有)?要求均已覆盖|全部约束均已落实)"
)
REASONING_WRONG_ATTRIBUTION_RE = re.compile(
    r"(?:(?:环境|网络|外部依赖)[^，。；]{0,16}(?:限制|故障|不可用|没有|缺失)|(?:发现|补齐)[^，。；]{0,8}(?:较晚|太晚|过晚))"
)


def load_records(paths: list[Path]) -> list[tuple[Path, dict]]:
    records: list[tuple[Path, dict]] = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: cannot read JSON: {exc}") from exc
        if isinstance(value, dict) and isinstance(value.get("records"), list):
            values = value["records"]
        else:
            values = value if isinstance(value, list) else [value]
        for index, item in enumerate(values, 1):
            if not isinstance(item, dict):
                raise ValueError(f"{path}: item {index} is not an object")
            if isinstance(item.get("changes"), dict):
                item = item["changes"]
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
        if ENGLISH_EVALUATOR_LABEL_RE.search(text):
            errors.append(f"{path} record {record_index}: {field} contains an English evaluator label")
        if CHINESE_ORDINAL_RE.search(text):
            errors.append(
                f"{path} record {record_index}: {field} must use Arabic digits for ordinals, for example 第80行"
            )
        for issue in prose_english_issues(text):
            errors.append(f"{path} record {record_index}: {field} {issue}")
        for issue in non_model_environment_issues(text):
            errors.append(f"{path} record {record_index}: {field} {issue}")
        chinese_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
        if chinese_count < MIN_DESCRIPTION_CHINESE:
            errors.append(
                f"{path} record {record_index}: {field} must contain at least "
                f"{MIN_DESCRIPTION_CHINESE} Chinese characters"
            )
        if "\n" in text or "\r" in text:
            errors.append(f"{path} record {record_index}: {field} must be a single paragraph")
        if len(text) > MAX_DESCRIPTION_CHARS:
            errors.append(
                f"{path} record {record_index}: {field} must not exceed "
                f"{MAX_DESCRIPTION_CHARS} characters"
            )
        if text.count("；") + text.count(";") > 1:
            errors.append(f"{path} record {record_index}: {field} has repeated semicolon joins")
        if text.count("，") >= 10 and text.count("、") >= 3:
            errors.append(f"{path} record {record_index}: {field} reads as a dense inventory")
        score = record.get(field.replace("_description", "_score"))
        if score == 5:
            if PERFECT_SCORE_CONTRADICTION_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} admits an unmet requirement with a perfect score")
            if not PERFECT_ACTION_RE.search(text) or not PERFECT_RESULT_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} lacks a concrete verification action and result for a perfect score")
            if VAGUE_PERFECT_CLAIM_RE.search(text) and not PRECISE_LOCATION_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} uses a vague perfect-score claim without a precise source location")
        if isinstance(score, int) and not isinstance(score, bool) and score < 5:
            if not NON_MAX_EVIDENCE_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} lacks a concrete evidence location")
            if not NON_MAX_IMPACT_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} lacks an objective consequence")
            if GENERIC_LOCATION_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} uses a generic file or test location without a precise locator")
            if field == "planning_description" and VAGUE_LIST_RE.search(text) and not LIST_ITEM_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} cites a checklist without naming its items")
            if re.search(r"(?:耗时|成本|代价|返工|等待(?!者))", text) and not OBJECTIVE_OUTCOME_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} gives only time or effort cost, not an objective engineering consequence")
            if field == "planning_description" and not PLANNING_EVIDENCE_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} lacks planning or state-tracking evidence")
            if field == "reasoning_description" and not REASONING_PREMISE_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} lacks a false premise, inference, or omitted branch")
            if field == "reasoning_description" and REASONING_WRONG_ATTRIBUTION_RE.search(text):
                errors.append(f"{path} record {record_index}: {field} attributes a deduction to environment or discovery timing")
            if field == "execution_description":
                if not EXECUTION_TOOL_ACTION_RE.search(text):
                    errors.append(f"{path} record {record_index}: {field} lacks a concrete tool action")
                if not EXECUTION_FOLLOWUP_RE.search(text):
                    errors.append(f"{path} record {record_index}: {field} lacks retry, recovery, or verification sequence evidence")
                if VAGUE_FAILURE_RE.search(text) and not EXACT_FAILURE_RE.search(text):
                    errors.append(f"{path} record {record_index}: {field} reports a failure without an exact command, error, or test result")

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
        for issue in non_model_environment_issues(text):
            errors.append(f"{path} record {record_index}: other_issues {issue}")
        if CHINESE_ORDINAL_RE.search(text):
            errors.append(
                f"{path} record {record_index}: other_issues must use Arabic digits for ordinals, for example 第80行"
            )
        if PLACEHOLDER_RE.search(text):
            errors.append(f"{path} record {record_index}: other_issues contains scaffolding or an arrow")
        if ENGLISH_EVALUATOR_LABEL_RE.search(text):
            errors.append(f"{path} record {record_index}: other_issues contains an English evaluator label")
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
    parser.add_argument("--db", type=Path, help="compare with every description already stored in SQLite")
    args = parser.parse_args()
    try:
        records = load_records(args.input)
    except ValueError as exc:
        print(f"STYLE GATE BLOCKED: {exc}", file=sys.stderr)
        return 1
    errors: list[str] = []
    opener_by_field: dict[str, dict[str, list[str]]] = {}
    descriptions_by_field: dict[str, list[tuple[str, str]]] = {}
    for record_index, (path, record) in enumerate(records, 1):
        errors.extend(check_record(path, record, record_index))
        for field in STYLE_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                opener_by_field.setdefault(field, {}).setdefault(first_words(value), []).append(str(path))
                descriptions_by_field.setdefault(field, []).append((str(path), value.strip()))
    for field, openers in opener_by_field.items():
        for opener, paths in openers.items():
            if opener and len(paths) > 1:
                errors.append(f"{field}: opener {opener!r} is repeated across {len(paths)} pending record(s)")
    for field, entries in descriptions_by_field.items():
        for left in range(len(entries)):
            left_path, left_text = entries[left]
            normalized_left = re.sub(r"[\s，。；：、,.!?！？;:'\"“”‘’（）()]", "", left_text)
            fragments = {
                normalized_left[index:index + 18]
                for index in range(max(0, len(normalized_left) - 17))
            }
            for right in range(left + 1, len(entries)):
                right_path, right_text = entries[right]
                normalized_right = re.sub(r"[\s，。；：、,.!?！？;:'\"“”‘’（）()]", "", right_text)
                duplicate = next((part for part in fragments if part in normalized_right), None)
                if duplicate:
                    errors.append(
                        f"{field}: exact long fragment {duplicate!r} is repeated across {left_path} and {right_path}"
                    )
    if args.db:
        try:
            with connect(args.db.resolve()) as connection:
                for path, record in records:
                    for match in history_matches(
                        connection, record, exclude_record_id=str(record.get("record_id") or "")
                    ):
                        errors.append(
                            f"{path}: {match['dimension']} 与 {match['source_node']}的 "
                            f"{match['record_id']} 过于相似（相似度 {match['similarity']:.3f}，"
                            f"最长重复片段 {match['longest_fragment']} 字）"
                        )
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"历史描述读取失败：{exc}")
    if errors:
        print("STYLE GATE BLOCKED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"STYLE GATE PASSED: checked {len(records)} record(s), five descriptions and non-empty other_issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
