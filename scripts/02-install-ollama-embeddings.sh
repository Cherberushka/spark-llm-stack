#!/usr/bin/env bash
# =============================================================================
# 02-install-ollama-embeddings.sh — Ollama (native, systemd) + bge-m3 embeddings
# Run as root: sudo bash scripts/02-install-ollama-embeddings.sh
#
# Role in the stack: Ollama serves the RAG embedding model only —
#   bge-m3 (BAAI, XLM-RoBERTa, 100+ languages incl. Russian, ~1.2 GB).
# The chat model runs on its own engine: vLLM :8000 (Qwen3.8-27B NVFP4).
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

EMBED_TAG="${EMBED_TAG:-bge-m3:latest}"

echo "=== [1/4] Install/update Ollama (official script, arm64 + CUDA) ==="
curl -fsSL https://ollama.com/install.sh | sh

echo "=== [2/4] Configure the ollama systemd service ==="
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<'EOF'
[Service]
# Bind on all interfaces so Docker containers (LiteLLM/Open WebUI/Caddy via
# host-gateway) and LAN clients can reach it. ufw restricts 11434 to the LAN.
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_ORIGINS=*"
# Embeddings model is tiny; keep it resident for instant RAG queries:
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF
systemctl daemon-reload
systemctl enable ollama
systemctl restart ollama

echo "waiting for the Ollama API on 127.0.0.1:11434 ..."
for i in $(seq 1 60); do
  curl -s -o /dev/null http://127.0.0.1:11434/api/version && break
  sleep 1
done
echo "Ollama version: $(curl -s http://127.0.0.1:11434/api/version)"

echo "=== [3/4] Pull ${EMBED_TAG} (~1.2 GB) ==="
ollama pull "$EMBED_TAG"

echo "=== [4/4] Embeddings smoke test ==="
RESP="$(curl -s http://127.0.0.1:11434/api/embed -d '{"model":"'"$EMBED_TAG"'","input":"Проверка эмбеддингов: приёмный покой, клинические рекомендации"}')"
DIM="$(echo "$RESP" | jq -r '.embeddings[0] | length' 2>/dev/null || echo 0)"
if (( DIM > 100 )); then
  echo "✓ bge-m3 embeddings work (vector dim: $DIM)"
else
  echo "✗ embeddings failed — journalctl -u ollama -n 50"; exit 1
fi

echo
echo "Next: sudo bash scripts/03-deploy-qwen38-vllm.sh"
