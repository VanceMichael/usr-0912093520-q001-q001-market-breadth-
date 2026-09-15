---
name: cc-usr-question-author
description: Create a named SQLite-backed batch of repository questions in either 0-1 mother mode or a non-0-1 derived mode, including working folders, published GitHub snapshots, mother-library lineage, and a review Markdown file. Do not score model output or analyze trajectories.
---

# CC USR Question Author

Turn the user's batch name, question count, and authoring requirements into one batch in `production.sqlite3`.

## Required context

1. Read the project-root `项目规范.md` completely.
2. Read [references/task-contract.md](references/task-contract.md).
3. Read [references/content-quality.md](references/content-quality.md) before writing prompts or preparing snapshot files.
4. Read [references/mother-library.md](references/mother-library.md) when the request uses derived mode.
5. Inspect existing SQLite questions and any source repositories needed for the requested tasks. Never inspect target-model sessions, responses, or output.

## Authoring rules

- Current project scope is backend-only. Use exactly one principal backend stack per task from Go, Python, Node.js (JavaScript or TypeScript), or Java, and keep reasonable stack diversity across a batch. Backend APIs, messaging, databases, caching, scheduled work, concurrency, reliability, security boundaries, observability, and automated tests are allowed. Do not request or create web pages, admin interfaces, browser clients, HTML/CSS, React, Vue, Angular, Svelte, mini programs, other visual UI, or full-stack work. Acceptance must be demonstrable through backend interfaces, tests, commands, or persisted state without browser interaction.
- Select exactly one mode from the user request. In `author_mode: "0-1"`, create exactly the requested number of distinct new mother tasks; every task uses `task_type: "0-1 代码生成"`. In `author_mode: "derived"`, select one valid row from `mother_library`, create independent non-0-1 tasks (at minimum `Bug 修复` or `Feature 迭代`), and preserve the mother relation in SQLite. Never silently turn a derived request into a new 0-1 batch.
- The operator is not required to choose a mother or task type manually. When the request only says to follow `项目规范.md`, choose a published, buildable mother with a matching readiness flag and the lowest usage count; prefer `Bug 修复` when `bugfix_ready=1`, otherwise use `Feature 迭代` when `iteration_ready=1`. Show the selected evidence in the record and let the server repeat the same selection if the UI sends no choice. A manual override is allowed only when the user explicitly names one.
- Every task must be `困难` or `地狱` from a human execution perspective. `简单` and `中等` are no longer accepted by the delivery contract. Bug or Feature work is allowed in derived mode even when the mother project has small defects. A minor defect that does not prevent build, startup, or the main workflow is evidence to record, not a reason to discard the mother. Block only for a materially unusable project, inaccessible or mismatched snapshot, missing source workspace, or a defect that makes the requested derived behavior impossible.
- Apply the difficulty gate before writing the batch spec. Count the independent high-complexity traits in the actual prompt and scaffold: cross-module or cross-service state, event/version ordering, idempotency or concurrency, transaction boundaries, timezone or cross-midnight rules, durable SQLite/database state, restart/recovery behavior, container/build/deployment constraints, permission/security boundaries, or substantial integration testing. Three or more traits are required for `困难`; five or more traits, hidden constraints, or architecture-level choices that are likely to defeat a strong engineer justify `地狱`. A candidate below the `困难` threshold must be strengthened with real, coherent business constraints or discarded, never mislabeled. Record the concrete traits in `difficulty_evidence`; never choose a difficulty from the prompt's apparent length or from the expected model success rate.
- Make the prompt's first action and overall intent ask for a complete project or a complete module that does not already exist. Do not relabel an extension, repair, or isolated function as 0-1.
- Require actual executable code, a startable result, and relevant automated tests in the task's own business flow. Never place a standalone `TODO` token in a User Prompt, even to forbid unfinished work, because external classifiers may misread it as a saturated todo-application category. State the completion boundary in natural Chinese instead.
- Write each User Prompt as one coherent Chinese paragraph in a realistic professional voice. Start from a concrete scenario, role, or operational pain point; avoid headings, checklist formatting, canned openings, repeated noun-swapping structures, and a reusable engineering-requirements tail. Before import, review the whole batch continuously and rewrite any prompt whose cadence resembles another one, contains AI/Codex/Claude self-reference or internal scoring, QC, trajectory, Prompt, model-performance, or generation-process language, or uses non-technical English evaluator words such as `rationale`, `overall`, `generally`, and `basically`. Preserve necessary technical names, protocols, commands, paths, and code identifiers in English.
- Treat human-language diversity as a hard pre-import gate, not a style preference. Before writing the spec, build a private sentence map for the batch recording each prompt's opening situation, primary user action, clause order, and acceptance evidence. For batches of five or more, use at least three genuinely different opening perspectives and at least three different requirement/acceptance orders. The phrases “请搭建/实现一个 Go 服务” and “并用测试覆盖/验证” may not be reused as the default spine in more than two prompts.
- Remove business nouns, technology names, and field names mentally, then compare the remaining sentence skeletons. If two prompts retain the same four-stage sequence (for example input → rules → state/concurrency → persistence → tests), or share a reusable tail of requirements, rewrite them before import. A batch is not ready when its prompts could be produced by replacing nouns in one template. Record the concrete rewrites in the private authoring notes; never put those notes in the question repository.
- Run the read-only duplicate comparison against all SQLite questions before snapshot publication. Any numeric or semantic template hit blocks import/publication. Use the controlled `prompt-update` command for every correction so the stored prompt hash and all QC fields are invalidated; then rerun the comparison. Do not leave a batch in `revise` as a substitute for rewriting it.
- Keep prompts business-led. Mention implementation technology only when the user explicitly requires it or it is the core subject of the task; otherwise put language and framework choices in metadata and let the prepared repository constrain them.
- Give every prompt enough business depth to require a multi-file result: cover at least four distinct responsibility areas such as an input boundary, domain rules, durable state, validation and failure handling, queries or reporting, and executable verification scenarios.
- Across a batch, vary domains, repositories, opening perspective, architecture, state transitions, and acceptance evidence.
- Reject banned or saturated topics, generic CRUD work, CLI utilities, and prompts that become the same skeleton after replacing nouns.
- A prompt must depend on its prepared repository or scaffold, require meaningful implementation judgment, and name observable acceptance behavior. The baseline must not already implement the requested complete project or module. Reject the task when two or more over-simple traits apply.
- Do not create gold patches, hidden answers, ratings, dissatisfaction descriptions, or target-model runs.
- Do not use model-like summary language in authoring notes or review Markdown. Write only concrete observations in short, varied sentences. Avoid repeated patterns such as “本轮主要是…顺序基本合理…无法证明…”, “最终工作区包含…逐条核对后没有发现…”, or a fixed “事实罗列 + 转折结论” paragraph. Do not manufacture exact tool counts, test counts, architecture labels, or conclusions that are not directly observed. The authoring record should read like a person recording what they actually checked, with uneven sentence length and no five-dimension evaluation template.
- A failed duplicate/template review requires repair, not only a label. Rewrite the affected prompt in natural Chinese prose, run `python3 tools/batch_pipeline.py --db production.sqlite3 prompt-update --batch <批次> --number <题号> --prompt-file <临时文件>`, remove the temporary file, and rerun both duplicate and mechanical QC. Do not publish or launch while any prompt still has a template finding.
- Treat `repeated_terminal_sentence`, checklist-style validation tails, and generic environment commentary as blocking. Rewrite the surrounding business outcome rather than changing punctuation or a single technical noun.
- For automatic 0-1 batches, enforce the batch-level technology diversity gate in `content-quality.md`. Use only Go, Python, Node.js/JavaScript/TypeScript, and Java as primary backend languages; keep the task backend-only and make every initial project Docker-runnable.
- Snapshot contents are part of the product, not part of the production workflow. Project-authored README files, design notes, comments, docstrings, examples, fixtures, and user-facing text must use natural Chinese except for necessary technical names and code identifiers. Do not place prompts, task instructions, difficulty notes, evaluation terminology, model names, QC reports, or wording such as “starting workspace” and “implementation is left to the task owner” in the question repository.

## Batch creation

The user supplies only the batch name, question count, and authoring requirements. Do not ask the user to create folders, JSON, GitHub repositories, commits, or snapshot links. Build an internal temporary spec matching the contract, then run:

```bash
python3 tools/batch_pipeline.py --db production.sqlite3 create-batch \
  --workspace . --spec <temporary-spec.json>
```

The command creates `<批次名>/`, exactly N question workspaces, and `<批次名>/题目_<批次名>.md`. Prompts and metadata live only in SQLite; do not write prompt files into workspaces. Remove only the temporary spec you created after successful import.

## Publish initial snapshots

Before mechanical QC, complete all of the following for every question without handing steps back to the user:

1. Prepare the intended initial scaffold in its question folder. Ensure `.gitignore` excludes credentials and local environment files. Review every tracked document and user-facing prose file against `content-quality.md`, then commit the complete clean baseline with a project-oriented Chinese commit message.
2. Create a dedicated repository under the authenticated GitHub account, add it as `origin`, and push the baseline without force-pushing. Use a collision-free repository name tied to the batch and question.
3. Verify the remote is accessible to the evaluation team and that its default branch resolves to the exact local `HEAD`. A private personal repository is not acceptable.
4. Build the immutable commit permalink from that 40-character SHA and register it with `tools/batch_pipeline.py set-repo`.
5. Re-read the SQLite row and verify `repo_url`, `initial_snapshot`, and `local_initial_sha` agree. Do not modify the baseline after registration; any necessary change requires a new normal commit, push, and snapshot registration before QC.

Snapshot hard gate (run for every question before mechanical QC):

- `local_initial_sha` must equal `git -C <question-folder> rev-parse HEAD` and must match exactly 40 hexadecimal characters. A short SHA, branch URL, tag URL, repository homepage, or latest-commit URL is invalid.
- `initial_snapshot` must match `https://github.com/<org>/<repo>/commit/<same-40-char-SHA>` and `repo_url` must be the same repository without the `/commit/...` suffix.
- Verify the remote commit with `git ls-remote origin HEAD` (or the resolved default branch) and verify the GitHub permalink is reachable. If either check fails, leave the question blocked; do not substitute a plausible URL or continue to launch.
- After `set-repo`, query SQLite again and compare all three values (`repo_url`, `initial_snapshot`, `local_initial_sha`) to the values just verified. Capture the command output or a small audit note in the run log so a later reviewer can reproduce the check.

If GitHub authentication, repository creation, push, or accessibility verification fails, keep the question `BLOCKED` and report the concrete blocker. Never claim the batch is complete or ready while any question lacks its published snapshot.

Run mechanical question QC only after every snapshot is registered. Treat every reported document-language, workflow-leakage, or prompt-style error as blocking; correct the project content, create a new normal commit, push it, and register the new snapshot before rerunning QC. Report every blocked item. For derived mode, increment the selected mother's usage count and insert one `mother_usages` row per derived question only after the question row is successfully created. Do not mark duplicate QC as passed unless you actually performed that review, and do not launch a target model.
