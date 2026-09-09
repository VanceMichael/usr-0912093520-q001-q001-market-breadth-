---
name: cc-usr-claude-runner
description: Select QC-passed SQLite batch question numbers and launch Claude Code sessions in their working folders using the root .env connection settings. On macOS with iTerm2, auto mode opens trusted native interactive sessions without workspace-trust or tool-permission prompts. Not for scoring, trajectory inspection, or Excel export.
---

# CC USR Claude Runner

Launch QC-passed questions without exposing relay credentials or altering prompts.

## Preconditions

- Read `项目规范.md` and [references/operator-guide.md](references/operator-guide.md).
- The selected SQLite question must pass mechanical and duplicate question QC and retain the QC prompt fingerprint. A duplicate-QC pass is sufficient for `READY`; no separate human approval is required.
- Root `.env` must contain the relay URL, model name, and key used for this batch.
- Claude Code must be available. On macOS, auto mode uses a visible native iTerm2 session when available; before opening any window, it pre-accepts workspace trust for every selected exact question directory. Other environments use an unattended process. Explicit `--mode iterm` uses the same trusted native session behavior.

## Use

The normal user path is to double-click `批量启动Claude.command` on macOS or `批量启动Claude.bat` on Windows, choose a batch, then enter question numbers such as `1,3-5`.

Terminal equivalents:

```bash
python3 .agents/skills/cc-usr-claude-runner/scripts/run_tasks.py \
  --db production.sqlite3 --batch 0911 --env-file .env --list

python3 .agents/skills/cc-usr-claude-runner/scripts/run_tasks.py \
  --db production.sqlite3 --batch 0911 --env-file .env --select 1,3-5

python3 .agents/skills/cc-usr-claude-runner/scripts/run_tasks.py \
  --db production.sqlite3 --batch 0911 --env-file .env --select 1,3-5 --launch --mode auto
```

By default, `--mode auto` on macOS with iTerm2 opens Claude Code's native interactive interface. The runner first updates each selected absolute question path in `~/.claude.json` with `hasTrustDialogAccepted: true`, preserving all existing configuration fields and file permissions through an atomic replacement. Claude then starts with `--dangerously-skip-permissions`, so operators see the complete native status, tool activity, and input box without answering workspace-trust or tool-permission prompts. `--mode server` remains the unattended print-mode path; on systems without iTerm2, auto mode falls back to it. At launch time, the helper re-reads `.env` and loads the exact prompt from SQLite. Use it only in the isolated question workspace.

Claude Code records workspace trust against exact project directories; trusting a batch parent directory does not reliably trust newly created question subdirectories. Always pre-trust every selected `qNNN` path before opening any interactive window. Do not rely on parent-folder trust and do not ask the operator to confirm Yes/No manually.

Pass URL, model, and key only through Claude's process environment. Do not require the CC Switch app once `.env` is filled. Never print or persist the key or prompt in generated launchers, logs, run metadata, or previews. Preserve Claude Code's native interactive session and original trajectory; never read, copy, transform, summarize, or evaluate trajectory files.
