---
name: cc-usr-delivery-producer
description: Store one human-authored delivery record per valid Claude Code turn in the production SQLite database. Normalize mechanical metadata, but never inspect trajectories, score performance, or write rating descriptions.
---

# CC USR Delivery Producer

Create one SQLite record for every valid user-prompt/model-response turn.

## Human-only fields

The human expert must personally inspect the process and product, choose all five scores, write all five descriptions, and decide `其他问题`. Never read model trajectories or output, recommend scores, paraphrase evidence, complete descriptions, or improve their wording. If a required human value is missing, request that exact value.

## Use

Read `项目规范.md` and [references/record-contract.md](references/record-contract.md), then run:

```bash
python3 .agents/skills/cc-usr-delivery-producer/scripts/collect_record.py \
  --db production.sqlite3 --batch 0911 --question 1 --turn 1
```

For prepared human input, add `--from-json <人工填写文件>`. The tool copies first-turn prompt, snapshot, reproducibility, language, task type, difficulty, and Claude Code version from SQLite. It requires the actual SessionID, PromptID, scores, descriptions, completion time, and confirmation from the human. Later turns also require that turn's exact prompt, type, difficulty, and languages.

Records are inserted as unapproved drafts. Existing record IDs and question/turn pairs are never overwritten. Do not include engineering/network failure-only turns; do include human `继续` turns caused by thinking limits.
