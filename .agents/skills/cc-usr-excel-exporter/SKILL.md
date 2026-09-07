---
name: cc-usr-excel-exporter
description: Export selected delivery-QC-passed SQLite records to a new 27-column CC_Codex workbook and copy their original Claude Code JSONL trajectories into the batch directory. Never generate, infer, or rewrite record values.
---

# CC USR Excel Exporter

Produce a clean final workbook from `production.sqlite3` and place the matched original trajectories beside it. This is a separate step after `$cc-usr-delivery-qc`; never run delivery QC or change records here.

## Preconditions

- Read `项目规范.md` and [references/excel-mapping.md](references/excel-mapping.md).
- Every exported record must already have `delivery_qc_passed=1` and the note `质检通过`.
- Do not analyze or transform trajectories, fill missing fields, repair records, or alter scores and descriptions. Copy the matched JSONL files byte for byte.

## Export

```bash
python3 .agents/skills/cc-usr-excel-exporter/scripts/export_xlsx.py \
  --db production.sqlite3 --batch 0911 --select 1,3-5
```

Omit `--select` to export every record in the batch. The exporter requires every selected record to have `delivery_qc_passed=1`, revalidates the selected records, and creates a new workbook from the bundled `CC_Codex 用户满意度标注（试标）.xlsx` layout. The result contains only one `数据表` worksheet and the exact A:AA 27-column contract; template example rows and SQLite-only audit fields are excluded.

The default workbook path is `<批次>/CC_Codex 用户满意度标注（<批次>-第<题号>题）.xlsx`. Every invocation chooses a new suffix instead of overwriting an older workbook. Copy one original JSONL per selected question/session into the same batch directory using `轨迹_<批次>_第<题号>题_<原文件名>.jsonl`; choose a new suffix when a prior copy exists. Keep the Excel `轨迹文件` column blank because the user uploads the JSONL manually and fills that field afterward. The SQLite `trajectory_file` value remains unchanged for internal traceability.

Report the exported row count, duplicate status, workbook path, and every trajectory path. Never export internal SQLite-only fields.
