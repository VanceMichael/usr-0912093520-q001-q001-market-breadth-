# SQLite batch contract

Use an internal JSON object only as input to `tools/batch_pipeline.py create-batch`. It is not a user-maintained artifact.

```json
{
  "batch": "0911",
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
      "similarity_tags": ["domain", "state-transition"]
    }
  ]
}
```

For batch authoring, every first-turn `task_type` must be `0-1 代码生成`. Other task types remain valid only for later conversation turns and delivery records; they are not accepted by `create-batch`.

Allowed first-turn difficulties: `中等`, `困难`, `地狱`.

Difficulty calibration is evidence-based, not a label chosen for balance. Count the independent high-complexity traits in the real prompt and starting scaffold: cross-module/service state, event ordering or versioning, idempotency/concurrency, transaction boundaries, timezone or cross-midnight behavior, durable SQLite/database state, restart/recovery, container/build/deployment constraints, security/permissions, and broad integration verification. Three or more traits make `困难` the minimum; five or more, hidden constraints, or architecture-level decisions with many boundary cases make `地狱` appropriate. A `中等` task should have only a few ordinary edge cases and no large system-constraint cluster. Put the counted traits and the reason for the selected band in `difficulty_evidence`; do not use “需要多文件” alone as evidence.

Each prompt is one natural-language paragraph whose primary action is to build a complete project or a complete module from zero. It must not describe extending or repairing an already implemented capability.

Each question folder is the exact working directory later passed to Codex. The folder starts as a clean Git repository. Prepare or copy only the intended scaffold, contracts, fixtures, or other genuine starting context; do not pre-implement the requested complete project or module. Commit and publish it, then store its accessible GitHub permalink before production QC. Never put `.env`, API keys, prompts, QC reports, ratings, or internal difficulty evidence inside the question workspace.

Snapshot publication is part of batch authoring, not a user prerequisite. For every question, the authoring workflow must create or select a dedicated evaluation-accessible GitHub repository, push the clean baseline, verify the remote default branch resolves to the local 40-character `HEAD`, and register the matching repository URL and commit permalink with `tools/batch_pipeline.py set-repo`. `initial_snapshot` must be exactly `https://github.com/<org>/<repo>/commit/<40 hexadecimal characters>` for that same SHA; a repository homepage, branch, tag, short SHA, or “latest commit” URL is invalid. Re-read SQLite after registration and compare `repo_url`, `initial_snapshot`, and `local_initial_sha` to the verified values. A batch with any missing, inaccessible, dirty, or mismatched snapshot is incomplete and must remain blocked.
