---
name: cc-usr-question-qc
description: Check SQLite-backed Coding Agent questions for duplicates, template-like similarity, and natural Chinese requirement writing before production. Never inspect repositories, model responses, trajectories, or implementation output.
---

# CC USR Question QC

Review questions stored in `production.sqlite3` for duplication and authored-language quality before any target-model run.

## Boundary

Read the duplicate-question rules in `项目规范.md`, [references/qc-rubric.md](references/qc-rubric.md), and the authoring skill's [content-quality.md](../cc-usr-question-author/references/content-quality.md). Inspect only stored prompts and the metadata needed for comparison. Do not inspect repositories, question implementation files, `~/.claude/projects`, target-model responses, generated diffs, or delivery ratings. Snapshot content is checked by the separate mechanical gate.

## Workflow

1. List a batch with `python3 tools/batch_pipeline.py --db production.sqlite3 list --batch <批次>`.
2. Run the read-only duplicate check with `python3 tools/batch_pipeline.py --db production.sqlite3 duplicate-check --batch <批次> --select <题号或区间>`. It compares the selection with every question in SQLite, including other batches.
3. Review the prompt pairs semantically for noun-swapped templates, repeated sentence structure, and substantially identical business flows that numeric similarity may miss. Build a sentence map for the selected batch: opening perspective, primary action, clause order, and acceptance evidence. A batch of five or more must have at least three distinct opening perspectives and three distinct requirement/acceptance orders. Repeated spines such as “请搭建/实现一个 Go 服务” followed by the same input → rules → state/concurrency → persistence → tests sequence are a template finding even when metrics are low. Also read each prompt as a standalone request: it must be natural Chinese professional prose, contain a concrete business situation and observable result, and contain no headings, label chains, canned opening or evaluation context.
4. A duplicate or template finding is a repair gate. Rewrite every affected prompt in human Chinese prose before recording a final decision. Use the controlled update command, `python3 tools/batch_pipeline.py --db production.sqlite3 prompt-update --batch <批次> --number <题号> --prompt-file <临时文件>`, which resets the prompt fingerprint and QC fields; then rerun duplicate-check and mechanical QC. Repeat until the batch has no exact, numeric, semantic, or template finding. Do not treat `revise` as completion. Use `reject` only when an exact duplicate cannot be made distinct without changing the requested task; otherwise repair and pass.
5. Store the duplicate-review result with `qc-set`. When the repaired batch has no duplicate or template finding, use `--decision pass --report '质检通过'` exactly; do not put metrics or explanations in a passing report. If the separate mechanical gate has not passed, repair the prompt first but report that prerequisite instead of broadening this review.
6. A passing `qc-set` makes the question immediately `READY` when mechanical QC and the prompt fingerprint are current. This skill only updates readiness; it does not launch Claude Code.

Any prompt edit invalidates prior duplicate QC and makes the question `BLOCKED`. Re-run the comparison and mechanical QC to restore `READY`. A batch may not be published or launched while any prompt has an unresolved template finding.

## Decision rules

- Reject exact or near-duplicate prompts, including matches across different batches or repositories.
- Reject when overall normalized similarity is at least 82%, trigram Jaccard similarity is at least 30%, or the longest normalized common substring is at least 50 characters.
- Reject same-repository questions whose similarity-tag Jaccard overlap is at least 75%.
- Reject noun-swapped templates and substantially identical business flows even when the numeric thresholds do not trigger.
- Reject prompts that read like generated task specifications rather than a real request, including repeated “从零构建一套” openings, background/function/technology/acceptance label chains, uniform sentence cadence across the batch, evaluation terminology, and reusable requirement tails.
- Treat uniform cadence as a blocking defect: after removing business and technology nouns, two prompts that retain the same four-stage request sequence, or three prompts that reuse the same “搭建服务 + 并用测试覆盖” spine, fail even if all numeric similarity scores are below threshold. The fix must change the speaker's situation, causal order, and observable outcome, not just replace nouns.
- Do not judge or reject based on task type, 0-1 intent, difficulty, prohibited topic, repository contents, snapshot accessibility, acceptance coverage, or reproducibility. Those belong to authoring and mechanical gates, not this duplicate-only skill.
- A passing QC report must contain exactly `质检通过`. Keep detailed comparison metrics in the execution output, not in the stored pass remark. Do not write a passing report until the repair loop has been rerun successfully.
