# Operator guide

The selector accepts question numbers, ranges, and task IDs, for example `1,3-5` or `0911-003`.

A question becomes `READY` as soon as mechanical QC and duplicate QC pass with a current prompt fingerprint. No separate human approval is needed. The QC step does not launch Claude Code; launch remains an explicit operator action.

Put the relay URL, model name, and key in root `.env`; `.env.example` is the field reference. The CC Switch app is not required. HTTPS is required except for localhost. URLs containing credentials, query strings, or fragments are rejected.

Preview shows the exact working directory while hiding all three configuration values and the prompt. Launch creates `<批次>/.runs/<时间>/<任务ID>/launch.command`, opens one iTerm2 window per question, and registers the run in SQLite. The generated launcher contains only database/question references and paths, never the URL, model, key, or prompt.

Each helper re-reads `.env`, maps the three values to Claude Code environment variables, changes into the question folder, and invokes exactly `claude <SQLite 原始 prompt>`. It does not use `-p` or add any CLI option. Claude Code therefore remains interactive and writes its native original trajectory. The runner never reads that trajectory.

One selected question starts one new session. Keep each conversation to at most ten counted turns. Network failures do not count; a human `继续` after thinking-limit exhaustion does count. The human records SessionID and PromptID.

After each valid turn, the human copies the session's `SessionID` and that user message's `PromptID`, reviews and writes the five scores and descriptions, then uses `$cc-usr-delivery-producer` to store the turn. The runner must not inspect Claude Code trajectories or attempt to collect those identifiers itself.
