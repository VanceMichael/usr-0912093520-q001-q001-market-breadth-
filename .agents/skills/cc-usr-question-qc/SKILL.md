---
name: cc-usr-question-qc
description: Quality-check SQLite-backed first-turn 0-1 Coding Agent batch questions before production for intent, difficulty, banned topics, diversity, repository, snapshot, and reproducibility. Never inspect model responses or trajectories.
---

# CC USR Question QC

Review questions stored in `production.sqlite3` before any target-model run.

## Boundary

Read `项目规范.md` and [references/qc-rubric.md](references/qc-rubric.md). You may inspect question workspaces, repositories, commits, dependencies, prompts, and other question metadata. Never inspect `~/.claude/projects`, target-model responses, generated diffs, or delivery ratings.

## Workflow

1. List a batch with `python3 tools/batch_pipeline.py --db production.sqlite3 list --batch <批次>`.
2. Run deterministic checks with `python3 tools/batch_pipeline.py --db production.sqlite3 qc-check --batch <批次> --select <题号或区间>`.
3. Semantically check that every declared type is `0-1 代码生成` and that the prompt's first action and overall intent genuinely request a complete project or previously absent complete module. Also check first-turn difficulty, repository dependence, objective acceptance, single-file escape risk, prohibited topics, and template-like similarity.
4. Store the result with `qc-set --decision pass|revise|reject --report '<具体证据>'`. A pass records the current prompt fingerprint.
5. Give the human reviewer the prompt and concrete QC evidence. Do not run `approve` unless the human explicitly states that they reviewed and approved the selected questions.

Any prompt edit invalidates prior QC. Re-run the complete review. A semantic pass is only a recommendation; human approval remains a separate gate.

## Decision rules

- Reject every first-turn task whose declared type is not `0-1 代码生成`.
- Reject prompts that are actually an extension, repair, explanation, refactor, engineering-only change, or test-only task even when their stored type says 0-1. The opening action and overall request must both describe building a complete project or previously absent complete module.
- Require one coherent natural-language paragraph in a realistic user voice. Reject sectioned requirement lists, canned prompt scaffolds, and prompts with a reusable engineering tail.
- Require enough business depth for a multi-file result, including at least four distinct responsibility areas. Reject isolated functions, thin CRUD shells, and tasks that can reasonably be completed in one source file.
- Reject first-turn `简单` tasks and tasks with two or more over-simple traits.
- Reject banned, saturated, highly similar, or noun-swapped template tasks.
- Reject mismatched task types, unverifiable acceptance, exposed fixes, credentials, or inaccessible snapshots.
- Require a full GitHub commit permalink, a clean prepared workspace, accurate reproducibility metadata, and concrete repository evidence. Reject a baseline that already implements the complete project or module requested by the prompt.
