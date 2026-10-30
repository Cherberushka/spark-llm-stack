#!/usr/bin/env bash
# PU3: Pi + skills in WSL, pointed at the central gateway
set -euo pipefail
source "$HOME/.spark-env"
command -v pi >/dev/null 2>&1 || { curl -fsSL https://pi.dev/install.sh | sh; }
export PATH="$HOME/.pi/agent/bin:$PATH"
mkdir -p "$HOME/.pi/agent"
sed "s|<SPARK_IP>|$SPARK_IP|" "$(dirname "$0")/../pi/models.json" > "$HOME/.pi/agent/models.json"
chmod 600 "$HOME/.pi/agent/models.json"
# skills (curated): возьмите скрипт со Spark или из пакета:
if [[ -f "$(dirname "$0")/../../scripts/10-install-pi-skills.sh" ]]; then
  bash "$(dirname "$0")/../../scripts/10-install-pi-skills.sh"
else
  scp "user@$SPARK_IP:spark-llm-stack/scripts/10-install-pi-skills.sh" /tmp/ && bash /tmp/10-install-pi-skills.sh
fi
pi --list-models spark | grep -q qwen3.8 && echo "✓ Pi видит spark/qwen3.8-27b" || { echo "✗ проверьте ~/.spark-env и models.json"; exit 1; }
echo "PU3 done. Next: bash pu4-mcp-local.sh"
