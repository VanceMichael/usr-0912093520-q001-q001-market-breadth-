---
name: cc-usr-delivery-qc
description: Quality-check existing SQLite delivery records for a batch, repair noncompliant values from verifiable evidence, rerun validation, and record 质检通过. Do not produce records, export Excel, or modify target-model work.
---

# CC USR Delivery QC

Make every delivery record conform to `项目规范.md` before export. This is a separate step after `$cc-usr-delivery-producer`; do not export Excel here.

## Stage boundary

The subject of this skill is the delivery content already stored in SQLite. Read those records first and compare them with the authoritative question, registered run, original Claude JSONL, initial snapshot, resulting code, diff, and verification evidence. Do not silently regenerate the batch or replace production with a new summary. An unattended scheduler may explicitly start this as a separate task after production; completion of the producer process alone never lets that process continue into QC.

## Required context

Read `项目规范.md`, [references/delivery-qc-checklist.md](references/delivery-qc-checklist.md), the producer's [record contract](../cc-usr-delivery-producer/references/record-contract.md), and [scoring rubric](../cc-usr-delivery-producer/references/scoring-rubric.md).

## Workflow

质检不是只报告问题的检查步骤，而是“发现即处理”的闭环。任何错误、警告、缺失字段、轨迹不匹配、分数与描述冲突、轮次链断裂或可疑证据一旦发现，必须立即回到权威来源核对并修正；不得先记录“质检通过”、不得带着已知问题导出，也不得把问题留给用户手工补。

1. Load the existing production set, confirm it is complete, and check every record in the requested batch:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911
```

For an explicitly selected question subset, add `--select 1,3-5`; the same selector may be used with `--fixes` and `--finalize` so that a single-question pipeline does not validate unrelated batch records.

2. Review all 28 export fields, not just the reported mechanical errors. Confirm exact prompts, identifiers and trajectory file names, the one-based `当前对话轮次排序`, valid enums and score ranges, snapshot and session consistency, timestamps, parent chains, score-description agreement, and natural evidence-based descriptions. `审核备注` is populated only by finalization. Treat every reported error and warning as an immediate repair task; do not defer it or mark the batch passed while it remains.
   - Recheck every successful functionality claim against verification completed inside that target turn and preserved in its original JSONL. Static reading, code shape, file existence, and final-response prose cannot support success by themselves. When frontend work is present, require browser-level interaction evidence; backend-only records do not need a browser test.
   - A failed test or runtime check does not justify dropping the produced record. Preserve it, ensure the description names the failed behavior and consequence, and lower the affected score. Only an unrecoverable trajectory, identity, turn-boundary, or contamination failure keeps the record blocked.
   - Every five-dimension description must remain one Chinese paragraph with at least 45 Chinese characters and no more than 420 total characters.
   - For every row, resolve the original JSONL from the successful run linked by the record's `session_id`. A non-empty `runs.trajectory_root` is authoritative; otherwise derive the isolated run directory, using legacy batch-folder copies only when no isolated directory exists. Missing, unreadable, malformed, empty, or ambiguous files are blocking errors.
   - Match normal records to exactly one byte-for-byte `(SessionID, PromptID, user_prompt)` event. For a verified `继续` record, match `raw_turn_id` and `raw_user_prompt=继续` to its own event, verify the exported prompt and PromptID reuse the nearest preceding interrupted task, and verify the consecutive continuation count. Require real-user turn order, complete order-sensitive tool-use/tool-result pairing, and a final assistant text response; the interrupted turn may omit final text only when its immediate next real user event is `继续`. A terminal `stream-json` output, execution summary, approximate text match, or homepage URL is not evidence and must fail QC.
   - Inspect the full matched session for context pollution. Any `Skill`/slash-command invocation or read of external skill, `CLAUDE.md`, `AGENTS.md`, Claude settings, or Codex config outside the question workspace is blocking and cannot be repaired by editing SQLite or deleting trajectory events; the model question must run again from its clean initial snapshot.
   - Ignore Claude-generated `<task-notification>`, meta and compact-summary events as user turns. Reject a trajectory that mixes another SessionID, has an ambiguous main JSONL, has a file name inconsistent with SessionID, or lacks complete ordered tool-use/tool-result pairs.
   - Semantically review every five-dimension description and non-empty `其他问题`. Reject AI/evaluator/scoring/generation language and English evaluator labels, while preserving necessary paths, commands, classes, protocols, frameworks, identifiers and verified numbers. `其他问题` must add an issue outside the five dimensions rather than repeat one.
   - A perfect-score description needs a concrete verification action and objective result. A non-max description needs a precise evidence location, actual behavior and engineering consequence. Planning must discuss decomposition/state tracking, reasoning must identify a false premise or omitted branch, and execution must describe the tool action plus retry, recovery or verification sequence.
   - Compare pending descriptions across the batch. Reused long fragments and stock endings are blocking even when each record passes in isolation.
   - Apply the score ceilings in the producer scoring rubric. In particular, prose admitting no plan/status tracking cannot carry planning 4/5; prose admitting several failures, repeated retries, or unresolved verification cannot carry execution 4/5. Do not “fix” contradiction by deleting the evidence; lower the score or recover stronger evidence.
3. Do not stop after listing a correctable issue. Resolve it immediately from authoritative evidence: SQLite question/run metadata for inherited fields; the matched session for SessionID, PromptID, prompts, turns, timestamps, and process evidence; and the initial snapshot plus resulting workspace for product evidence. Never guess a missing value. If the first repair does not clear the issue, continue the repair-and-recheck loop in the same task and report the exact blocker only when the source evidence is genuinely unavailable.
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

5. Apply the corrections immediately, delete the temporary JSON after it is accepted, and rerun the full check in the same workflow. Continue the repair-and-recheck loop until the report has zero errors and zero warnings. A non-zero validation exit, an unchanged error, or a newly introduced warning means the batch is still blocked and must not be finalized:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911 \
  --fixes <临时修正文件.json>
```

6. Finalize only after the complete batch is compliant and the immediately preceding validation report has zero errors and zero warnings. If finalization itself reports a problem, return to the same evidence-based repair loop rather than treating the partial result as passed:

```bash
python3 .agents/skills/cc-usr-delivery-qc/scripts/validate_records.py \
  --db production.sqlite3 --batch 0911 --finalize
```

This writes `delivery_qc_passed=1`, `delivery_qc_note=质检通过`, and the check time for every record. It does not open the final delivery gate. End with `批次 <批次名>：质检通过（<数量> 条记录）` and leave the records for a separate human or Codex five-dimension review.

The validator's trajectory gate is mandatory. Do not replace it with a manual claim, a different trajectory root, a copied summary, or an older report. If the authoritative run directory has been deleted, keep the batch unpassed until the original evidence is recovered or the target model is rerun cleanly. Trajectory contamination is not fixable metadata; reset the question safely and create a new isolated model run.

## Boundaries

Corrections may change only supported exported fields and must be recorded in `delivery_qc_changes` with the before/after values and reason. `审核备注` is controlled by the finalization command and cannot be supplied as a repair. Do not alter record linkage, audit provenance, target-model trajectories, responses, or repository files. Do not rewrite a valid field merely for stylistic preference. If a required value cannot be recovered from evidence, leave the batch unpassed and state the exact missing source; fabricating a compliant-looking value is forbidden.
