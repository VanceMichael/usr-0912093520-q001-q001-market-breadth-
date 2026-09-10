# Duplicate question QC rubric

A question passes this skill when all of these duplicate checks pass:

- Its prompt is not an exact or near duplicate of any question stored in SQLite, including questions from other batches.
- Normalized overall similarity is below 82%, trigram Jaccard similarity is below 30%, and the longest normalized common substring is shorter than 50 characters for every pair.
- For questions using the same repository, similarity-tag Jaccard overlap is below 75%.
- After business nouns are mentally removed, the prompt does not reuse another question's sentence skeleton or request sequence.
- The state transitions, entities, constraints, and acceptance evidence do not describe substantially the same task in different words.
- The prompt is one natural Chinese paragraph with a concrete professional context and observable business result; it does not use a canned opening, headings, checklist labels, a reusable requirement tail, or internal evaluation language.
- Its sentence order and cadence are materially different from other prompts in the batch, rather than merely replacing nouns, technologies, and state names.
- Its final sentence and final two clauses are not copied from another prompt or lightly paraphrased from a shared tail. Generic endings about startup methods, Docker, tests, validation, or documentation are blocking when reused.
- It does not end with an implementation-summary recipe such as “storage keeps A/B/C + validate scenario A/B/C/D”. A checklist of three or more scenarios before “场景验证/用例验证”, or generic environment commentary, fails even when it appears only once.
- For a batch with five or more questions, at least three opening perspectives and three requirement/acceptance orders must be visibly different. Fewer than three is a batch-level failure.
- After removing business nouns, technologies, and field names, a repeated four-stage request sequence is a blocking template finding. Three prompts sharing the “搭建/实现服务 + 并用测试覆盖” spine also fail, even when numeric similarity is below threshold.
- A finding must trigger prompt repair through the controlled `prompt-update` command, followed by duplicate-check and mechanical QC. Recording `revise` without changing the prompt is not a completed QC outcome.
- A `repeated_terminal_sentence` result is a hard failure even when whole-prompt similarity is below every numeric threshold.

Task type, 0-1 intent, difficulty, banned topics, repository validity, snapshots, acceptance coverage, and reproducibility are intentionally outside this skill. Authoring and mechanical checks remain separate gates. Passing duplicate QC makes the question ready to launch when mechanical QC and the stored prompt fingerprint are current.

When all duplicate checks pass, store the report text exactly as `质检通过`. Only failed reviews carry detailed duplicate evidence.
