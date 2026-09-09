#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")
PYTHON_BIN=$(command -v python3)
USER_HOME_DIR=${HOME:?HOME is required}
AGENT_DIR="$USER_HOME_DIR/Library/LaunchAgents"

mkdir -p "$AGENT_DIR" "$PROJECT_ROOT/runs/daemon"

for service in pipeline console; do
  template="$PROJECT_ROOT/deploy/launchd/com.ccusr.$service.plist.template"
  destination="$AGENT_DIR/com.ccusr.$service.plist"
  sed \
    -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    -e "s|__PYTHON__|$PYTHON_BIN|g" \
    -e "s|__USER_HOME__|$USER_HOME_DIR|g" \
    "$template" > "$destination"
  launchctl bootout "gui/$UID" "$destination" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$destination"
done

echo "CC USR console and scheduler are installed."
echo "Console: http://127.0.0.1:4173/"
