# Delivery QC checklist

Check every record and correct every supported noncompliance before finalizing the batch.

发现即处理：任何错误、警告、缺失字段、证据冲突或链路不一致都要在当前质检任务中立即核对、修正并复检。不能只把问题列在报告里，不能等导出阶段再处理，也不能在仍有问题时写入“质检通过”。

## Required export fields

- `User Prompt` is the exact prompt for a normal turn. A verified `继续` event reuses the nearest preceding interrupted task's complete exported prompt; its actual `继续` text remains byte-for-byte in `raw_user_prompt`.
- `SessionID` identifies the matched session; all turns from one session share it.
- `TurnID/PromptID` is the normal turn's exact ID. A verified continuation reuses the interrupted task's exported ID and preserves its own distinct event ID in `raw_turn_id`; repeated exported IDs are forbidden for every other case.
- `当前对话轮次排序` comes from SQLite `turn_no` and is the user turn's one-based chronological position within the same `SessionID`. It starts at 1 and remains consecutive. Do not use the question number, tool-call count, global row number, or parent-record suffix.
- `初始环境快照` is the reachable GitHub commit permalink with a full 40-character SHA and remains stable across the session. A repository homepage, branch/tag URL, short SHA, or latest-commit link is invalid.
- `轨迹文件` is the matched original JSONL file name without a local directory path and remains stable across the session. Reject stream-json terminal output or any file that does not contain the referenced SessionID, PromptID, and user prompt.
- Resolve the JSONL from the successful registered run linked by `session_id`, not from the latest failed retry or a convenient global Claude directory. A non-empty `runs.trajectory_root` is authoritative. Missing, ambiguous, unreadable, malformed, or empty trajectories are hard failures.
- The matched JSONL contains exactly one raw event for each record. Normal records match the exported triple; continuation records match the raw `继续` fields and point back to the nearest interrupted task. Require a single session, order-sensitive `tool_use.id` to `tool_result.tool_use_id` pairing, a final text-bearing assistant event after the recovered work, and trajectory-derived user-turn order equal to `当前对话轮次排序`.
- The matched JSONL must be clean for this question. Reject it if any nested tool event invokes `Skill`/slash-command loading or reads external skill, `CLAUDE.md`, `AGENTS.md`, Claude settings, or Codex config outside the current question workspace. Such contamination cannot be repaired in SQLite; require a new clean model run.
- Reproducibility, harness, harness version, operating system, task type, difficulty, and languages use the project-approved values and describe this turn accurately.
- All five scores are integers from 1 through 5 and agree with their descriptions and the rubric. No plan/status evidence caps `任务规划` at 2; substantial failed/repeated/unverified execution caps `执行能力` at 3 unless the evidence clearly shows an external-only failure and precise recovery. A description-score contradiction is a blocking error, not a style preference.
- All five descriptions are non-empty, factual, dimension-specific Chinese professional prose. Every conclusion names verifiable evidence from a file, function, command, error, test result, explicit requirement, or exact trajectory action. A deduction makes the problem location, actual behavior, and impact clear without visible labels or a fixed sentence frame.
- All descriptions and non-empty `其他问题` reject AI/evaluator/scoring/generation language and non-technical English evaluator words such as `rationale`, `overall`, `generally`, and `basically`; necessary file names, classes, commands, frameworks, protocols, paths, and code identifiers remain valid.
- `其他问题` is text and is empty when there is nothing additional. When non-empty, it follows the same Chinese, evidence, and natural-writing requirements as the five descriptions and must add an issue outside the five score dimensions.
- `提交人` is the real configured person, never a tool or model name.
- `提交时间` is ISO 8601 with a timezone and meets the project deadline.
- `父记录` is empty only when `当前对话轮次排序` is 1; later turns point to the record whose order is exactly one lower in the same session.
- `审核备注` is written as `质检通过` only by successful finalization.

## Batch consistency

- Every valid turn is present once, no session/turn pair or record is duplicated, and no session exceeds ten submitted turns.
- Snapshot, harness, harness version, and operating system stay consistent within a session.
- Scores and descriptions do not contradict each other or omit an obvious issue visible in the evidence.
- Descriptions contain no evaluator self-reference, model-performance wording, generation-process wording, scoring/QC language, fixed element labels, stock openings, arrows, placeholders, or verbatim reuse across dimensions. They do not read like translated or mechanically assembled prose.
- Internal provenance remains truthful. Corrections never change `human_authored` or claim a human action that did not occur.

## Repair loop

1. 看到首个错误或警告后，先定位对应 `record_id` 和字段，再读取规定的权威来源。
2. 能从证据恢复的字段立即写入临时修正 JSON，并执行 `--fixes`。每项修正必须记录原因和 before/after；不能为了让检查变绿而猜值或改动无关字段。
3. 修正命令结束后马上重新执行整批校验。若仍有问题、出现新问题或命令返回非零，继续下一轮修正，不得执行 `--finalize`。
4. 只有在同一批次最新报告同时满足 `errors=[]` 和 `warnings=[]` 后，才允许 finalize。若证据确实缺失，保持未通过并明确缺少哪份来源。
5. 报告为零错误之前确认轨迹门禁确实读取到了权威文件；“文件不存在所以没有发现污染”属于质检失败。

Use the stored question/run metadata for inherited fields and inspect the matched session or workspace only when a judgment field needs correction. Record the reason and exact before/after values for every change.
