---
name: cc-usr-delivery-qc
description: Quality-check every required SQLite delivery field for a batch, repair noncompliant values from verifiable evidence, rerun validation, and record 质检通过. Do not export Excel or modify target-model work.
---

# CC USR Delivery QC

Make every delivery record conform to `项目规范.md` before export. This is a separate step after `$cc-usr-delivery-producer`; do not export Excel here.

## Required context

Read `项目规范.md`, [references/delivery-qc-checklist.md](references/delivery-qc-checklist.md), the producer's [record contract](../cc-usr-delivery-producer/references/record-contract.md), and [scoring rubric](../cc-usr-delivery-producer/references/scoring-rubric.md).

## Workflow

1. Check every record in the requested batch:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911
```

2. Review all 27 export fields, not just the reported mechanical errors. Confirm exact prompts, identifiers and trajectory file names, valid enums and score ranges, snapshot and session consistency, timestamps, parent chains, score-description agreement, and natural evidence-based descriptions. `审核备注` is populated only by finalization.
   - For every row, open the original JSONL named by `trajectory_file` and match the exact `(SessionID, PromptID, user_prompt)` triple. A terminal `stream-json` output, an execution summary, a prior-turn prompt, or a homepage URL is not evidence and must fail QC. A `继续` turn must contain the literal current user text `继续` while inheriting only the allowed classification metadata.
   - Apply the score ceilings in the producer scoring rubric. In particular, prose admitting no plan/status tracking cannot carry planning 4/5; prose admitting several failures, repeated retries, or unresolved verification cannot carry execution 4/5. Do not “fix” contradiction by deleting the evidence; lower the score or recover stronger evidence.
3. Do not stop after listing a correctable issue. Resolve it from authoritative evidence: SQLite question/run metadata for inherited fields; the matched session for SessionID, PromptID, prompts, turns, timestamps, and process evidence; and the initial snapshot plus resulting workspace for product evidence. Never guess a missing value.
4. Put supported corrections in a temporary JSON file. Each changed record needs a concrete reason:

```json
{
  "records": [
    {
      "record_id": "0911-001-T01",
      "reason": "提交人字段误写为工具名称，已按根目录配置改为实际提交人",
      "changes": {"submitter": "真实姓名"}
    }
  ]
}
```

5. Apply the corrections, delete the temporary JSON after it is accepted, and rerun the full check. Continue until no error or warning remains:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911 \
  --fixes <临时修正文件.json>
```

6. Finalize only after the complete batch is compliant:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911 --finalize
```

This writes `delivery_qc_passed=1`, `delivery_qc_note=质检通过`, and the check time for every record. End with `批次 <批次名>：质检通过（<数量> 条记录）`.

## Boundaries

Corrections may change only supported exported fields and must be recorded in `delivery_qc_changes` with the before/after values and reason. `审核备注` is controlled by the finalization command and cannot be supplied as a repair. Do not alter record linkage, audit provenance, target-model trajectories, responses, or repository files. Do not rewrite a valid field merely for stylistic preference. If a required value cannot be recovered from evidence, leave the batch unpassed and state the exact missing source; fabricating a compliant-looking value is forbidden.
