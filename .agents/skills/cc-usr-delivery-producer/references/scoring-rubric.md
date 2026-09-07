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

Every description is required, including scores of 5. A supported deficiency must contain:

1. **When**: the stage, step, tool call, or file edit where it occurred.
2. **What**: the exact behavior or product defect.
3. **Impact**: the requirement or business outcome affected.
4. **Root cause and correction** when the evidence supports them.

Use these shapes when useful:

```text
过程：在【步骤/环节】，模型【具体行为】，导致【影响】；根因是【依据充分的原因】，正确做法是【可执行做法】。
产物：产物在【文件/功能】存在【具体问题】，证据是【报错/缺失需求/行为】，因此无法满足【需求】。
```

When process and product problems are causally related, close the loop in one precise explanation. When they are independent, describe both without forcing a connection.

## Calibration examples

- Weak: `没完成任务，不满意。`
  Strong: `prompt 要求支持分页，产物只实现列表查询且未处理分页参数，导致大数据量下无法按页读取结果，交付不完整。`
- Weak: `代码有 bug。`
  Strong: `产物 utils/date.py 的 parse() 未处理时区，输入带 Z 的 ISO 字符串会抛出 ValueError，导致合法时间数据无法导入。`
- Weak: `过程比较乱。`
  Strong: `规划阶段后，模型在第 2、4、6 步重复读取 config.py，第 5 步修改后也未运行验证，增加了无效调用并留下未验证交付。`
- Weak: `越改问题越多。`
  Strong: `终端返回 npm install 版本冲突后，模型连续三次执行相同安装命令，没有检查 package.json 的依赖范围，导致冲突未解决且浪费执行步骤；正确做法是先定位冲突依赖再调整兼容版本。`
- Weak: `写的代码不符合需求。`
  Strong: `prompt 明确限制只修改前端缓存逻辑，模型却修改后端数据库查询，违反作用域约束并扩大回归风险；规划阶段应先固定允许修改的边界。`

For a score of 5, still state what was checked, for example which requirements were implemented, which verification passed, and why no material omission or false success remains.
