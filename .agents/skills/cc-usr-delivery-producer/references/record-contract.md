# SQLite delivery record contract

The database stores the exact 28 Excel fields plus internal linkage and audit fields.

Scored input required for every turn:

```text
session_id, turn_id, trajectory_file,
delivery_score, delivery_description,
instruction_score, instruction_description,
planning_score, planning_description,
reasoning_score, reasoning_description,
execution_score, execution_description,
other_issues, submitter, turn_completed_at, human_authored
evidence_ledger, requirement_coverage
```

`session_id` is the exact Claude Code session identifier. For a normal turn, `turn_id` and `user_prompt` store that user event's exact `PromptID` and text. For an exact `继续` event after interruption, the exported `turn_id` and `user_prompt` reuse the nearest preceding non-continuation task identity required by SOLO2, while internal `raw_turn_id` and `raw_user_prompt` preserve the actual continuation event. Set `is_continuation=true` and record consecutive `continuation_count`; distinct raw PromptIDs remain mandatory. `trajectory_file` is the matched original JSONL base name, normally `<SessionID>.jsonl`, without a local directory path; a terminal `stream-json` transcript is not a trajectory artifact.

`turn_no` is the one-based chronological position of the user turn inside the current `SessionID` and is exported as `当前对话轮次排序`. It is already a required SQLite column, so do not create a duplicate database field. Derive it from the matched original JSONL: the first submitted turn is 1 and later submitted turns are consecutive through at most 10. It is not a question number, tool-call count, record ID suffix, or global batch row number.

For turns after the first, the human also supplies `user_prompt`, `task_type`, `difficulty`, and `languages` because classification is based on that turn's actual intent. Normal prompts are copied exactly. A verified `继续` record reuses the interrupted task's exported prompt and classification while keeping `继续` only in the raw audit fields. `other_issues` must state the interruption and recovery, including the count when continuation happened more than once.

Scores are integers 1 through 5. Every description is non-empty even for score 5. Set `human_authored` to false for automatically produced scores and descriptions; never mislabel them as human-authored. Keep provenance in this internal field, not in exported descriptions. `submitter` is the real person's configured or explicitly supplied name, never a tool or invented identity. Timestamps use ISO 8601 with a timezone. The first record has no parent; later records automatically point to the immediately previous stored turn.

Each of the five score descriptions is one paragraph, contains at least 45 Chinese characters, and is at most 420 characters long. Successful behavior must be supported by verification inside the matched target turn; commands run later by production, QC, or an operator may confirm current product state but cannot be attributed to the target turn. A failed verification is retained and reflected honestly in the description and score.

`evidence_ledger` is a JSON array that binds every sentence in all five descriptions to a source. Every new item includes one semantic `kind`: `requirement` uses `prompt`, `product` uses `workspace`, `process` uses a trajectory `tool_use` or `thinking` event, and `runtime` uses a trajectory `tool_result` event plus `related_line` pointing to the earlier matching `tool_use`. Trajectory entries include the exact one-based JSONL line, excerpt and SHA-256; workspace entries include a safe relative path, excerpt and full-file SHA-256; prompt entries include the exact excerpt and prompt SHA-256. A trajectory line and its related tool call must fall after this turn's real user event and before the next real user event. `requirement_coverage` quotes each material requirement exactly, records `met`/`unmet`/`uncertain`, and points to evidence IDs. Missing evidence, an invalid source/kind pairing, an unmatched runtime result, a later-turn source, a changed hash, or an unbound sentence blocks insertion. Legacy records without `kind` remain readable, but once one item supplies `kind`, every item in that ledger must supply it.

New records start with `delivery_qc_passed=false`. The separate delivery QC step may repair only supported exported fields, records every reason and before/after value in `delivery_qc_changes`, and sets `delivery_qc_note=质检通过` plus a check timestamp only after the complete batch passes. The note is exported as `审核备注`; the pass flag, check time, and change history remain internal.

Final delivery also requires one explicit five-dimension review method. A person may confirm all five dimensions individually, setting `review_method=human`; alternatively, the console may run an isolated Codex review over the secret-free review dossier and set `review_method=codex` only when all five structured conclusions are approved and the deterministic gates pass again. Keep the legacy `human_qc_*` columns for storage compatibility, but `review_method` is the authoritative provenance and must never mislabel Codex as a person.
