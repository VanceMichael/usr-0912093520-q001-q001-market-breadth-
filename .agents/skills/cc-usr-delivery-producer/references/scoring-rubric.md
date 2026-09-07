# Evidence-based scoring and description rubric

Apply the full five-dimensional anchors in `项目规范.md` to each turn independently. Prior turns provide context, but each record scores the current prompt-response pair.

## Evidence collection

Use all relevant evidence before scoring:

- The exact user prompt and its explicit and implicit constraints.
- The turn's reasoning, plan updates, tool calls, failures, retries, recovery, and final response.
- The initial snapshot versus the code state produced by the model.
- Relevant tests, build results, runtime behavior, missing files, stubs, hard-coded data, regressions, and unverified claims.

Do not invent a step number, command, file, function, exception, test result, requirement, or business impact. A claim in the model's final response is not proof that the change exists or works.

## Five dimensions

- `交付完整性`: Judge whether the requested result exists, covers the requirements, runs correctly, and avoids false claims. Cite concrete product evidence.
- `指令遵循`: Compare each meaningful constraint with the model's actual behavior and output. Quote or precisely identify violated constraints.
- `任务规划`: Judge decomposition, ordering, progress tracking, clarification, and closure. Cite where the plan helped or failed.
- `推理能力`: Judge understanding, diagnosis, decisions, boundary reasoning, and reasoning cost relative to task difficulty. Separate overthinking from insufficient reasoning.
- `执行能力`: Judge tool choice, correctness, efficiency, recovery, and validation. Cite repeated reads, failed commands, wrong tools, missing verification, or precise efficient execution.

Scores are integers from 1 to 5 and must follow the anchors in `项目规范.md`. Do not force all dimensions to the same score.

## Description requirements

Every description is required, including scores of 5. Write as a programmer reporting a concrete review finding to another programmer. A reader should be able to tell, without labels or a prescribed format, where the issue surfaced, what actually happened, and what it changed or prevented. Add the likely cause and the better engineering approach when the available evidence supports both.

Do not turn those ingredients into visible headings, a checklist, or a repeated sentence frame. Vary the opening, rhythm, and level of detail according to the evidence. Some findings read most naturally from the failed requirement to the code evidence; others should start with a command failure, a mistaken decision, or a specific file. Use “根因是” or “正确做法是” only when it genuinely improves clarity, not as a mandatory tail.

When process and product problems are causally related, explain that connection in ordinary prose. When they are independent, discuss both without inventing a connection.

The descriptions are production feedback, not an audit note. They must not identify or discuss the evaluator, the writing process, automation, internal provenance, or how the text was generated. Do not use self-referential wording such as `AI 分析认为`, `Codex 认为`, “自动生成” or “基于轨迹生成”. An exact product, API, or business-domain term may be retained only when it is necessary to describe the task evidence.

Before storing a turn, read the five descriptions as a set. They must not be identical paraphrases with only the dimension name changed, and they must not start with fixed labels such as `When:`、`What:`、`Impact:`、“过程：”或“产物：”. Openers such as “经检查”“根据轨迹”“综合来看” also expose a writing formula instead of getting to the engineering fact. Do not use arrows or bracketed placeholders.

## Calibration examples

- Weak: `没完成任务，不满意。`
  Strong: `分页是 prompt 中的核心能力，但查询入口只接收筛选条件，响应结构里也没有页码和总数；数据量增长后调用方仍只能一次取回全部记录。`
- Weak: `代码有 bug。`
  Strong: `utils/date.py` 的 `parse()` 直接交给无时区格式解析，带 `Z` 的合法 ISO 时间会抛出 `ValueError`，导入任务因此会在第一条 UTC 数据处中断。
- Weak: `过程比较乱。`
  Strong: `完成配置修改后没有执行任何验证，随后又三次回读同一个 config.py，却始终没检查配置是否能被应用加载；这些重复操作没有降低交付风险，最终状态仍未经确认。`
- Weak: `越改问题越多。`
  Strong: `npm install 首次报告 peer dependency 冲突后，相同命令又原样执行了两次。package.json 中的版本范围没有被核对，冲突当然不会自行消失；这里应先定位不兼容的直接依赖，再选择匹配版本。`
- Weak: `写的代码不符合需求。`
  Strong: `需求把修改范围限定在前端缓存层，提交中却包含后端查询重写。这个改动既没有改善指定缓存路径，还把数据库行为纳入回归范围，说明动手前没有固定任务边界。`

For a score of 5, still state what was checked, for example which requirements were implemented, which verification passed, and why no material omission or false success remains.
