---
name: cc-usr-delivery-producer
description: After a Claude Code turn, store the human-provided SessionID, PromptID, scores, and descriptions as one delivery record in the production SQLite database. Normalize mechanical metadata, but never inspect trajectories, score performance, or write rating descriptions.
---

# CC USR Delivery Producer

Create one SQLite record for every valid user-prompt/model-response turn.

## Human-only fields

The human expert must personally inspect the process and product, choose all five scores, write all five descriptions, and decide `其他问题`. Never read model trajectories or output, recommend scores, paraphrase evidence, complete descriptions, or improve their wording. If a required human value is missing, request that exact value.

The human must also copy the exact identifiers from Claude Code: the session's `SessionID` and the current user message's `PromptID`. Never search, open, or parse `~/.claude/projects` to obtain them. Do not substitute the task ID, batch run ID, question ID, or record ID. All turns in one Claude window use the same `SessionID`; every turn uses a distinct `PromptID`.

## Handoff after a run

This is the immediate next skill after a human finishes reviewing a Claude Code turn. Ask for the batch, question number, and turn number, plus the human-only fields, then store the record. For a first turn, the tool copies the original prompt, initial snapshot, task metadata, and registered Claude Code version from SQLite. The user does not re-enter those values.

## Use

Read `项目规范.md` and [references/record-contract.md](references/record-contract.md), then run:

```bash
python3 .agents/skills/cc-usr-delivery-producer/scripts/collect_record.py \
  --db production.sqlite3 --batch 0911 --question 1 --turn 1
```

For prepared human input, add `--from-json <人工填写文件>`. The tool copies first-turn prompt, snapshot, reproducibility, language, task type, difficulty, and Claude Code version from SQLite. It requires the actual SessionID, PromptID, scores, descriptions, completion time, and confirmation from the human. Later turns also require that turn's exact prompt, type, difficulty, and languages.

Records are inserted as unapproved drafts. Existing record IDs and question/turn pairs are never overwritten. Do not include engineering/network failure-only turns; do include human `继续` turns caused by thinking limits.

Store turns in order. For a one-turn task, run this skill once with `--turn 1`. For a multi-turn task, repeat it after each valid turn with increasing turn numbers, the same `SessionID`, and that turn's distinct `PromptID`. After all records for the batch are stored, hand off to `$cc-usr-delivery-qc`; do not approve or export records in this skill.
