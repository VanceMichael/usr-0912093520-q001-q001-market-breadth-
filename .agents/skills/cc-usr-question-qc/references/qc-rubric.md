# Duplicate question QC rubric

A question passes this skill when all of these duplicate checks pass:

- Its prompt is not an exact or near duplicate of any question stored in SQLite, including questions from other batches.
- Normalized overall similarity is below 82%, trigram Jaccard similarity is below 30%, and the longest normalized common substring is shorter than 50 characters for every pair.
- For questions using the same repository, similarity-tag Jaccard overlap is below 75%.
- After business nouns are mentally removed, the prompt does not reuse another question's sentence skeleton or request sequence.
- The state transitions, entities, constraints, and acceptance evidence do not describe substantially the same task in different words.

Task type, 0-1 intent, difficulty, banned topics, repository validity, snapshots, acceptance coverage, and reproducibility are intentionally outside this skill. Authoring checks, mechanical checks, duplicate review, and human approval remain separate gates.
