---
name: cc-usr-delivery-producer
description: After Claude Code runs, locate its sessions, extract SessionID and PromptID, inspect each turn and resulting code, assign five evidence-based scores, write concrete descriptions, and store delivery records in production SQLite. Stop after production; never run delivery QC, change target-model output, or author questions.
---

# CC USR Delivery Producer

Produce one scored SQLite delivery record for every valid Claude Code user-prompt/model-response turn.

## Stage boundary

This skill performs delivery production only. It may inspect evidence, score turns, write descriptions, and insert production records, but it must not invoke `$cc-usr-delivery-qc`, run its validator or finalizer, write `质检通过`, or change any delivery-QC or final-review field. After production finishes, report `生产完成，等待独立交付质检` and stop. An unattended scheduler may start a separate QC task afterward; that does not authorize this production task to cross the boundary itself.

## Required context

Read `项目规范.md`, [references/scoring-rubric.md](references/scoring-rubric.md), [references/record-contract.md](references/record-contract.md), and [references/natural-writing.md](references/natural-writing.md). Work only on launched questions in the requested batch. This skill is the immediate next step after the target-model run finishes.

## Evidence boundary

- Read the SQLite question and registered run, the matching Claude Code trajectory in that run's isolated Claude home, and the question workspace produced by that run. A non-empty `runs.trajectory_root` is authoritative; do not silently fall back to the operator's global `~/.claude/projects`.
- Use the original Claude JSONL trajectory (not a stream-json terminal transcript or a copied execution summary) to locate turns, assess the model's process, and extract the exact `SessionID`, each user message's `PromptID`, prompts, timestamps, tool calls, and responses. The trajectory artifact used for a record must itself contain the matching user event, its PromptID, and the session identifier.
- Inspect the initial snapshot, current Git diff and code, and relevant verification results to assess the product. Run reasonable read-only or test commands when needed, but never repair, rewrite, or improve the target-model output.
- A claim that requested behavior works needs verification performed inside the target turn and preserved in the original JSONL, such as an actual test command with its result, a real service interaction, or another observable runtime check. Static code inspection, file existence, or the final reply may prove a defect, but none of them alone proves successful behavior. When frontend delivery is actually in scope, require browser-level interaction evidence; backend-only work does not require a browser test.
- A failed test or runtime check remains valid delivery evidence. Store the record, describe the exact failure and consequence, and lower the affected dimensions unless the trajectory identity or turn boundary cannot be established.
- Do not infer evidence that is absent. If the trajectory match, turn boundary, identifier, or product state is ambiguous, stop and report the blocker instead of guessing.

## Workflow

1. Query SQLite for the selected question, initial prompt, workspace, snapshot, and latest registered Claude Code run.
2. Resolve the authoritative trajectory root first, then locate the matching session with:

```bash
python3 .agents/skills/cc-usr-delivery-producer/scripts/find_claude_turns.py \
  --db production.sqlite3 --batch <批次> --question <题号> \
  --claude-root <本次运行的 trajectory_root>
```

3. Confirm the locator matched the exact workspace and first-turn prompt. Read the selected trajectory and split it into valid turns. Exclude pure network/model-service failures, meta events, compact summaries and Claude-generated `<task-notification>` events; count a human `继续` after thinking-limit exhaustion.
4. Build a turn evidence table before scoring. For every valid turn, record the JSONL file name, event location, exact raw user text, exact user-message `PromptID`, `SessionID`, timestamp, current-dialogue order, and the next model response/tool span. Set `turn_no` to the one-based chronological position within that `SessionID`; it is exported as `当前对话轮次排序`, starts at 1, and must remain consecutive. All turns from one window must share a `SessionID` and trajectory file. Normal turns export their own prompt and `PromptID`. A later real user event whose text is exactly `继续` is a continuation record: export the nearest preceding non-continuation task's complete prompt and `PromptID`, while preserving the actual event as `raw_user_prompt=继续`, `raw_turn_id=<继续事件 PromptID>`, `is_continuation=true`, and the consecutive continuation count. If any field cannot be matched in the original JSONL, stop and report the blocker instead of using a summary or terminal stream.
5. Build a five-dimension evidence ledger and requirement coverage table before scoring. Bind every description sentence to a source ID. Trajectory evidence records the exact JSONL line, excerpt and line hash; workspace evidence records a relative path, excerpt and file hash; prompt evidence records the exact excerpt and prompt hash. Every trajectory line must fall inside the current turn, from its real user event through the event before the next real user message. Evidence produced by a later turn, QC, or another machine cannot be attributed to the target turn.
6. Evaluate that turn against the five score dimensions in `项目规范.md`. Compare claims in the response with actual tool activity, repository changes, code, and verification evidence. Planning evidence concerns decomposition and state tracking; reasoning evidence concerns assumptions, inference and diagnosis; execution evidence concerns tool sequence, failures, recovery and final verification.
7. Write all five descriptions as natural Chinese professional prose. Each description is one paragraph, contains at least 45 Chinese characters, and is no more than 420 total characters. Each statement must be traceable to a named file, function, command, error, test result, requirement, or an exact observable action in the original trajectory; a conclusion without such evidence must not be stored. Make the point of failure, concrete behavior or defect, and practical consequence understandable from the prose itself; add the root cause or a better approach only when the evidence supports it. Do not expose these ingredients as labels or force every field into the same sentence structure. Cover process and product separately when both have issues. A score of 5 must name a concrete verification action and its objective result. A score below 5 must name the evidence location, observed behavior and engineering consequence.
   - Treat writing style as a hard acceptance gate, not a polish step. Do not use a shared “事实罗列 + 转折结论” template across dimensions or turns. Start from the concrete command, file, behavior, test, or missing requirement that matters for that dimension. Avoid opening several fields with `本轮`/`这轮`/`这一轮`, and do not repeat the same sentence rhythm or closing judgment.
   - Do not write evaluator or audit language into the exported description. The following phrases are forbidden unless they are part of an exact quoted product message: `最终工作区包含`, `逐条核对后没有发现`, `核心取舍是清楚的`, `本轮主要是`, `本轮按`, `顺序基本合理`, `这些修正体现了`, `无法证明`, `根据轨迹`, `综合来看`, `经检查`, `唯一问题`. Avoid dense architecture-name or test-number inventories; include a number only when it changes the engineering judgment.
   - Before calling `collect_record.py`, place the pending record payloads in temporary input JSON files without inserting them, read the five descriptions together, and run the mandatory style gate:

```bash
python3 .agents/skills/cc-usr-delivery-producer/scripts/check_description_style.py \
  --input <all-pending-record-json-files>
```

The command must exit successfully. Any style error, repeated opener, prohibited phrase, visible `过程/产物/When/What/Impact` label, or mechanically similar pair blocks insertion. Rewrite the descriptions and rerun the check; do not bypass it by weakening the checker or by storing the record first.
   - Calibrate `任务规划` from observable planning and state tracking, not from the size of the task. No explicit decomposition and no checkable status updates cannot receive 4 or 5; use the 2 anchor when the agent starts coding blindly and has almost no tracking. A broad plan with material omissions is at most 3. A 4 requires a reasonable breakdown plus tracking for most of the turn; a 5 requires unusually clear decomposition, updates, and ambiguity handling.
   - Calibrate `执行能力` from the complete tool sequence. Count failed commands, errors, repeated identical attempts, unverified claims, and multi-round script/test repair. A turn with several failures and continued debugging, even if it eventually succeeds, is normally 3 rather than 4; 4 allows only a few minor redundancies and no material unresolved failure; 5 requires precise, minimal calls with effective verification. Do not write a description admitting these facts while assigning a higher anchor.
   - Before insertion, perform a score-description contradiction pass: every deduction named in a description must be reflected in that dimension's score, and a score of 4/5 must not coexist with prose saying the defining anchor was absent. If the evidence does not support the proposed score, lower the score rather than softening or omitting the evidence.
8. After the style gate passes, use those temporary input JSON files matching the record contract and set `human_authored` to `false`. Read `CC_USR_SUBMITTER` from the root `.env` for `submitter`; if it is absent, require the user's real name before insertion. Never put a tool, model, or invented identity in this field. Store the record with `collect_record.py --from-json`, then delete only that temporary input after a successful insert.
9. Store turns in chronological order. Pass the same one-based `turn_no` to `collect_record.py --turn`; do not derive it from record count, question number, tool-call count, or a previous export. First-turn metadata comes from SQLite; ordinary later turns use their exact prompt and classification. A verified `继续` turn reuses the interrupted prompt, PromptID and classification while preserving its own raw prompt and raw PromptID. Never overwrite an existing question/turn record.
10. Before handing off, run a traceability preflight over every staged record: normal records match their own raw event exactly; continuation records match their raw `继续` event and reuse the nearest interrupted task identity; one trajectory file contains every referenced turn; the initial snapshot stays stable; and the parent chain follows turn order. Repeated exported `(SessionID, PromptID)` is permitted only for verified continuation records with distinct raw PromptIDs. Any other mismatch blocks insertion or handoff.
11. After every valid turn is stored and the production checks pass, stop. Report the produced record count and any blocker, leaving delivery QC, final human/Codex review, Excel export, and SOLO2 submission to their separate stages.

## Output quality

Keep scores and descriptions internally consistent. Name exact steps, commands, files, functions, errors, constraints, or missing requirements. Do not use unsupported adjectives or generic claims such as “表现一般”, “代码有 bug”, or “任务没完成”. Do not blame network or environment failures on model capability.

The five exported descriptions contain only task evidence and engineering judgment. Never mention the evaluator, authorship, generation method, automation, trajectory-based generation, or internal audit fields. In particular, do not write self-referential phrases containing `AI`, `Codex`, “自动生成”, or “基于轨迹生成”. Preserve an exact business-domain name only when it is itself necessary evidence, such as a product or API identifier.

Avoid formulaic prose: do not print `When`/`What`/`Impact`, “过程：”/“产物：”, arrows, bracketed placeholders, or stock openings such as “经检查”“根据轨迹”“综合来看”“本次任务中”“总体而言”“综上所述”. Do not mention评分、评测、质检、评价者、模型表现 or generation provenance, and replace non-technical evaluator words such as `rationale`, `overall`, `generally`, and `basically` with natural Chinese; preserve necessary file names, commands, frameworks, protocols, classes, and code identifiers. Give each dimension its own evidence and review the five descriptions together before insertion. `其他问题` must be empty unless it adds a concrete issue outside the five dimensions, and must not repeat a dimension description. The checker in `scripts/check_description_style.py` is the minimum gate; passing it does not excuse a semantic review for awkward or synthetic wording.
