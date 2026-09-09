---
name: cc-usr-claude-runner
description: Select QC-passed SQLite batch question numbers and launch unattended Claude Code sessions in their working folders using the root .env connection settings. Auto mode always uses headless/server execution; iTerm2 is opt-in for interactive debugging. Not for scoring, trajectory inspection, or Excel export.
---

# CC USR Claude Runner

Launch QC-passed questions without exposing relay credentials or altering prompts.

## Preconditions

- Read `项目规范.md` and [references/operator-guide.md](references/operator-guide.md).
- The selected SQLite question must pass mechanical and duplicate question QC and retain the QC prompt fingerprint. A duplicate-QC pass is sufficient for `READY`; no separate human approval is required.
- Root `.env` must contain the relay URL, model name, and key used for this batch.
- Claude Code must be available. Auto mode is unattended on every platform; iTerm2 is only needed when an operator explicitly requests a local interactive launch with `--mode iterm`.

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

By default, `--mode auto` uses detached headless/server execution with no terminal or stdin, including on macOS when iTerm2 is installed. Headless mode uses Claude Code print mode plus `--permission-prompts none`, so workspace-trust and permission prompts cannot block the run. `--mode server` is the same unattended path; `--mode iterm` is an explicit interactive override and may show Claude Code's trust prompt. At launch time, the helper re-reads `.env` and loads the exact prompt from SQLite. Interactive launches use `[claude_binary, "--dangerously-skip-permissions", stored_prompt]`; headless launches add `--print`, an explicit bypass permission mode, and disabled permission prompts. Use it only in the isolated question workspace.

Pass URL, model, and key only through Claude's process environment. Do not require the CC Switch app once `.env` is filled. Never print or persist the key or prompt in generated launchers, logs, run metadata, or previews. Preserve Claude Code's native interactive session and original trajectory; never read, copy, transform, summarize, or evaluate trajectory files.
