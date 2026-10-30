#!/usr/bin/env bash
# =============================================================================
# 03b-deploy-qwen38-llamacpp.sh — Qwen3.8-27B via llama.cpp (NO NGC required)
# Run as root: sudo bash scripts/03b-deploy-qwen38-llamacpp.sh
#
# WHY THIS SCRIPT EXISTS
#   The NVIDIA vLLM recipe (scripts/03) requires the NGC image
#   nvcr.io/nvidia/vllm:26.10.pre1-py3, which carries NVIDIA-only patches
#   (vllm#54614 nvfp4_dynamic, #59021, #59025, #59244, #60252). As of
#   2026-10-22 those PRs are OPEN (not merged upstream), so Docker Hub images
#   vllm/vllm-openai:* REJECT the recipe's --kernel-config and can corrupt
#   output with DFlash+prefix-caching (#60174 unfixed). NGC is unreachable
#   from Russia. This script deploys the same model via NVIDIA's official
#   llama.cpp recipe instead — all artifacts from GitHub + HF (mirror OK):
#     models/qwen/qwen3.8-27b/geforce-rtx/windows/llama-cpp/30gb/q4-k-m/vision-dflash2
#     (llama.cpp b11574, measured 159.6 tok/s on GeForce; ~25-30 tok/s on Spark)
#
# CONTRACT (identical to the vLLM path, so the rest of the stack is unchanged):
#   OpenAI-compatible API on :8000, Bearer key = VLLM_API_KEY (/etc/spark-llm.env),
#   model name in requests: anything (llama-server serves the loaded model);
#   LiteLLM/Open WebUI/Pi keep using "qwen3.8-27b".
#
# KNOBS:
#   QWEN_GGUF=Qwen3.8-27B-UD-Q4_K_XL.gguf   (default; Q5_K_M — see runbook)
#   QWEN_CTX=163840        (recipe value; hybrid GDN attention → cheap KV)
#   REASONING_BUDGET=0     (0 = thinking OFF, agent mode; -1/"" = model default)
#   HF_ENDPOINT=https://hf-mirror.com  (default here; set to huggingface.co if reachable)
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

BUILD_DIR=/opt/llama.cpp
MODEL_DIR=/opt/models/qwen3.8-27b
HF=/opt/hf-venv/bin/hf
ENV_FILE=/etc/spark-llm.env
CUDA_ARCH="${CUDA_ARCH:-121}"                       # GB10 = sm_121
QWEN_GGUF="${QWEN_GGUF:-Qwen3.8-27B-UD-Q4_K_XL.gguf}"
DRAFT_GGUF="${DRAFT_GGUF:-Qwen3.8-27B-DFlash2-Q4_K_M.gguf}"
MMPROJ="${MMPROJ:-mmproj-F16.gguf}"
QWEN_CTX="${QWEN_CTX:-262144}"   # validated on Spark: 2 slots x 131072 with DFlash2 alive (~50 tok/s)
REASONING_BUDGET="${REASONING_BUDGET-0}"
PORT="${QWEN_PORT:-8000}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

echo "HF_ENDPOINT=$HF_ENDPOINT  quant=$QWEN_GGUF  ctx=$QWEN_CTX  reasoning_budget='${REASONING_BUDGET}'"

echo "=== [1/6] Remove crashed vLLM container (if any) ==="
if docker ps -a --format '{{.Names}}' | grep -q '^vllm-qwen38$'; then
  docker update --restart=no vllm-qwen38 >/dev/null 2>&1 || true
  docker rm -f vllm-qwen38 >/dev/null
  echo "removed vllm-qwen38 (the upstream-image path is not used)"
else
  echo "nothing to remove"
fi

echo "=== [2/6] API key (${ENV_FILE}) ==="
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
if ! grep -q '^VLLM_API_KEY=' "$ENV_FILE"; then
  echo "VLLM_API_KEY=$(openssl rand -hex 24)" >> "$ENV_FILE"
  echo "generated VLLM_API_KEY"
else
  echo "reusing existing VLLM_API_KEY (gateway contract unchanged)"
fi

echo "=== [3/6] Build llama.cpp (CUDA sm_${CUDA_ARCH}) — 10-30 min, skipped if present ==="
if [[ -x "$BUILD_DIR/build/bin/llama-server" ]]; then
  echo "existing build found: $("$BUILD_DIR/build/bin/llama-server" --version 2>&1 | head -n1)"
  read -r -p "Rebuild from latest master? [y/N] " A
  REBUILD="${A}"
else
  REBUILD="y"
fi
if [[ "$REBUILD" =~ ^[Yy] || ! -x "$BUILD_DIR/build/bin/llama-server" ]]; then
  # sudo resets PATH; DGX OS keeps CUDA in /usr/local/cuda — find it ourselves:
  if ! command -v nvcc >/dev/null 2>&1; then
    for cand in /usr/local/cuda/bin /usr/local/cuda-*/bin; do
      if [[ -x "$cand/nvcc" ]]; then PATH="$cand:$PATH"; export PATH; echo "found CUDA at $cand"; break; fi
    done
  fi
  command -v nvcc >/dev/null 2>&1 || {
    echo "✗ nvcc not found (checked PATH and /usr/local/cuda*)"
    echo "  Fix: sudo apt-get update && sudo apt-get install -y cuda-toolkit"
    echo "  (или, если toolkit в нестандартном месте: sudo env PATH=/путь/до/cuda/bin:\$PATH bash $0)"
    exit 1; }
  echo "nvcc: $(nvcc --version | awk -F'release ' '/release/ {print $2}' | cut -d, -f1)"
  if [[ -d "$BUILD_DIR/.git" ]]; then git -C "$BUILD_DIR" pull --ff-only
  else rm -rf "$BUILD_DIR"; git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$BUILD_DIR"; fi
  cmake -S "$BUILD_DIR" -B "$BUILD_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" -DGGML_NATIVE=ON
  cmake --build "$BUILD_DIR/build" --config Release -j"$(nproc)"
fi
BIN="$BUILD_DIR/build/bin/llama-server"
"$BIN" --help 2>&1 | grep -q -- '--spec-type' || { echo "✗ llama.cpp too old (no --spec-type) — remove $BUILD_DIR and re-run"; exit 1; }

echo "=== [4/6] Download model files (~20 GB, resumable) ==="
mkdir -p "$MODEL_DIR"
"$HF" download unsloth/Qwen3.8-27B-GGUF "$QWEN_GGUF" "$MMPROJ" --local-dir "$MODEL_DIR"
# drafter: NVIDIA recipe repo (incoai); z-lab is an identical mirror fallback
"$HF" download incoai/Qwen3.8-27B-DFlash2-GGUF "$DRAFT_GGUF" --local-dir "$MODEL_DIR" \
  || "$HF" download z-lab/Qwen3.8-27B-DFlash2-GGUF "$DRAFT_GGUF" --local-dir "$MODEL_DIR"
chmod -R a+rX "$MODEL_DIR"
ls -lh "$MODEL_DIR"/*.gguf

echo "=== [5/6] systemd service qwen38-llamacpp (:${PORT}) ==="
BUDGET_FLAG=""
[[ -n "$REASONING_BUDGET" ]] && BUDGET_FLAG="--reasoning-budget ${REASONING_BUDGET}"
cat > /etc/systemd/system/qwen38-llamacpp.service <<EOF
[Unit]
Description=llama.cpp server — Qwen3.8-27B (unsloth ${QWEN_GGUF} + DFlash2 spec-decoding + vision)
Documentation=https://github.com/nvidia/local-ai-inference-recipes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=$ENV_FILE
ExecStart=$BIN \\
  --model $MODEL_DIR/$QWEN_GGUF \\
  --mmproj $MODEL_DIR/$MMPROJ \\
  --backend-sampling \\
  --batch-size 4096 \\
  --ubatch-size 512 \\
  --ctx-size $QWEN_CTX \\
  --parallel 1 \\
  -ngl 999 \\
  --spec-type draft-dflash \\
  --spec-draft-n-max 7 \\
  --spec-draft-model $MODEL_DIR/$DRAFT_GGUF \\
  --spec-draft-backend-sampling \\
  --temp 1.0 \\
  --top-p 0.95 \\
  --top-k 20 \\
  --min-p 0.0 \\
  --presence-penalty 0.0 \\
  --repeat-penalty 1.0 \\
  $BUDGET_FLAG \\
  --jinja \\
  --metrics \\
  --host 0.0.0.0 --port $PORT \\
  --api-key \${VLLM_API_KEY}
Restart=on-failure
RestartSec=5
TimeoutStartSec=900
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable qwen38-llamacpp
systemctl restart qwen38-llamacpp

echo "=== [6/6] Wait for /health (weights load: 1-3 min) ==="
CODE=000
for i in $(seq 1 300); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/health || true)"
  [[ "$CODE" == "200" ]] && break
  if ! systemctl is-active --quiet qwen38-llamacpp; then
    echo "✗ service died — last logs:"; journalctl -u qwen38-llamacpp -n 40 --no-pager; exit 1
  fi
  sleep 2
done
if [[ "$CODE" == "200" ]]; then
  echo "✓ llama-server (Qwen3.8-27B) healthy on :${PORT}"
  KEY="$(grep -oP '(?<=^VLLM_API_KEY=).*' "$ENV_FILE")"
  curl -s "http://127.0.0.1:${PORT}/props" -H "Authorization: Bearer $KEY" | jq '{total_slots, version}' 2>/dev/null || true
  echo "quick smoke (8 tokens, thinking off via budget=${REASONING_BUDGET})..."
  curl -s "http://127.0.0.1:${PORT}/v1/chat/completions" -H "Authorization: Bearer $KEY" \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen3.8-27b","max_tokens":8,"messages":[{"role":"user","content":"Say OK"}]}' \
    | jq -r '.choices[0].message.content // "no content"' | head -c 80; echo
else
  echo "✗ /health=$CODE after 10 min — journalctl -u qwen38-llamacpp -n 80"; exit 1
fi

echo
echo "Engine ready on :${PORT} with the SAME contract as the vLLM path."
echo "Next: sudo bash scripts/04-deploy-web.sh"
