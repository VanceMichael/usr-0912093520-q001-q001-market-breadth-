---
name: cc-usr-question-author
description: Create a named SQLite-backed batch of repository-based Coding Agent questions from 项目规范.md, including working folders and a review Markdown file. Do not score model output or analyze trajectories.
---

# CC USR Question Author

Turn the user's batch name, question count, and authoring requirements into one batch in `production.sqlite3`.

## Required context

1. Read the project-root `项目规范.md` completely.
2. Read [references/task-contract.md](references/task-contract.md).
3. Inspect existing SQLite questions and any source repositories needed for the requested tasks. Never inspect target-model sessions, responses, or output.

## Authoring rules

- Create exactly the requested number of distinct first-turn tasks. Every first turn must be `中等`, `困难`, or `地狱` from a human execution perspective.
- Apply the daily type preference in the specification. Across a batch, vary domains, repositories, opening perspective, architecture, state transitions, and acceptance evidence.
- Reject banned or saturated topics, generic CRUD work, CLI utilities, and prompts that become the same skeleton after replacing nouns.
- A prompt must depend on the prepared repository, require meaningful implementation judgment, and name observable acceptance behavior. Reject it when two or more over-simple traits apply.
- Bug prompts state reproducible symptoms and impact without revealing the root cause, target file, or fix.
- Do not create gold patches, hidden answers, ratings, dissatisfaction descriptions, or target-model runs.

## Batch creation

The user does not create folders or JSON. Build an internal temporary spec matching the contract, then run:

```bash
python3 tools/batch_pipeline.py --db production.sqlite3 create-batch \
  --workspace . --spec <temporary-spec.json>
```

The command creates `<批次名>/`, exactly N question workspaces, and `<批次名>/题目_<批次名>.md`. Prompts and metadata live only in SQLite; do not write prompt files into workspaces. Remove only the temporary spec you created after successful import.

Run mechanical question QC after creation and report every blocked item. A missing accessible GitHub commit permalink remains blocking. Do not mark semantic QC as passed unless you actually performed the repository-based review, and do not record human approval yourself.
