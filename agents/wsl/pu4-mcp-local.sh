#!/usr/bin/env bash
# PU4: local MCP servers for Pi in WSL (your repos & docs only)
set -euo pipefail
mkdir -p "$HOME/projects" "$HOME/.pi/agent"
cat > "$HOME/.pi/agent/mcp.json" <<E
{
  "mcpServers": {
    "wsl-repos": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "$HOME/projects"]
    },
    "win-docs-ro": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/mnt/c/Users/Public/ClinicDocs"]
    }
  }
}
E
echo "✓ mcp.json: $HOME/projects (rw) + /mnt/c/... (read-only по смыслу; замените путь на свой)"
echo "  Прим.: /mnt/c медленный — для репо используйте FS WSL; документы читать с /mnt/c можно."
echo "PU4 done. Next: bash pu5-orchestrator-local.sh"
