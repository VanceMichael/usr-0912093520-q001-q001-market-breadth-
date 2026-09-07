# Question QC rubric

A production-ready question must pass all of these checks:

- The stored prompt is the exact first-turn text intended for Claude Code.
- The declared first-turn task type is exactly `0-1 代码生成`.
- The prompt's first action and overall intent ask for a complete project or a complete module that is absent from the baseline; it is not an extension, repair, explanation, refactor, engineering-only change, or test-only task with a relabeled type.
- The prompt is one coherent natural-language paragraph in a realistic user voice, without headings, checklist formatting, canned openings, or a reusable engineering tail.
- The first turn is not `简单`, and fewer than two over-simple traits apply.
- The task requires meaningful use of the prepared repository or scaffold rather than an isolated one-file answer, and the baseline does not pre-implement the requested complete project or module.
- The business request spans at least four distinct responsibility areas and naturally requires a multi-file result.
- The languages match the actual work recorded in metadata.
- Acceptance criteria are observable without exposing the repair or a gold answer.
- The topic is not prohibited or saturated under `项目规范.md`.
- No existing question combines a nearly identical environment, request, sentence skeleton, and similarity tags.
- The question workspace exists, is a Git repository, and contains no committed credentials.
- The initial snapshot is an evaluation-accessible `https://github.com/<org>/<repo>/commit/<40 SHA>` permalink.
- The reproducibility class matches the actual dependency setup.

Mechanical checks, semantic review, and human approval are separate gates. None substitutes for another.
