#!/usr/bin/env bash
# PU6: local workers smoke (echo + doc-convert on personal orchestrator)
set -euo pipefail
ORCH="$HOME/personal-orchestrator"; source "$ORCH/env"
echo "test md" > "$HOME/agents-inbox/pu-test.md"
TID=$(curl -s -X POST http://127.0.0.1:8501/api/tasks -H "X-Orch-Key: $ORCH_API_KEY" -H 'Content-Type: application/json' \
  -d '{"template":"doc-convert","payload":{"file":"pu-test.md"},"requester":"pu","group":"development"}' | jq -r .id)
for i in $(seq 1 30); do ST=$(curl -s http://127.0.0.1:8501/api/tasks/$TID | jq -r .task.status); [[ "$ST" == done || "$ST" == failed ]] && break; sleep 1; done
echo "task #$TID: $ST"; ls "$HOME/agents-artifacts/task-$TID/" 2>/dev/null
echo "PU6 done. Next: bash pu7-bridge.sh"
