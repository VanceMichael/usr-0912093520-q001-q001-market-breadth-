#!/bin/zsh
set -u

PROJECT_DIR="${0:A:h}"
RUNNER="$PROJECT_DIR/.agents/skills/cc-usr-claude-runner/scripts/run_tasks.py"
ENV_FILE="$PROJECT_DIR/.env"
DATABASE="$PROJECT_DIR/production.sqlite3"

cd "$PROJECT_DIR" || exit 1

echo "Claude Code 用户满意度批量运行"
echo ""
python3 "$PROJECT_DIR/tools/batch_pipeline.py" --db "$DATABASE" list
echo ""
read "BATCH?请输入批次目录名（例如 0911）："
if [[ -z "$BATCH" ]]; then
  echo "未选择批次。"
  read "_?按回车关闭窗口。"
  exit 0
fi

echo ""
if ! python3 "$RUNNER" --db "$DATABASE" --batch "$BATCH" --env-file "$ENV_FILE" --list; then
  echo ""
  echo "当前批次不存在或没有可列出的题目。"
  read "_?按回车关闭窗口。"
  exit 1
fi

echo ""
read "SELECTION?请输入题号、区间或任务 ID（例如 1,3-5）："
if [[ -z "$SELECTION" ]]; then
  echo "未选择题目。"
  read "_?按回车关闭窗口。"
  exit 0
fi

echo ""
if ! python3 "$RUNNER" --db "$DATABASE" --batch "$BATCH" --env-file "$ENV_FILE" --select "$SELECTION"; then
  echo ""
  read "_?预检失败，按回车关闭窗口。"
  exit 1
fi

echo ""
read "CONFIRM?确认启动以上题目请输入 RUN："
if [[ "$CONFIRM" != "RUN" ]]; then
  echo "已取消，不会启动 Claude Code。"
  read "_?按回车关闭窗口。"
  exit 0
fi

python3 "$RUNNER" \
  --db "$DATABASE" \
  --batch "$BATCH" \
  --env-file "$ENV_FILE" \
  --select "$SELECTION" \
  --launch \
  --mode auto

echo ""
read "_?批次启动完成，按回车关闭本窗口。"
