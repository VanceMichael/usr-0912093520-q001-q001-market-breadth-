# Delivery QC checklist

Check every record and correct every supported noncompliance before finalizing the batch.

## Required export fields

- `User Prompt` is the exact prompt for this turn, not a summary. First-turn text matches the stored question. For later turns, compare byte-for-byte with that turn's user event in the original JSONL; `继续` stays `继续` and must never be replaced with the session's first prompt.
- `SessionID` identifies the matched session; all turns from one session share it.
- `TurnID/PromptID` is the exact unique ID of this user turn, and the same ID must be present on the matched user event in the original JSONL.
- `初始环境快照` is the reachable GitHub commit permalink with a full 40-character SHA and remains stable across the session. A repository homepage, branch/tag URL, short SHA, or latest-commit link is invalid.
- `轨迹文件` is the matched original JSONL file name without a local directory path and remains stable across the session. Reject stream-json terminal output or any file that does not contain the referenced SessionID, PromptID, and user prompt.
- Reproducibility, harness, harness version, operating system, task type, difficulty, and languages use the project-approved values and describe this turn accurately.
- All five scores are integers from 1 through 5 and agree with their descriptions and the rubric. No plan/status evidence caps `任务规划` at 2; substantial failed/repeated/unverified execution caps `执行能力` at 3 unless the evidence clearly shows an external-only failure and precise recovery. A description-score contradiction is a blocking error, not a style preference.
- All five descriptions are non-empty, factual, dimension-specific, and written as natural professional engineering observations. A deduction makes the problem location, actual behavior, and impact clear without visible labels or a fixed sentence frame.
- `其他问题` is text and is empty when there is nothing additional.
- `提交人` is the real configured person, never a tool or model name.
- `提交时间` is ISO 8601 with a timezone and meets the project deadline.
- `父记录` is empty only for the first turn; later turns point to the immediately preceding record in the same session.
- `审核备注` is written as `质检通过` only by successful finalization.

## Batch consistency

- Every valid turn is present once, no session/turn pair or record is duplicated, and no session exceeds ten submitted turns.
- Snapshot, harness, harness version, and operating system stay consistent within a session.
- Scores and descriptions do not contradict each other or omit an obvious issue visible in the evidence.
- Descriptions contain no evaluator self-reference, generation-process wording, fixed element labels, arrows, placeholders, or verbatim reuse across dimensions.
- Internal provenance remains truthful. Corrections never change `human_authored` or claim a human action that did not occur.

Use the stored question/run metadata for inherited fields and inspect the matched session or workspace only when a judgment field needs correction. Record the reason and exact before/after values for every change.
