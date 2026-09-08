# Operator guide

The selector accepts question numbers, ranges, and task IDs, for example `1,3-5` or `0911-003`.

A question becomes `READY` as soon as mechanical QC and duplicate QC pass with a current prompt fingerprint. No separate human approval is needed. The QC step does not launch Claude Code; launch remains an explicit operator action.

Put the relay URL, model name, and key in root `.env`; `.env.example` is the field reference. The CC Switch app is not required. HTTPS is required except for localhost. URLs containing credentials, query strings, or fragments are rejected.

Preview shows the exact working directory while hiding all three configuration values and the prompt. Launch creates `<批次>/.runs/<时间>/<任务ID>/launch.command`, automatically opens one iTerm2 window per question on macOS when iTerm2 is available, and otherwise starts detached headless processes. `--mode iterm` and `--mode server` explicitly override detection. The generated launcher contains only database/question references and paths, never the URL, model, key, or prompt.

Each helper re-reads `.env`, maps the three values to Claude Code environment variables, changes into the question folder, and invokes `claude --dangerously-skip-permissions <SQLite 原始 prompt>` interactively, or `claude --print --dangerously-skip-permissions --permission-mode bypassPermissions --permission-prompts none <SQLite 原始 prompt>` headlessly. The runner never prints the key or reads the trajectory.

One selected question starts one new session. Keep each conversation to at most ten counted turns. Network failures do not count; a human `继续` after thinking-limit exhaustion does count.

After the run finishes, use `$cc-usr-delivery-producer` as the next step. It locates the matching trajectory, extracts `SessionID` and each user message's `PromptID`, assesses every valid turn and resulting product, and stores the scored records. The runner itself does not inspect trajectories.
