# SQLite batch contract

Use an internal JSON object only as input to `tools/batch_pipeline.py create-batch`. It is not a user-maintained artifact.

```json
{
  "batch": "0911",
  "author_mode": "0-1",
  "brief": "用户给出的本批次出题要求",
  "questions": [
    {
      "folder": "q001",
      "task_id": "0911-001",
      "title": "简短题名",
      "prompt": "首轮完整 User Prompt 原文",
      "task_type": "0-1 代码生成",
      "difficulty": "困难",
      "languages": ["Go", "React"],
      "repo_url": "https://github.com/org/repo",
      "initial_snapshot": "https://github.com/org/repo/commit/40-character-sha",
      "reproducibility": "无外部依赖",
      "expected_areas": ["api", "web"],
      "difficulty_evidence": ["需要追踪跨模块状态流"],
      "similarity_tags": ["domain", "state-transition"],
      "mother_id": null
    }
  ]
}
```

`author_mode` is required in new specs and defaults to `0-1` for older callers. In `0-1` mode every first-turn `task_type` must be `0-1 代码生成`; successful source rows are registered in `mother_library`. In `derived` mode, `mother_id` must reference an existing mother row and each first-turn task must use a non-0-1 type, such as `Bug 修复` or `Feature 迭代`. `create-batch` records `questions.mother_id`, one `mother_usages` row per derived question, and increments `mother_library.use_count` transactionally. The derived Prompt remains a new first-turn requirement in its own workspace; it is not a later conversational turn copied from the mother.

The mother row is the source of truth for the original 0-1 Prompt, local workspace, GitHub URL, immutable snapshot, local SHA, usage count, recent usage, readiness flags, and defect notes. A small known defect may be retained and used as the subject of a bug-fix or iteration task. Do not reject it unless the project cannot build/start, the repository or snapshot is inaccessible/mismatched, or the requested derived behavior cannot be isolated.

Allowed first-turn difficulties: `中等`, `困难`, `地狱`.

Difficulty calibration is evidence-based, not a label chosen for balance. Count the independent high-complexity traits in the real prompt and starting scaffold: cross-module/service state, event ordering or versioning, idempotency/concurrency, transaction boundaries, timezone or cross-midnight behavior, durable SQLite/database state, restart/recovery, container/build/deployment constraints, security/permissions, and broad integration verification. Three or more traits make `困难` the minimum; five or more, hidden constraints, or architecture-level decisions with many boundary cases make `地狱` appropriate. A `中等` task should have only a few ordinary edge cases and no large system-constraint cluster. Put the counted traits and the reason for the selected band in `difficulty_evidence`; do not use “需要多文件” alone as evidence.

Each prompt is one natural Chinese paragraph whose primary action is to build a complete project or a complete module from zero. It must not describe extending or repairing an already implemented capability. Before import, review every prompt in the batch and reject AI/model self-reference, internal scoring/QC/trajectory/Prompt/generation language, copied sentence frames, and non-technical English evaluator wording. Necessary technical names, protocols, commands, paths, and identifiers remain valid. Apply [content-quality.md](content-quality.md) before publication.

The prompt itself must require actual executable code, a startable result, and relevant automated tests. It must not contain a standalone `TODO` token, including in a negative clause; describe completed delivery behavior in natural Chinese instead so an external category classifier cannot mistake the task for a todo application.

Human-language diversity is a hard contract for the whole batch. For five or more questions, the private authoring review must show at least three distinct opening perspectives and three distinct requirement/acceptance orders. After removing business nouns, technologies, and field names, prompts may not share the same four-stage request sequence, and three prompts may not share a “搭建/实现服务 + 并用测试覆盖” spine. A prompt that only swaps nouns in a batch template is invalid even when numeric duplicate metrics are low. Rewrite it with a different speaker, causal order, and observable outcome before import or publication.

When a duplicate or template finding is discovered after import, update the stored prompt only through `tools/batch_pipeline.py prompt-update --batch <批次> --number <题号> --prompt-file <临时文件>`. This command invalidates the prompt fingerprint and dependent QC fields; rerun duplicate and mechanical QC before the question can be marked ready. A `revise` label without a prompt rewrite is not completion.

Each question folder is the exact working directory later passed to the target harness. The folder starts as a clean Git repository. Prepare or copy only the intended scaffold, contracts, fixtures, or other genuine starting context; do not pre-implement the requested complete project or module. Project-authored documents and user-facing prose use Chinese and discuss only the product domain, architecture, interfaces, data, operation, or development conventions. Commit and publish the repository, then store its accessible GitHub permalink before production QC. Never put `.env`, API keys, prompts, task instructions, QC reports, ratings, evaluation language, model names, or internal difficulty evidence inside the question workspace.

Snapshot publication is part of batch authoring, not a user prerequisite. For every question, the authoring workflow must create or select a dedicated evaluation-accessible GitHub repository, push the clean baseline, verify the remote default branch resolves to the local 40-character `HEAD`, and register the matching repository URL and commit permalink with `tools/batch_pipeline.py set-repo`. `initial_snapshot` must be exactly `https://github.com/<org>/<repo>/commit/<40 hexadecimal characters>` for that same SHA; a repository homepage, branch, tag, short SHA, or “latest commit” URL is invalid. Re-read SQLite after registration and compare `repo_url`, `initial_snapshot`, and `local_initial_sha` to the verified values. A batch with any missing, inaccessible, dirty, or mismatched snapshot is incomplete and must remain blocked.
