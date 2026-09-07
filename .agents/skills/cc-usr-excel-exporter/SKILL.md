---
name: cc-usr-excel-exporter
description: Export human-QC-approved SQLite records for one batch into the exact 25-column CC_Codex user-satisfaction workbook template. Never generate, infer, or rewrite ratings and descriptions.
---

# CC USR Excel Exporter

Produce the final workbook from `production.sqlite3`.

## Preconditions

- Read `项目规范.md` and [references/excel-mapping.md](references/excel-mapping.md).
- Records must pass deterministic QC and explicit human qualitative QC.
- Do not read model trajectories or fill missing human-authored fields.

## Export

```bash
python3 .agents/skills/cc-usr-excel-exporter/scripts/export_xlsx.py \
  --db production.sqlite3 --batch 0911 \
  --output "outputs/CC_Codex 用户满意度标注（0911）.xlsx"
```

The exporter selects only records with `human_qc_approved=1`, revalidates them, copies the bundled template, and fills `数据表` in exact A:Y order. It preserves the template structure and refuses to overwrite the template or an existing output unless `--force` is explicitly supplied.

Report the exported row count, duplicate status, and output path. Never export internal SQLite-only fields.
