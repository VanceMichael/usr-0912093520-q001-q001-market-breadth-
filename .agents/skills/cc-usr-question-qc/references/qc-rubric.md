# Question QC rubric

A production-ready question must pass all of these checks:

- The stored prompt is the exact first-turn text intended for Claude Code.
- The first turn is not `简单`, and fewer than two over-simple traits apply.
- The task requires meaningful use of the prepared repository rather than an isolated one-file answer.
- The primary intent matches the declared task type and the languages match the actual work.
- Acceptance criteria are observable without exposing the repair or a gold answer.
- The topic is not prohibited or saturated under `项目规范.md`.
- No existing question combines a nearly identical environment, request, sentence skeleton, and similarity tags.
- The question workspace exists, is a Git repository, and contains no committed credentials.
- The initial snapshot is an evaluation-accessible `https://github.com/<org>/<repo>/commit/<40 SHA>` permalink.
- The reproducibility class matches the actual dependency setup.

Mechanical checks, semantic review, and human approval are separate gates. None substitutes for another.
