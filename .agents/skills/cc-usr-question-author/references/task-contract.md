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
      "task_type": "Feature 迭代",
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

Allowed task types: `0-1 代码生成`, `Feature 迭代`, `Bug 修复`, `代码理解`, `代码重构`, `工程化`, `代码测试`.

Allowed first-turn difficulties: `中等`, `困难`, `地狱`.

Each question folder is the exact working directory later passed to Codex. The folder starts as a clean Git repository. Prepare or copy the intended baseline into it, commit and publish it, then store its accessible GitHub permalink before production QC. Never put `.env`, API keys, prompts, QC reports, ratings, or internal difficulty evidence inside the question workspace.
