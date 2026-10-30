#!/usr/bin/env bash
# =============================================================================
# 03-deploy-qwen38-vllm.sh — Qwen3.8-27B via vLLM NVFP4 (NVIDIA official recipe)
# Run as root: sudo bash scripts/03-deploy-qwen38-vllm.sh
#
# Source of truth: nvidia/local-ai-inference-recipes
#   models/qwen/qwen3.8-27b/dgx-spark/linux/vllm/128gb/nvfp4/recipe.yaml
#   (validated 2026-10-19 on DGX Spark 128GB, driver 580.126.20, CUDA 13.4;
#    measured 37.3 tok/s decode, concurrency 8, 262K context)
#
# Checkpoints: nvidia/Qwen3.8-27B-NVFP4  +  z-lab/Qwen3.8-27B-DFlash2 (drafter)
#
# IMAGE choice — READ THIS:
#   DEFAULT  nvcr.io/nvidia/vllm:26.10.pre1-py3 — the literal recipe image.
#            It is the ONLY image carrying the NVIDIA patches the recipe needs
#            (vllm#54614 nvfp4_dynamic, #59021, #59025, #59244, #60252).
#            As of 2026-10-22 those PRs are OPEN (not merged upstream), so
#            Docker Hub images vllm/vllm-openai:* REJECT --kernel-config
#            (literal_error: nvfp4_dynamic) and DFlash+prefix-caching may
#            corrupt output there (#60174). Do not use upstream images with
#            recipe flags.
#   NGC unreachable (e.g. Russia)? Use scripts/03b-deploy-qwen38-llamacpp.sh
#            (official llama.cpp recipe, no NGC needed), or import the NGC
#            image offline: pull it elsewhere (--platform linux/arm64) →
#            docker save | gzip → transfer → docker load (runbook Appendix A).
#
# WEIGHTS from huggingface.co: from Russia use the mirror (default here):
#   HF_ENDPOINT=https://hf-mirror.com   (override to https://huggingface.co)
#
# IMPORTANT recipe notes baked in below:
#  - mount ONLY ~/.cache/huggingface (the image carries prebuilt kernel/autotune
#    caches under /root/.cache — mounting over all of it hides them)
#  - --gpu-memory-utilization must stay well below 0.7 on Spark (recipe: 0.60;
#    at 0.85 the node hung during startup autotune). We default to 0.55 and
#    recommend 0.45 when the optional Nemotron engine is co-resident.
#
# First start downloads ~17 GB of weights and runs autotune: 15-45 min.
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.10.pre1-py3}"   # NGC, recipe-exact
MODEL="${QWEN_CHECKPOINT:-nvidia/Qwen3.8-27B-NVFP4}"
DRAFTER="${QWEN_DRAFTER:-z-lab/Qwen3.8-27B-DFlash2}"
GPU_UTIL="${QWEN_GPU_UTIL:-0.60}"        # exactly per NVIDIA recipe (0.85 hangs the node!)
MAX_MODEL_LEN="${QWEN_MAX_MODEL_LEN:-262144}"
MAX_NUM_SEQS="${QWEN_MAX_NUM_SEQS:-8}"
PORT="${QWEN_PORT:-8000}"
ENV_FILE=/etc/spark-llm.env
CONTAINER=vllm-qwen38

echo "=== [1/4] API keys (${ENV_FILE}) ==="
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
gen_key() { # VARNAME
  if grep -q "^$1=" "$ENV_FILE"; then echo "keeping existing $1"; else
    echo "$1=$(openssl rand -hex 24)" >> "$ENV_FILE"; echo "generated $1"; fi
}
gen_key VLLM_API_KEY
gen_key LITELLM_MASTER_KEY
VLLM_API_KEY="$(grep -oP '(?<=^VLLM_API_KEY=).*' "$ENV_FILE")"

echo "=== [2/4] Docker dirs + image pull (large, ~20 GB image) ==="
mkdir -p /root/.cache/huggingface
if ! docker pull "$IMAGE"; then
  case "$IMAGE" in
    nvcr.io/*)
      echo "⚠ pull failed — most common NGC cause: stale docker credentials."
      echo "  retrying once after 'docker logout nvcr.io' ..."
      docker logout nvcr.io >/dev/null 2>&1 || true ;;
    *)
      echo "⚠ pull failed — retrying once ..." ;;
  esac
  if ! docker pull "$IMAGE"; then
    cat <<'HINT'
✗ Image pull failed. Check in order:

1) Docker Hub доступен? (образ по умолчанию — оттуда)
     curl -s -o /dev/null -w 'HTTP %{http_code}\n' https://registry-1.docker.io/v2/
   Норма — 401. Если таймаут/блок — пропишите registry-зеркало в
   /etc/docker/daemon.json:  {"registry-mirrors": ["https://<ваше-зеркало>"]}
   затем: sudo systemctl restart docker

2) Прокси для docker-демона (curl ходит напрямую, dockerd — нет?):
     sudo systemctl show docker --property=Environment
   При необходимости: sudo systemctl edit docker → [Service]
   Environment="HTTPS_PROXY=http://proxy:3128" ... → daemon-reload + restart docker

3) NGC-образ из рецепта (если nvcr.io доступен / через VPN):
     VLLM_IMAGE=nvcr.io/nvidia/vllm:26.10.pre1-py3 sudo -E bash scripts/03-deploy-qwen38-vllm.sh

4) Community-образ для Spark (Docker Hub):
     VLLM_IMAGE=eugr/spark-vllm:nightly-20261015 sudo -E bash scripts/03-deploy-qwen38-vllm.sh

Примечание: веса модели качаются изнутри контейнера с HF; из РФ по умолчанию
используется зеркало hf-mirror.com (переопределяется: HF_ENDPOINT=...).
HINT
    exit 1
  fi
fi

echo "=== [3/4] Start ${CONTAINER} (recipe flags verbatim + api-key) ==="
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# --enable-flashinfer-autotune ships in the NGC image; upstream builds may not
# know the flag. Auto: on for nvcr.io images, off otherwise. Override:
# DROP_AUTOTUNE=1 (force off) or DROP_AUTOTUNE=0 (force on).
case "${DROP_AUTOTUNE:-auto}" in
  1)    AUTOTUNE_FLAG="" ;;
  0)    AUTOTUNE_FLAG="--enable-flashinfer-autotune" ;;
  auto) case "$IMAGE" in nvcr.io/*) AUTOTUNE_FLAG="--enable-flashinfer-autotune" ;; *) AUTOTUNE_FLAG="" ;; esac ;;
esac
echo "image: $IMAGE   autotune-flag: ${AUTOTUNE_FLAG:-<none>}   HF_ENDPOINT: ${HF_ENDPOINT:-https://hf-mirror.com}"

docker run -d --name "$CONTAINER" \
  --gpus all --ipc host \
  --restart unless-stopped \
  -p "${PORT}:8000" \
  --shm-size=16g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -e HF_TOKEN="${HF_TOKEN:-}" \
  -e HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}" \
  -v /root/.cache/huggingface:/root/.cache/huggingface \
  "$IMAGE" \
  vllm serve "$MODEL" \
    --served-model-name qwen3.8-27b \
    --host 0.0.0.0 --port 8000 \
    --api-key "$VLLM_API_KEY" \
    --kv-cache-dtype fp8 \
    --max-model-len "$MAX_MODEL_LEN" \
    --max-num-seqs "$MAX_NUM_SEQS" \
    --async-scheduling \
    $AUTOTUNE_FLAG \
    --gpu-memory-utilization "$GPU_UTIL" \
    --kernel-config '{"linear_backend":"nvfp4_dynamic","nvfp4_dynamic_max_tokens":16,"unquantized_linear_backend":"flashinfer_cudnn","linear_backend_per_quant":{"fp8_w8a8":"cutlass"}}' \
    --speculative-config "{\"method\":\"dflash\",\"model\":\"${DRAFTER}\",\"num_speculative_tokens\":7,\"draft_sample_method\":\"probabilistic\",\"rejection_sample_method\":\"standard\"}" \
    --compilation-config '{"pass_config":{"fuse_act_quant":true},"max_cudagraph_capture_size":64}' \
    --reasoning-parser qwen3 \
    --tool-call-parser qwen3_xml \
    --enable-auto-tool-choice

echo "=== [4/4] Wait for /health (downloads + autotune: 15-45 min first time) ==="
echo "follow: docker logs -f ${CONTAINER}"
for i in $(seq 1 1350); do   # up to 45 min
  CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/health || true)"
  [[ "$CODE" == "200" ]] && break
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "✗ container died — last logs:"; docker logs --tail 40 "$CONTAINER"; exit 1
  fi
  sleep 2
done
if [[ "$CODE" == "200" ]]; then
  echo "✓ vLLM healthy on :${PORT} (model name: qwen3.8-27b)"
  curl -s http://127.0.0.1:${PORT}/v1/models -H "Authorization: Bearer $VLLM_API_KEY" | jq -r '.data[].id' 2>/dev/null || true
else
  echo "✗ not healthy after 45 min — docker logs ${CONTAINER} | tail -50"; exit 1
fi

echo "=== tokenizer_config sanity check (known NVFP4-build quirk) ==="
# Some Qwen3.8-27B-NVFP4 tokenizer_config.json builds ship a small
# model_max_length (e.g. 2048). Server-side context is governed by
# --max-model-len (262144 above), but the check documents the quirk and
# scripts/05-verify.sh proves there is no silent truncation.
TOKCFG="$(find /root/.cache/huggingface -name tokenizer_config.json -path '*Qwen3.8*' 2>/dev/null | head -1 || true)"
if [[ -n "$TOKCFG" ]]; then
  MML="$(grep -o '"model_max_length"[[:space:]]*:[[:space:]]*[^,}]*' "$TOKCFG" | head -1 || echo 'model_max_length: not set')"
  echo "  $TOKCFG → $MML"
  case "$MML" in
    *2048*|*4096*|*8192*)
      echo "  ⚠ small model_max_length in tokenizer config — known quirk of some builds."
      echo "    vLLM serves up to --max-model-len ${MAX_MODEL_LEN} regardless; client SDKs"
      echo "    that truncate locally must not trust this field. Verify: scripts/05-verify.sh"
      echo "    runs a >2048-token probe. Optional fix: edit model_max_length in the cached"
      echo "    tokenizer_config.json and restart the container." ;;
    *) echo "  ✓ no small-limit quirk detected" ;;
  esac
else
  echo "  (tokenizer_config.json not in cache yet — will appear after first load)"
fi

echo
echo "API key: $VLLM_API_KEY"
echo "Next: sudo bash scripts/04-deploy-web.sh"
