#!/bin/bash
set -euo pipefail

# Ensure jq is installed
if ! command -v jq >/dev/null 2>&1; then
  echo "Error: 'jq' is not installed. Please install it first (e.g., 'sudo apt install jq' or 'brew install jq')."
  exit 1
fi

SETTINGS_FILE="$HOME/.gemini/settings.json"

# Install the gdb-mcp tool
uv tool install git+https://github.com/schuay/gdb-mcp.git

mkdir -p "$(dirname "$SETTINGS_FILE")"
[ -f "$SETTINGS_FILE" ] || echo '{}' > "$SETTINGS_FILE"
cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak"

trap 'rm -f "$SETTINGS_FILE.tmp"' EXIT
jq '.mcpServers["gdb-mcp"] = {"command": "gdb-mcp"}' \
  "$SETTINGS_FILE" > "$SETTINGS_FILE.tmp" && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"

echo "Successfully updated $SETTINGS_FILE"
