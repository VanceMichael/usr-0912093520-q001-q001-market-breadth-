# Delivery QC checklist

The human reviewer must personally confirm:

- Task type, difficulty, and languages match this turn.
- Each score matches the rubric in `项目规范.md`.
- Each description naturally makes the problem location, concrete behavior, and practical impact clear without fixed labels or sentence frames.
- Process and product issues are both covered when present.
- File names, functions, errors, missed requirements, and tool calls are factual.
- Scores and descriptions do not contradict each other.
- No obvious issue was omitted.
- Automatically produced records are transparently marked `human_authored=false` in the internal audit field.
- Exported descriptions contain only supported task evidence and engineering judgment, with no evaluator self-reference or generation-process wording.
- The five descriptions are distinct, natural professional observations rather than mechanically repeated templates.

The approval command only records the reviewer's completed decision; it does not rewrite scores or descriptions.
