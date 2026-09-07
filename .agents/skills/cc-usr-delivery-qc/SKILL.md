---
name: cc-usr-delivery-qc
description: Run deterministic structural QC on SQLite delivery records and record explicit human qualitative approval. Check fields, IDs, turns, snapshots, timestamps, duplicates, and session consistency without analyzing trajectories or judging scores.
---

# CC USR Delivery QC

Validate database records without asking AI to assess model behavior.

## Mechanical QC

Read `项目规范.md` and [references/human-qc-checklist.md](references/human-qc-checklist.md), then run:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911
```

The checker validates fields, enums, score ranges, full snapshots, duplicate IDs, turn limits, parent chains, timestamps, deadlines, first-turn difficulty, and stable session metadata. Errors block approval. Warnings require human disposition.

## Human approval

Only after the reviewer personally completes the checklist, record that decision explicitly:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911 \
  --approve 0911-001-T01 --reviewer <姓名> --confirm-human-review YES
```

Then rerun with `--require-human-qc`. This command records the human's decision; it does not make or infer it.

Never open or analyze model trajectories, responses, generated code, diffs, or terminal logs. Never judge whether scores are correct, rewrite descriptions, detect AI authorship semantically, or generate missing evidence.
