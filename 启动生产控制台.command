#!/bin/zsh
set -u

PROJECT_DIR="${0:A:h}"
URL="http://127.0.0.1:4173"

cd "$PROJECT_DIR" || exit 1

if curl -fsS "$URL/api/dashboard" >/dev/null 2>&1; then
  open "$URL"
  exit 0
fi

exec python3 "$PROJECT_DIR/webapp/server.py"
