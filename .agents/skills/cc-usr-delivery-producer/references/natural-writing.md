# Natural description hard gate

The exported five-dimensional descriptions are feedback written for another programmer. They are not an audit summary, a trajectory digest, or a model self-report. Technical facts may come from the trajectory, but the wording must read like a person who inspected the change and is explaining the useful conclusion.

## Non-negotiable rules

- Lead with the fact that matters for this dimension: a command result, a file or function, an observed behavior, a missing requirement, or a concrete recovery. Do not open with a recap of “this turn” or with an inspection preamble.
- Let the judgment follow the fact in ordinary prose. Do not force every field into “facts, then however, then conclusion”. A planning description may start with the first planned step; an execution description may start with the error; an instruction description may start with the violated requirement.
- Give each dimension a different job. Delivery explains what exists and what is still missing. Instruction following compares the output with explicit constraints. Planning discusses decomposition and state tracking. Reasoning discusses decisions and diagnosis. Execution discusses tool choice, failures, retries, and verification.
- Use only evidence needed for the judgment. A test count, list of modules, or protocol name belongs in the description only when it helps establish completion, scope, or a defect. Do not turn the field into an inventory.
- Read the five fields in sequence. If two can be swapped without changing meaning, rewrite at least one. Their first sentence, sentence length, connectors, and final sentence should not all line up.

## Forbidden wording and visible scaffolding

Do not use these stock phrases as prose: `最终工作区包含`, `逐条核对后没有发现`, `核心取舍是清楚的`, `本轮主要是`, `本轮按`, `顺序基本合理`, `这些修正体现了`, `无法证明`, `根据轨迹`, `综合来看`, `经检查`, `唯一问题`. Avoid openings based on `本轮`、`这轮`、`这一轮` altogether; the record already identifies the turn.

Do not expose a writing template through `When`/`What`/`Impact`, `过程：`, `产物：`, `事实：`, `结论：`, arrows, bracketed placeholders, or repeated “虽然……但是……” constructions. Do not mention the evaluator, authorship, automation, trajectory processing, or “AI/Codex 认为”.

## Rewrite test

Before the style script runs, ask:

1. Could a reader identify the file, command, behavior, or missing requirement without seeing the other four fields?
2. Does this field contain a judgment that belongs to another dimension instead?
3. Did a number or technical name earn its place, or is it only making the paragraph look authoritative?
4. Does the first sentence sound like a human starting a review, rather than a generated report section?
5. Does this sentence still sound natural when read aloud without the column heading?

If any answer is no, rewrite before insertion. A style checker is a blocking floor, not a substitute for this reread.

## Examples

Avoid: `本轮主要是读取合同、写入包骨架和少量配置检查，顺序基本合理，但无法证明事务边界已经正确。`

Prefer: `实际执行停在合同阅读、目录搭建和包骨架，通行证服务还没开始。没有数据库或 API 运行结果，事务边界只能算设计意图，不能当作已经验证。`

Avoid: `本轮按设备包、编译器、策略服务、通行证服务推进，这些修正体现了较好的边界推理，仍未进入集成验证。`

Prefer: `扁平 overlay 规则和 compile_ruleset 参数对不上时，模型找到了接口不一致的位置，并调整了调用方式。到交互结束仍没有跑 Postgres 集成测试，修正是否覆盖真实链路还没有证据。`
