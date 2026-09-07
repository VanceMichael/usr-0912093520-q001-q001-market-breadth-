# SQLite delivery record contract

The database stores the exact 27 Excel fields plus internal linkage and audit fields.

Scored input required for every turn:

```text
session_id, turn_id, trajectory_file,
delivery_score, delivery_description,
instruction_score, instruction_description,
planning_score, planning_description,
reasoning_score, reasoning_description,
execution_score, execution_description,
other_issues, submitter, turn_completed_at, human_authored
```

`session_id` is the exact Claude Code session identifier. `turn_id` stores the project-required `PromptID` from that turn's user message. `trajectory_file` is the matched original JSONL base name, normally `<SessionID>.jsonl`, without a local directory path; a terminal `stream-json` transcript is not a trajectory artifact. The delivery producer extracts all three from the matched Claude Code trajectory and must be able to locate the same user event by the `(session_id, turn_id, user_prompt)` triple. A session keeps one `session_id` and trajectory file across turns, while `turn_id` must be unique for every recorded turn.

For turns after the first, the human also supplies `user_prompt`, `task_type`, `difficulty`, and `languages` because classification is based on that turn's actual intent. The prompt is copied exactly from that user event; if the user typed `继续`, store exactly `继续` and inherit only the classification metadata that the project rules permit.

Scores are integers 1 through 5. Every description is non-empty even for score 5. Set `human_authored` to false for automatically produced scores and descriptions; never mislabel them as human-authored. Keep provenance in this internal field, not in exported descriptions. `submitter` is the real person's configured or explicitly supplied name, never a tool or invented identity. Timestamps use ISO 8601 with a timezone. The first record has no parent; later records automatically point to the immediately previous stored turn.

New records start with `delivery_qc_passed=false`. The separate delivery QC step may repair only supported exported fields, records every reason and before/after value in `delivery_qc_changes`, and sets `delivery_qc_note=质检通过` plus a check timestamp only after the complete batch passes. The note is exported as `审核备注`; the pass flag, check time, and change history remain internal.
