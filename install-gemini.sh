#!/bin/bash
set -euo pipefail

# Install the gdb-mcp tool
uv tool install git+https://github.com/schuay/gdb-mcp.git

# Update ~/.gemini/settings.json with the new MCP server
if command -v jq >/dev/null 2>&1; then
  # Create settings file with an empty object if it doesn't exist yet
  if [ ! -f ~/.gemini/settings.json ]; then
    mkdir -p ~/.gemini
    echo '{}' > ~/.gemini/settings.json
  fi

  cp ~/.gemini/settings.json ~/.gemini/settings.json.bak
  trap 'rm -f ~/.gemini/settings.json.tmp' EXIT
  jq '.mcpServers["gdb-mcp"] = {"command": "gdb-mcp"}' ~/.gemini/settings.json > \
    ~/.gemini/settings.json.tmp && mv ~/.gemini/settings.json.tmp ~/.gemini/settings.json
  echo "Successfully updated ~/.gemini/settings.json"
else
  echo "jq not found. Please manually add the following to the 'mcpServers' section in ~/.gemini/settings.json:"
  echo '    "gdb-mcp": {'
  echo '      "command": "gdb-mcp"'
  echo '    }'
fi
