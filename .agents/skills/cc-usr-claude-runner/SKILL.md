---
name: cc-usr-claude-runner
description: Select approved SQLite batch question numbers and launch separate interactive Claude Code sessions in their working folders through iTerm2 using the root .env connection settings. Not for scoring, trajectory inspection, or Excel export.
---

# CC USR Claude Runner

Launch approved questions without exposing relay credentials or altering prompts.

## Preconditions

- Read `项目规范.md` and [references/operator-guide.md](references/operator-guide.md).
- The selected SQLite question must pass mechanical and semantic question QC, retain the QC prompt fingerprint, and have explicit human approval.
- Root `.env` must contain the relay URL, model name, and key used for this batch.
- Claude Code and iTerm2 must be available.

## Use

The normal user path is to double-click `批量启动Claude.command`, choose a batch, then enter question numbers such as `1,3-5`.

Terminal equivalents:

```bash
python3 .agents/skills/cc-usr-claude-runner/scripts/run_tasks.py \
  --db production.sqlite3 --batch 0911 --env-file .env --list

python3 .agents/skills/cc-usr-claude-runner/scripts/run_tasks.py \
  --db production.sqlite3 --batch 0911 --env-file .env --select 1,3-5

python3 .agents/skills/cc-usr-claude-runner/scripts/run_tasks.py \
  --db production.sqlite3 --batch 0911 --env-file .env --select 1,3-5 --launch
```

Each selected question opens a new iTerm2 window and runs Claude Code with that question folder as its working directory. At launch time, the helper re-reads `.env` and loads the exact prompt from SQLite. The target command must be exactly `[claude_binary, stored_prompt]`: do not add `-p`, model, permission, output-format, system-prompt, agent, or any other Claude CLI option.

Pass URL, model, and key only through Claude's process environment. Do not require the CC Switch app once `.env` is filled. Never print or persist the key or prompt in generated launchers, logs, run metadata, or previews. Preserve Claude Code's native interactive session and original trajectory; never read, copy, transform, summarize, or evaluate trajectory files.
