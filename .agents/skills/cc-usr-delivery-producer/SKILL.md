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
- Use the original Claude JSONL trajectory (not a stream-json terminal transcript or a copied execution summary) to locate turns, assess the model's process, and extract the exact `SessionID`, each user message's `PromptID`, prompts, timestamps, tool calls, and responses. The trajectory artifact used for a record must itself contain the matching user event, its PromptID, and the session identifier.
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
4. Build a turn evidence table before scoring. For every valid turn, record the JSONL file name, event location, exact raw user text, exact user-message `PromptID`, `SessionID`, timestamp, and the next model response/tool span. All turns from one window must share a `SessionID` and trajectory file; every turn must have a distinct `PromptID`. A later turn whose user text is `继续` is stored as exactly `继续`; never replace it with the prior original request. If any field cannot be matched in the original JSONL, stop and report the blocker instead of using a prior turn, a summary, or a terminal stream.
5. Evaluate that turn against the five score dimensions in `项目规范.md`. Compare claims in the response with actual tool activity, repository changes, code, and verification evidence.
6. Write all five descriptions as natural Chinese professional prose. Each statement must be traceable to a named file, function, command, error, test result, requirement, or an exact observable action in the original trajectory; a conclusion without such evidence must not be stored. Make the point of failure, concrete behavior or defect, and practical consequence understandable from the prose itself; add the root cause or a better approach only when the evidence supports it. Do not expose these ingredients as labels or force every field into the same sentence structure. Cover process and product separately when both have issues. Even a score of 5 needs concrete verification evidence.
   - Calibrate `任务规划` from observable planning and state tracking, not from the size of the task. No explicit decomposition and no checkable status updates cannot receive 4 or 5; use the 2 anchor when the agent starts coding blindly and has almost no tracking. A broad plan with material omissions is at most 3. A 4 requires a reasonable breakdown plus tracking for most of the turn; a 5 requires unusually clear decomposition, updates, and ambiguity handling.
   - Calibrate `执行能力` from the complete tool sequence. Count failed commands, errors, repeated identical attempts, unverified claims, and multi-round script/test repair. A turn with several failures and continued debugging, even if it eventually succeeds, is normally 3 rather than 4; 4 allows only a few minor redundancies and no material unresolved failure; 5 requires precise, minimal calls with effective verification. Do not write a description admitting these facts while assigning a higher anchor.
   - Before insertion, perform a score-description contradiction pass: every deduction named in a description must be reflected in that dimension's score, and a score of 4/5 must not coexist with prose saying the defining anchor was absent. If the evidence does not support the proposed score, lower the score rather than softening or omitting the evidence.
7. Create a temporary input JSON matching the record contract and set `human_authored` to `false`. Read `CC_USR_SUBMITTER` from the root `.env` for `submitter`; if it is absent, require the user's real name before insertion. Never put a tool, model, or invented identity in this field. Store the record with `collect_record.py --from-json`, then delete only that temporary input after a successful insert.
8. Store turns in chronological order. First-turn metadata comes from SQLite; later turns use that turn's exact prompt, task type, difficulty, and languages. Never overwrite an existing question/turn record.
9. Before handing off, run a traceability preflight over every staged record: exact prompt equality to the matched JSONL user event, unique `(SessionID, PromptID)`, one trajectory file containing all referenced turns, stable initial snapshot across the session, and a parent chain in turn order. Any mismatch blocks insertion or handoff; do not “repair” it by copying the previous prompt.
10. After every valid turn is stored, hand the batch to `$cc-usr-delivery-qc`. Do not approve records or export Excel in this skill.

## Output quality

Keep scores and descriptions internally consistent. Name exact steps, commands, files, functions, errors, constraints, or missing requirements. Do not use unsupported adjectives or generic claims such as “表现一般”, “代码有 bug”, or “任务没完成”. Do not blame network or environment failures on model capability.

The five exported descriptions contain only task evidence and engineering judgment. Never mention the evaluator, authorship, generation method, automation, trajectory-based generation, or internal audit fields. In particular, do not write self-referential phrases containing `AI`, `Codex`, “自动生成”, or “基于轨迹生成”. Preserve an exact business-domain name only when it is itself necessary evidence, such as a product or API identifier.

Avoid formulaic prose: do not print `When`/`What`/`Impact`, “过程：”/“产物：”, arrows, bracketed placeholders, or stock openings such as “经检查”“根据轨迹”“综合来看”“本次任务中”“总体而言”“综上所述”. Do not mention评分、评测、质检、评价者、模型表现 or generation provenance in exported descriptions. Give each dimension its own evidence, vary sentence length and structure naturally, and do not append the same closing judgment to every field. Read the five descriptions together before insertion; rewrite any pair that sounds interchangeable, translated, or mechanically assembled. `其他问题`非空时遵循同一写作标准。
