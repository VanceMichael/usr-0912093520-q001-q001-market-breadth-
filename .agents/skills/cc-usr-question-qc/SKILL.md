---
name: cc-usr-question-qc
description: Check SQLite-backed Coding Agent batch questions for duplicates and template-like similarity before production. Never inspect model responses, trajectories, or implementation output.
---

# CC USR Question QC

Review questions stored in `production.sqlite3` for duplication before any target-model run.

## Boundary

Read the duplicate-question rules in `项目规范.md` and [references/qc-rubric.md](references/qc-rubric.md). Inspect only stored prompts and the metadata needed for comparison. Do not inspect repositories, question implementation files, `~/.claude/projects`, target-model responses, generated diffs, or delivery ratings.

## Workflow

1. List a batch with `python3 tools/batch_pipeline.py --db production.sqlite3 list --batch <批次>`.
2. Run the read-only duplicate check with `python3 tools/batch_pipeline.py --db production.sqlite3 duplicate-check --batch <批次> --select <题号或区间>`. It compares the selection with every question in SQLite, including other batches.
3. Review the prompt pairs semantically for noun-swapped templates, repeated sentence structure, and substantially identical business flows that numeric similarity may miss.
4. Store the duplicate-review result with `qc-set`. When no duplicate is found, use `--decision pass --report '质检通过'` exactly; do not put metrics or explanations in a passing report. When duplicates are found, use `revise` or `reject` and record the concrete matching task IDs and evidence. If the separate mechanical gate has not passed, report that prerequisite instead of broadening this review.
5. A passing `qc-set` makes the question immediately `READY` when mechanical QC and the prompt fingerprint are current. This skill only updates readiness; it does not launch Claude Code.

Any prompt edit invalidates prior duplicate QC and makes the question `BLOCKED`. Re-run the comparison to restore `READY`.

## Decision rules

- Reject exact or near-duplicate prompts, including matches across different batches or repositories.
- Reject when overall normalized similarity is at least 82%, trigram Jaccard similarity is at least 30%, or the longest normalized common substring is at least 50 characters.
- Reject same-repository questions whose similarity-tag Jaccard overlap is at least 75%.
- Reject noun-swapped templates and substantially identical business flows even when the numeric thresholds do not trigger.
- Do not judge or reject based on task type, 0-1 intent, difficulty, prohibited topic, repository contents, snapshot accessibility, acceptance coverage, or reproducibility. Those belong to authoring and mechanical gates, not this duplicate-only skill.
- A passing QC report must contain exactly `质检通过`. Keep detailed comparison metrics in the execution output, not in the stored pass remark.
