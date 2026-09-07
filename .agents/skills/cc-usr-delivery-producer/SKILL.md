---
name: cc-usr-delivery-producer
description: After Claude Code runs, locate its sessions, extract SessionID and PromptID, inspect each turn and resulting code, assign five evidence-based scores, write concrete descriptions, and store delivery records in production SQLite. Do not change target-model output or author questions.
---

# CC USR Delivery Producer

Produce one scored SQLite delivery record for every valid Claude Code user-prompt/model-response turn.

## Required context

Read `项目规范.md`, [references/scoring-rubric.md](references/scoring-rubric.md), and [references/record-contract.md](references/record-contract.md). Work only on launched questions in the requested batch. This skill is the immediate next step after the target-model run finishes.

## Evidence boundary

- Read the SQLite question and registered run, the matching Claude Code trajectory under `~/.claude/projects`, and the question workspace produced by that run.
- Use the trajectory only to locate turns, assess the model's process, and extract the exact `SessionID`, each user message's `PromptID`, prompts, timestamps, tool calls, and responses.
- Inspect the initial snapshot, current Git diff and code, and relevant verification results to assess the product. Run reasonable read-only or test commands when needed, but never repair, rewrite, or improve the target-model output.
- Do not infer evidence that is absent. If the trajectory match, turn boundary, identifier, or product state is ambiguous, stop and report the blocker instead of guessing.

## Workflow

1. Query SQLite for the selected question, initial prompt, workspace, snapshot, and latest registered Claude Code run.
2. Locate the matching session with:

```bash
python3 .agents/skills/cc-usr-delivery-producer/scripts/find_claude_turns.py \
  --db production.sqlite3 --batch <批次> --question <题号>
```

3. Confirm the locator matched the exact workspace and first-turn prompt. Read the selected trajectory and split it into valid turns. Exclude pure network or model-service failures; count a human `继续` after thinking-limit exhaustion.
4. For every valid turn, extract the exact session ID, user-message prompt ID, and matched JSONL trajectory file name. All turns from one window must share a `SessionID` and trajectory file; every turn must have a distinct `PromptID`.
5. Evaluate that turn against the five score dimensions in `项目规范.md`. Compare claims in the response with actual tool activity, repository changes, code, and verification evidence.
6. Write all five descriptions in natural Chinese from a professional programmer's perspective. Each deficiency must make the point of failure, the concrete behavior or defect, and its practical consequence understandable from the prose itself; add the root cause or a better approach only when the evidence supports it. Do not expose these requirements as labels or force every field into the same sentence structure. Cover process and product separately when both have issues. Even a score of 5 needs concrete verification evidence.
7. Create a temporary input JSON matching the record contract and set `human_authored` to `false`. Read `CC_USR_SUBMITTER` from the root `.env` for `submitter`; if it is absent, require the user's real name before insertion. Never put a tool, model, or invented identity in this field. Store the record with `collect_record.py --from-json`, then delete only that temporary input after a successful insert.
8. Store turns in chronological order. First-turn metadata comes from SQLite; later turns use that turn's exact prompt, task type, difficulty, and languages. Never overwrite an existing question/turn record.
9. After every valid turn is stored, hand the batch to `$cc-usr-delivery-qc`. Do not approve records or export Excel in this skill.

## Output quality

Keep scores and descriptions internally consistent. Name exact steps, commands, files, functions, errors, constraints, or missing requirements. Do not use unsupported adjectives or generic claims such as “表现一般”, “代码有 bug”, or “任务没完成”. Do not blame network or environment failures on model capability.

The five exported descriptions contain only task evidence and engineering judgment. Never mention the evaluator, authorship, generation method, automation, trajectory-based generation, or internal audit fields. In particular, do not write self-referential phrases containing `AI`, `Codex`, “自动生成”, or “基于轨迹生成”. Preserve an exact business-domain name only when it is itself necessary evidence, such as a product or API identifier.

Avoid formulaic prose: do not print `When`/`What`/`Impact`, “过程：”/“产物：”, arrows, bracketed placeholders, or stock openings such as “经检查”“根据轨迹”“综合来看”. Give each dimension its own evidence, vary sentence length and structure naturally, and do not append the same closing judgment to every field. Read the five descriptions together before insertion; rewrite any pair that sounds interchangeable or mechanically assembled.
