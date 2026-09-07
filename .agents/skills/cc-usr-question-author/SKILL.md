---
name: cc-usr-question-author
description: Create a named SQLite-backed batch of repository-based, first-turn 0-1 Coding Agent questions from 项目规范.md, including working folders and a review Markdown file. Do not score model output or analyze trajectories.
---

# CC USR Question Author

Turn the user's batch name, question count, and authoring requirements into one batch in `production.sqlite3`.

## Required context

1. Read the project-root `项目规范.md` completely.
2. Read [references/task-contract.md](references/task-contract.md).
3. Inspect existing SQLite questions and any source repositories needed for the requested tasks. Never inspect target-model sessions, responses, or output.

## Authoring rules

- Create exactly the requested number of distinct first-turn tasks. Every task must use `task_type: "0-1 代码生成"` and be `中等`, `困难`, or `地狱` from a human execution perspective. Bug, Feature, understanding, refactoring, engineering, and test prompts are reserved for later turns and must not be authored into a new batch.
- Make the prompt's first action and overall intent ask for a complete project or a complete module that does not already exist. Do not relabel an extension, repair, or isolated function as 0-1.
- Write each User Prompt as one coherent natural-language paragraph in a realistic user voice. Start from the scenario, role, or pain point; avoid headings, checklist formatting, canned openings, and a reusable engineering-requirements tail.
- Keep prompts business-led. Mention implementation technology only when the user explicitly requires it or it is the core subject of the task; otherwise put language and framework choices in metadata and let the prepared repository constrain them.
- Give every prompt enough business depth to require a multi-file result: cover at least four distinct responsibility areas such as an input boundary, domain rules, durable state, validation and failure handling, queries or reporting, and executable verification scenarios.
- Across a batch, vary domains, repositories, opening perspective, architecture, state transitions, and acceptance evidence.
- Reject banned or saturated topics, generic CRUD work, CLI utilities, and prompts that become the same skeleton after replacing nouns.
- A prompt must depend on its prepared repository or scaffold, require meaningful implementation judgment, and name observable acceptance behavior. The baseline must not already implement the requested complete project or module. Reject the task when two or more over-simple traits apply.
- Do not create gold patches, hidden answers, ratings, dissatisfaction descriptions, or target-model runs.

## Batch creation

The user does not create folders or JSON. Build an internal temporary spec matching the contract, then run:

```bash
python3 tools/batch_pipeline.py --db production.sqlite3 create-batch \
  --workspace . --spec <temporary-spec.json>
```

The command creates `<批次名>/`, exactly N question workspaces, and `<批次名>/题目_<批次名>.md`. Prompts and metadata live only in SQLite; do not write prompt files into workspaces. Remove only the temporary spec you created after successful import.

Run mechanical question QC after creation and report every blocked item. A missing accessible GitHub commit permalink remains blocking. Do not mark semantic QC as passed unless you actually performed the repository-based review, and do not record human approval yourself.
