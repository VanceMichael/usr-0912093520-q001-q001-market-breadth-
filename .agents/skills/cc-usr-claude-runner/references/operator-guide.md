# Operator guide

The selector accepts question numbers, ranges, and task IDs, for example `1,3-5` or `0911-003`.

A question becomes `READY` as soon as mechanical QC and duplicate QC pass with a current prompt fingerprint. No separate human approval is needed. The QC step does not launch Claude Code; launch remains an explicit operator action.

Put the relay URL, model name, and key in root `.env`; `.env.example` is the field reference. The CC Switch app is not required. HTTPS is required except for localhost. URLs containing credentials, query strings, or fragments are rejected.

Preview shows the exact working directory while hiding all three configuration values and the prompt. Launch creates `<批次>/.runs/<时间>/<任务ID>/launch.command` for detached server runs or visible macOS iTerm2 runs, and `launch.ps1` for Windows PowerShell. On macOS with iTerm2, `--mode auto` opens Claude's native interactive interface after pre-trusting every selected exact question path; `--mode server` is detached and unattended. Explicit `--mode iterm` uses the same pre-trusted native interface. Both paths register the run in SQLite. Generated launchers contain only database/question references and paths, never the URL, model, key, or prompt.

Before any interactive window opens, the runner updates the selected exact paths in `~/.claude.json` using structured JSON and atomic replacement, preserving existing settings and permissions. Each helper then re-reads `.env`, maps the three values to Claude Code environment variables, changes into the question folder, and invokes `claude --dangerously-skip-permissions <SQLite 原始 prompt>` interactively. Server mode invokes `claude --print --verbose --output-format stream-json --dangerously-skip-permissions --permission-mode bypassPermissions --permission-prompts none <SQLite 原始 prompt>` headlessly. The runner never prints the key or reads the trajectory.

Do not assume a trusted parent directory covers future question workspaces. Claude Code persists trust for exact project paths, so pre-trust every selected `qNNN` path before launching the first iTerm2 window. Tool approvals remain bypassed by the CLI flag; the native interface remains visible and accepts follow-up input.

One selected question starts one new session. Keep each conversation to at most ten counted turns. Network failures do not count; a human `继续` after thinking-limit exhaustion does count.

After the run finishes, use `$cc-usr-delivery-producer` as the next step. It locates the matching trajectory, extracts `SessionID` and each user message's `PromptID`, assesses every valid turn and resulting product, and stores the scored records. The runner itself does not inspect trajectories.
