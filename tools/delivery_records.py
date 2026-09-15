#!/usr/bin/env python3
"""Shared structural contract for SQLite-backed delivery records."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tools.delivery_quality import (
    evidence_ledger_sha256, non_model_environment_issues, prose_english_issues,
    validate_evidence_structure,
)


try:
    PROJECT_TIMEZONE = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows Python installations may not ship the IANA timezone database.
    PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


TASK_TYPES = {
    "0-1 代码生成", "Feature 迭代", "Bug 修复", "代码理解",
    "代码重构", "工程化", "代码测试",
}
DIFFICULTIES = {"简单", "中等", "困难", "地狱"}
REPRODUCIBILITY = {
    "无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境",
}
HARNESSES = {"Claude Code", "Codex CLI"}
OPERATING_SYSTEMS = {"MacOS/Linux", "Windows"}
SCORE_PREFIXES = ("delivery", "instruction", "planning", "reasoning", "execution")
CURRENT_QUALITY_CONTRACT_VERSION = 2
SNAPSHOT_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/commit/[0-9a-fA-F]{40}$"
)
NUMERIC_HARNESS_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")

EXPORT_HEADERS = [
    "User Prompt", "SessionID", "TurnID/PromptID", "当前对话轮次排序",
    "初始环境快照", "轨迹文件", "环境可复现等级", "Harness", "Harness 版本", "操作系统",
    "任务类型", "任务难度", "语言/框架", "交付完整性", "交付完整性 - 描述",
    "指令遵循", "指令遵循 - 描述", "任务规划", "任务规划 - 描述",
    "推理能力", "推理能力 - 描述", "执行能力", "执行能力 - 描述",
    "其他问题", "提交人", "提交时间", "父记录", "审核备注",
]
EXPORT_KEYS = [
    "user_prompt", "session_id", "turn_id", "turn_no", "initial_snapshot",
    "trajectory_file", "reproducibility", "harness", "harness_version",
    "operating_system", "task_type", "difficulty", "languages", "delivery_score",
    "delivery_description", "instruction_score", "instruction_description",
    "planning_score", "planning_description", "reasoning_score",
    "reasoning_description", "execution_score", "execution_description",
    "other_issues", "submitter", "submitted_at", "parent_record", "delivery_qc_note",
]
SCORE_KEYS = {f"{prefix}_score" for prefix in SCORE_PREFIXES}
MIN_DESCRIPTION_CHINESE = 45
MAX_DESCRIPTION_CHARS = 420


def late_delivery_allowed() -> bool:
    """Return whether an operator explicitly enabled historical backfill."""
    return os.environ.get("CC_DELIVERY_ALLOW_LATE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
DESCRIPTION_META_PATTERNS = (
    re.compile(r"(?i)(?:^|[^A-Za-z])AI\s*(?:分析|生成|评分|撰写|认为)"),
    re.compile(r"(?:由|作为|本)\s*(?:AI|Codex)\b", re.IGNORECASE),
    re.compile(r"Codex\s*(?:分析|生成|评分|撰写|认为)", re.IGNORECASE),
    re.compile(r"(?:自动生成|基于轨迹生成|根据轨迹生成|本评分由)"),
    re.compile(r"(?:评分|打分|评测|质检|评价者|审核人员|生成过程)"),
    re.compile(r"(?:轨迹|日志)(?:显示|表明|可见)"),
    re.compile(r"(?:模型|智能体|助手)(?:的)?(?:表现|回答|输出|生成过程)"),
    re.compile(r"(?:作为|身为)\s*(?:AI|Codex|Claude|ChatGPT|模型|智能体)", re.IGNORECASE),
    re.compile(r"(?:由|使用|借助)\s*(?:AI|Codex|Claude|ChatGPT|模型)\s*(?:生成|撰写|创建)", re.IGNORECASE),
)
UNNECESSARY_ENGLISH_RE = re.compile(
    r"(?<![/\\._'\"\w])(?:rationale|overall|generally|basically|summary|conclusion)"
    r"\b(?!\s*[:=])",
    re.IGNORECASE,
)
DESCRIPTION_TEMPLATE_PATTERNS = (
    re.compile(r"(?:^|[\s；;。])(?:When|What|Impact)\s*[:：]", re.IGNORECASE),
    re.compile(r"^\s*(?:过程|产物)\s*[:：]"),
    re.compile(r"^\s*对(?:过程|产物)不满意的原因\s*[:：]"),
    re.compile(r"^\s*(?:经检查|通过检查|根据轨迹|从轨迹看|结合轨迹|综合来看|总体来看|本次任务中|本轮任务中|总体而言|综上所述|值得注意的是|需要指出的是)[，,:：]?"),
    re.compile(r"[→➡]"),
    re.compile(r"【(?:第几步|哪个环节|具体行为|什么后果|根因|正确做法|哪个文件|哪个功能)】"),
)
NON_MAX_EVIDENCE_RE = re.compile(
    r"(?:第\d+(?:至第?\d+)?(?:步|次|轮|行)|"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"[A-Za-z][A-Za-z0-9_.-]+\.(?:go|java|py|ts|tsx|js|jsx|rs|sql|md)|"
    r"文件|模块|函数|方法|类|配置|命令|脚本|测试|用例|接口|页面|"
    r"数据库|数据表|仓储|服务|构建|编译|安装|迁移|事务|路由|规则|存储|查询|发布|调度|回执|状态机|依赖)"
 )
NON_MAX_IMPACT_RE = re.compile(
    r"(?:导致|使得|造成|增加|延长|无法|不能|未能|风险|返工|中断|冲突|偏差|遗漏|落空|"
    r"暴露|限制|不一致|重复|仍需|影响|缺少|缺失|不足|成本|代价|耗时|误判|重做|多轮|不存在|未完成|没有落地)"
 )
PRECISE_LOCATION_RE = re.compile(
    r"(?:第\d+(?:至第?\d+)?(?:步|次|轮|行)|"
    r"(?:目录|子目录)[^，。；]{0,30}(?:文件|模块)|"
    r"名为[“\"'][^”\"']+[”\"']?(?:的)?(?:文件|源文件|测试|用例)|"
    r"[^，。；]{1,24}(?:函数|方法|用例|断言|命令)(?:中|处|返回|报告|失败|通过|验证|检查))"
 )
GENERIC_LOCATION_RE = re.compile(
    r"(?:启动文件|存储文件|网页接口文件|接口文件|服务文件|接口测试文件|服务测试文件|"
    r"某次测试|某个文件|相关文件|命令目录|服务目录|主入口源文件|内部目录|存储源文件|结算目录)"
 )
VAGUE_LIST_RE = re.compile(r"(?:测试|检查|任务|步骤|验证)(?:项)?清单")
LIST_ITEM_RE = re.compile(r"[^，。；]{2,}(?:、|，)[^，。；]{2,}(?:、|，)[^，。；]{2,}")
VAGUE_FAILURE_RE = re.compile(r"(?:对象|服务|流程|构建|测试|命令)(?:创建|执行|运行|启动)?(?:失败|中断)")
EXACT_FAILURE_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_.]*(?:Exception|Error)|TS\d{3,5}|"
    r"exit(?:ed)?\s+(?:code\s+)?\d+|tests?\s+\d+|fail(?:ed)?\s+\d+|"
    r"mvn(?:\s+-[A-Za-z]+)*\s+test|node\s+--test|tsc\s+--noEmit)"
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
PERFECT_SCORE_CONTRADICTION_RE = re.compile(
    r"(?:仍然?|依然|还)(?:存在|有|未|没有)|(?:未实现|未完成|未覆盖|未遵守|违反|遗漏了|漏掉了|缺少|缺失)[^，。；]{0,18}(?:要求|约束|功能|场景|验证|处理|能力)"
 )
PERFECT_ACTION_RE = re.compile(r"(?:核对|比对|断言|检查|读取|查询|执行|运行|复验|验证|构建|编译|测试)")
PERFECT_RESULT_RE = re.compile(r"(?:通过|成功|正常|一致|返回|结束|拒绝|保留|生效|恢复|完成|符合|零失败|零错误)")
PLANNING_EVIDENCE_RE = re.compile(r"(?:计划|规划|拆解|步骤|阶段|顺序|状态追踪|进度|收尾|歧义|节点|安排)")
REASONING_PREMISE_RE = re.compile(
    r"(?:假设|前提|推断|推导|误判|误以为|错误地(?:认为|认定|假设)|没有解释|未说明|没有区分|未区分|遗漏[^，。；]{0,12}(?:分支|条件|先后关系|边界)|(?:把|将)[^，。；]{0,48}(?:当作|当成))"
 )
EXECUTION_TOOL_ACTION_RE = re.compile(
    r"(?:(?:使用|调用)[^，。；]{0,20}(?:工具|命令|脚本)|第\d+次[^，。；]{0,30}(?:读取|写入|修改|编辑|检索|搜索|执行|运行|重试|重跑|复验|验证|启动)|(?:读取|写入|修改|编辑|检索|搜索|执行|运行|重试|重跑|复验|验证|启动)[^，。；]{0,16}(?:工具|命令|脚本|文件|构建|测试))"
 )
EXECUTION_FOLLOWUP_RE = re.compile(r"(?:随后|之后|紧接着|又|再|直到|才|连续|重复|重试|重跑|复验|补充|修正|修改|覆盖|作废|转入后台)")
CHINESE_ORDINAL_RE = re.compile(
    r"第[零〇一二两三四五六七八九十百千万]+"
    r"(?=(?:至第?[零〇一二两三四五六七八九十百千万]+)?"
    r"(?:行|步|次|轮|个|处|阶段|条|项|章|节|题|页|列|点))"
)


def as_record(row: sqlite3.Row | dict) -> dict:
    record = dict(row)
    record["human_authored"] = bool(record.get("human_authored"))
    record["human_qc_approved"] = bool(record.get("human_qc_approved"))
    record["evidence_gate_passed"] = bool(record.get("evidence_gate_passed"))
    record["history_gate_passed"] = bool(record.get("history_gate_passed"))
    record["delivery_qc_passed"] = bool(record.get("delivery_qc_passed"))
    record["is_continuation"] = bool(record.get("is_continuation"))
    record["quality_contract_version"] = int(record.get("quality_contract_version") or 1)
    return record


def load_records(
    connection: sqlite3.Connection,
    batch: str | None = None,
    *,
    only_human_approved: bool = False,
) -> list[dict]:
    clauses: list[str] = []
    parameters: list[object] = []
    if batch:
        clauses.append("b.name = ?")
        parameters.append(batch)
    if only_human_approved:
        clauses.extend([
            "r.human_qc_approved = 1",
            "r.review_method IN ('human','codex')",
        ])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        "SELECT r.*, b.name AS batch_name, q.question_no, q.task_id "
        "FROM records r JOIN questions q ON q.id=r.question_id "
        "JOIN batches b ON b.id=q.batch_id" + where +
        " ORDER BY b.created_at, q.question_no, r.turn_no",
        parameters,
    ).fetchall()
    return [as_record(row) for row in rows]


def _parse_timestamp(
    value: object, field: str, record_id: str, errors: list[str]
) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{record_id}: {field} is required")
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"{record_id}: {field} must be ISO 8601")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{record_id}: {field} must include timezone offset")
        return None
    return parsed


def _description_style_errors(description: str, *, minimum: int = MIN_DESCRIPTION_CHINESE) -> list[str]:
    errors: list[str] = []
    chinese_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", description))
    if chinese_count < minimum:
        errors.append(f"必须包含至少 {minimum} 个中文字符并使用自然、完整的书面语")
    if "\n" in description or "\r" in description:
        errors.append("五维描述必须是单段文本")
    if len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(f"五维描述不得超过 {MAX_DESCRIPTION_CHARS} 个字符")
    sentences = [part for part in re.split(r"[。！？!?]+", description) if part.strip()]
    if minimum >= MIN_DESCRIPTION_CHINESE and len(sentences) < 2:
        errors.append("必须用至少两个完整句子分别说明证据和工程影响")
    if any(pattern.search(description) for pattern in DESCRIPTION_META_PATTERNS):
        errors.append("不得包含评价者自述、评分质检、模型表现或生成过程措辞")
    if any(pattern.search(description) for pattern in DESCRIPTION_TEMPLATE_PATTERNS):
        errors.append("不得使用固定标签、套话开头、箭头或占位模板")
    if CHINESE_ORDINAL_RE.search(description):
        errors.append("序号必须使用阿拉伯数字，例如第80行")
    prose = re.sub(r"```.*?```|`[^`]*`|https?://\S+", " ", description, flags=re.DOTALL)
    if UNNECESSARY_ENGLISH_RE.search(prose):
        errors.append("不得使用可由中文直接表达的英文评价或衔接词")
    errors.extend(prose_english_issues(description))
    errors.extend(non_model_environment_issues(description))
    return errors


def _score_description_errors(score: object, field: str, description: str) -> list[str]:
    """Apply evidence-specific rules shared by production, QC and export."""
    if not isinstance(score, int) or isinstance(score, bool):
        return []
    errors: list[str] = []
    if score == 5:
        if PERFECT_SCORE_CONTRADICTION_RE.search(description):
            errors.append("满分描述仍承认有要求、约束或功能未完成")
        if not PERFECT_ACTION_RE.search(description) or not PERFECT_RESULT_RE.search(description):
            errors.append("满分描述必须写明具体验证动作及客观结果")
        if VAGUE_PERFECT_CLAIM_RE.search(description) and not PRECISE_LOCATION_RE.search(description):
            errors.append("满分描述不能只写笼统的全部通过，必须给出精确来源位置")
    elif score < 5:
        if not NON_MAX_EVIDENCE_RE.search(description):
            errors.append("非满分描述必须给出具体文件、函数、命令、测试或行号定位")
        if not NON_MAX_IMPACT_RE.search(description):
            errors.append("非满分描述必须说明可观察的工程后果")
        if GENERIC_LOCATION_RE.search(description):
            errors.append("非满分描述不能只写泛化的文件或测试位置")
        if field == "planning_description" and VAGUE_LIST_RE.search(description) and not LIST_ITEM_RE.search(description):
            errors.append("规划描述引用清单时必须点出清单中的具体项目")
        if re.search(r"(?:耗时|成本|代价|返工|等待(?!者))", description) and not OBJECTIVE_OUTCOME_RE.search(description):
            errors.append("不能只写耗时或成本，必须说明客观工程后果")
        if field == "planning_description" and not PLANNING_EVIDENCE_RE.search(description):
            errors.append("规划描述缺少计划、拆解或状态追踪证据")
        if field == "reasoning_description" and not REASONING_PREMISE_RE.search(description):
            errors.append("推理描述没有指出错误前提、推断或遗漏分支")
        if field == "reasoning_description" and REASONING_WRONG_ATTRIBUTION_RE.search(description):
            errors.append("推理描述不能把扣分归因于环境、网络或发现时机")
        if field == "execution_description":
            if not EXECUTION_TOOL_ACTION_RE.search(description):
                errors.append("执行描述缺少具体工具或命令动作")
            if not EXECUTION_FOLLOWUP_RE.search(description):
                errors.append("执行描述缺少重试、恢复或复验过程")
            if VAGUE_FAILURE_RE.search(description) and not EXACT_FAILURE_RE.search(description):
                errors.append("执行描述报告失败时必须写出具体错误、命令或测试结果")
    return errors


def validate_one(
    record: dict,
    require_human_qc: bool = False,
    require_delivery_qc: bool = False,
    require_quality_gates: bool = False,
) -> tuple[list[str], list[str]]:
    record_id = str(record.get("record_id") or "<unknown>")
    errors: list[str] = []
    warnings: list[str] = []
    quality_contract_version = record.get("quality_contract_version", 1)
    if (
        isinstance(quality_contract_version, bool)
        or not isinstance(quality_contract_version, int)
        or quality_contract_version < 1
    ):
        errors.append(f"{record_id}: quality_contract_version must be a positive integer")
        quality_contract_version = 1
    required_text = (
        "record_id", "user_prompt", "session_id", "turn_id", "initial_snapshot",
        "trajectory_file", "harness_version", "languages", "submitter",
    )
    for key in required_text:
        if not isinstance(record.get(key), str) or not record[key].strip():
            errors.append(f"{record_id}: {key} must be non-empty text")
    if record.get("human_authored") is False:
        submitter = str(record.get("submitter", "")).strip().casefold()
        if submitter in {"ai", "codex", "claude", "claude code", "自动", "系统"}:
            errors.append(f"{record_id}: submitter must be a real person's name")
    for key in ("other_issues", "parent_record"):
        if not isinstance(record.get(key), str):
            errors.append(f"{record_id}: {key} must be text")
    if not SNAPSHOT_RE.fullmatch(str(record.get("initial_snapshot", ""))):
        errors.append(f"{record_id}: invalid initial_snapshot")
    trajectory_file = str(record.get("trajectory_file", ""))
    if (
        not trajectory_file.endswith(".jsonl")
        or "/" in trajectory_file
        or "\\" in trajectory_file
    ):
        errors.append(f"{record_id}: trajectory_file must be a JSONL file name")
    for key, allowed in (
        ("reproducibility", REPRODUCIBILITY), ("harness", HARNESSES),
        ("operating_system", OPERATING_SYSTEMS), ("task_type", TASK_TYPES),
        ("difficulty", DIFFICULTIES),
    ):
        if record.get(key) not in allowed:
            errors.append(f"{record_id}: invalid {key}")
    if (
        record.get("harness") == "Claude Code"
        and not NUMERIC_HARNESS_VERSION_RE.fullmatch(
            str(record.get("harness_version", "")).strip()
        )
    ):
        errors.append(f"{record_id}: Claude Code harness_version must be numeric only")
    turn_no = record.get("turn_no")
    if isinstance(turn_no, bool) or not isinstance(turn_no, int) or not 1 <= turn_no <= 10:
        errors.append(f"{record_id}: turn_no must be integer 1-10")
    for prefix in SCORE_PREFIXES:
        score = record.get(f"{prefix}_score")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            errors.append(f"{record_id}: {prefix}_score must be integer 1-5")
        description = record.get(f"{prefix}_description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{record_id}: {prefix}_description is required")
        else:
            for style_error in _description_style_errors(description):
                errors.append(f"{record_id}: {prefix}_description {style_error}")
            if quality_contract_version >= CURRENT_QUALITY_CONTRACT_VERSION:
                for score_error in _score_description_errors(
                    record.get(f"{prefix}_score"), f"{prefix}_description", description
                ):
                    errors.append(f"{record_id}: {prefix}_description {score_error}")
    descriptions = [
        re.sub(r"\s+", "", str(record.get(f"{prefix}_description", "")))
        for prefix in SCORE_PREFIXES
    ]
    repeated = [text for text, count in Counter(descriptions).items() if text and count > 1]
    if repeated:
        errors.append(f"{record_id}: score descriptions must not repeat verbatim")
    other_issues = record.get("other_issues")
    if isinstance(other_issues, str) and other_issues.strip():
        for style_error in _description_style_errors(other_issues, minimum=12):
            errors.append(f"{record_id}: other_issues {style_error}")
        normalized_other = re.sub(r"\s+", "", other_issues)
        for description in descriptions:
            if normalized_other == description or SequenceMatcher(
                None, normalized_other, description,
            ).ratio() >= 0.9:
                errors.append(
                    f"{record_id}: other_issues must contain only issues outside the five score dimensions"
                )
                break
    if not isinstance(record.get("human_authored"), bool):
        errors.append(f"{record_id}: human_authored must be boolean")
    if not isinstance(record.get("human_qc_approved"), bool):
        errors.append(f"{record_id}: human_qc_approved must be boolean")
    elif record["human_qc_approved"] is True:
        review_method = str(record.get("review_method") or "")
        if review_method not in {"human", "codex"}:
            errors.append(f"{record_id}: final review method must be human or codex")
        if not str(record.get("human_qc_reviewer") or "").strip():
            errors.append(f"{record_id}: final reviewer is missing")
        _parse_timestamp(
            record.get("human_qc_approved_at"),
            "human_qc_approved_at",
            record_id,
            errors,
        )
    elif require_human_qc:
        errors.append(f"{record_id}: final five-dimension review is not approved")
    if not isinstance(record.get("delivery_qc_passed"), bool):
        errors.append(f"{record_id}: delivery_qc_passed must be boolean")
    elif require_delivery_qc and record["delivery_qc_passed"] is not True:
        errors.append(f"{record_id}: delivery QC is not passed")
    if record.get("delivery_qc_passed") is True:
        if record.get("delivery_qc_note") != "质检通过":
            errors.append(f"{record_id}: delivery_qc_note must be 质检通过")
        _parse_timestamp(
            record.get("delivery_qc_checked_at"),
            "delivery_qc_checked_at",
            record_id,
            errors,
        )
    changes = record.get("delivery_qc_changes")
    if not isinstance(changes, str):
        errors.append(f"{record_id}: delivery_qc_changes must be JSON text")
    else:
        try:
            parsed_changes = json.loads(changes)
        except ValueError:
            errors.append(f"{record_id}: delivery_qc_changes must be valid JSON")
        else:
            if not isinstance(parsed_changes, list):
                errors.append(f"{record_id}: delivery_qc_changes must contain an array")

    evidence_errors, _evidence, _coverage = validate_evidence_structure(record)
    errors.extend(evidence_errors)
    ledger_hash = str(record.get("evidence_ledger_sha256") or "").strip()
    if ledger_hash and ledger_hash != evidence_ledger_sha256(record.get("evidence_ledger")):
        errors.append(f"{record_id}: evidence_ledger_sha256 与当前证据账本不一致")
    for field, label in (
        ("evidence_gate_passed", "事实证据门禁"),
        ("history_gate_passed", "历史反模板门禁"),
    ):
        if not isinstance(record.get(field), bool):
            errors.append(f"{record_id}: {field} must be boolean")
        elif require_quality_gates and record[field] is not True:
            errors.append(f"{record_id}: {label}未通过")

    is_continuation = record.get("is_continuation", False)
    if not isinstance(is_continuation, bool):
        errors.append(f"{record_id}: is_continuation must be boolean")
    continuation_count = record.get("continuation_count", 0)
    if (
        isinstance(continuation_count, bool)
        or not isinstance(continuation_count, int)
        or continuation_count < 0
    ):
        errors.append(f"{record_id}: continuation_count must be a non-negative integer")
    raw_user_prompt = record.get("raw_user_prompt", "")
    raw_turn_id = record.get("raw_turn_id", "")
    if not isinstance(raw_user_prompt, str) or not isinstance(raw_turn_id, str):
        errors.append(f"{record_id}: raw continuation fields must be text")
    elif is_continuation:
        if raw_user_prompt.strip() != "继续" or not raw_turn_id.strip():
            errors.append(f"{record_id}: continuation must preserve raw 继续 and its PromptID")
        if not isinstance(continuation_count, bool) and continuation_count < 1:
            errors.append(f"{record_id}: continuation_count must be positive for a continuation")
        other = str(record.get("other_issues") or "")
        if "中断" not in other or "继续" not in other:
            errors.append(f"{record_id}: continuation must explain the interruption and recovery in other_issues")
    elif continuation_count not in {0, False}:
        errors.append(f"{record_id}: a normal turn cannot have continuation_count")

    completed = _parse_timestamp(
        record.get("turn_completed_at"), "turn_completed_at", record_id, errors
    )
    submitted = _parse_timestamp(
        record.get("submitted_at"), "submitted_at", record_id, errors
    )
    if completed and submitted:
        completed_local = completed.astimezone(PROJECT_TIMEZONE)
        submitted_local = submitted.astimezone(PROJECT_TIMEZONE)
        if completed_local.time() < time(20, 0):
            deadline = datetime.combine(
                completed_local.date(), time.max, PROJECT_TIMEZONE
            )
        else:
            deadline = datetime.combine(
                completed_local.date() + timedelta(days=1),
                time(14, 0),
                PROJECT_TIMEZONE,
            )
        if submitted_local > deadline and not late_delivery_allowed():
            errors.append(
                f"{record_id}: submitted after project deadline {deadline.isoformat()}"
            )
        if submitted_local < completed_local:
            errors.append(f"{record_id}: submitted_at precedes turn_completed_at")
    if record.get("difficulty") == "简单" and not record.get("parent_record", "").strip():
        errors.append(f"{record_id}: a first-turn record cannot be 简单")
    if len(str(record.get("user_prompt", ""))) > 32767:
        warnings.append(f"{record_id}: User Prompt exceeds Excel's per-cell text limit")
    return errors, warnings


def validate_records(
    records: list[dict],
    require_human_qc: bool = False,
    require_delivery_qc: bool = False,
    require_quality_gates: bool = False,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for record in records:
        item_errors, item_warnings = validate_one(
            record, require_human_qc, require_delivery_qc, require_quality_gates
        )
        errors.extend(item_errors)
        warnings.extend(item_warnings)

    ids = [str(record.get("record_id", "")) for record in records]
    for duplicate, count in Counter(ids).items():
        if duplicate and count > 1:
            errors.append(f"duplicate record_id: {duplicate}")
    pair_records: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        pair_records[(
            str(record.get("session_id", "")), str(record.get("turn_id", "")),
        )].append(record)
    for duplicate, items in pair_records.items():
        if not all(duplicate) or len(items) <= 1:
            continue
        ordered = sorted(items, key=lambda item: int(item.get("turn_no") or 0))
        if any(not bool(item.get("is_continuation")) for item in ordered[1:]):
            errors.append(
                f"duplicate SessionID + TurnID without verified continuation: "
                f"{duplicate[0]} / {duplicate[1]}"
            )

    by_id = {str(record.get("record_id")): record for record in records}
    sessions: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        sessions[str(record.get("session_id", ""))].append(record)
    stable_fields = (
        "initial_snapshot", "trajectory_file", "harness", "harness_version",
        "operating_system",
    )
    for session_id, items in sessions.items():
        if not session_id:
            continue
        if len(items) > 10:
            errors.append(f"{session_id}: {len(items)} submitted turns exceed the limit of 10")
        valid_turn_numbers = [
            item.get("turn_no") for item in items
            if isinstance(item.get("turn_no"), int)
            and not isinstance(item.get("turn_no"), bool)
        ]
        if len(valid_turn_numbers) == len(items):
            ordered = sorted(items, key=lambda item: item["turn_no"])
            actual = [item["turn_no"] for item in ordered]
            expected = list(range(1, len(ordered) + 1))
            if actual != expected:
                errors.append(
                    f"{session_id}: turn_no must be consecutive from 1, found {actual}"
                )
            for index, item in enumerate(ordered):
                expected_parent = (
                    "" if index == 0
                    else str(ordered[index - 1].get("record_id", ""))
                )
                if str(item.get("parent_record", "")).strip() != expected_parent:
                    errors.append(
                        f"{item.get('record_id')}: parent_record must match the immediately preceding turn"
                    )
        for field in stable_fields:
            values = {str(item.get(field, "")) for item in items}
            if len(values) > 1:
                errors.append(f"{session_id}: inconsistent {field} across turns")
        roots = [item for item in items if not str(item.get("parent_record", "")).strip()]
        if len(roots) != 1:
            errors.append(f"{session_id}: expected exactly one root record, found {len(roots)}")
        for item in items:
            parent_id = str(item.get("parent_record", "")).strip()
            if not parent_id:
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                errors.append(f"{item.get('record_id')}: parent_record {parent_id} not found")
            elif parent.get("session_id") != session_id:
                errors.append(
                    f"{item.get('record_id')}: parent_record belongs to another session"
                )
    return errors, warnings
