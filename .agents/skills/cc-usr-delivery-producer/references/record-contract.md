# SQLite delivery record contract

The database stores the exact 25 Excel fields plus internal linkage and audit fields.

Human input required for every turn:

```text
session_id, turn_id,
delivery_score, delivery_description,
instruction_score, instruction_description,
planning_score, planning_description,
reasoning_score, reasoning_description,
execution_score, execution_description,
other_issues, submitter, turn_completed_at, human_authored
```

`session_id` is the exact Claude Code session identifier. `turn_id` stores the project-required `PromptID` from that turn's user message. These values are copied manually by the human from Claude Code's session record; this skill must not inspect the trajectory to discover them. A session keeps one `session_id` across turns, while `turn_id` must be unique for every recorded turn.

For turns after the first, the human also supplies `user_prompt`, `task_type`, `difficulty`, and `languages` because classification is based on that turn's actual intent.

Scores are integers 1 through 5. Every description is non-empty even for score 5. `human_authored` must be true. Timestamps use ISO 8601 with a timezone. The first record has no parent; later records automatically point to the immediately previous stored turn.
