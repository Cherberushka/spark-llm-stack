#!/usr/bin/env bash
# =============================================================================
# 00-check-prereqs.sh — Preflight checks for the DGX Spark LLM stack
# Run as root (sudo bash scripts/00-check-prereqs.sh)
# Verifies: architecture, GPU/driver, unified memory, disk, Docker + NVIDIA
# runtime, CUDA toolkit, network reachability.
# Exit code 0 = all critical checks passed.
# =============================================================================
set -uo pipefail

FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$1"; }

echo "=== DGX Spark preflight ==="

# --- 1. Architecture ---------------------------------------------------------
ARCH="$(uname -m)"
if [[ "$ARCH" == "aarch64" ]]; then
  ok "CPU architecture: aarch64 (Arm64, as expected on GB10)"
else
  bad "CPU architecture is '$ARCH' — this stack expects aarch64 (NVIDIA DGX Spark / GB10)"
fi

# --- 2. GPU + driver ---------------------------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_LINE="$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)"
  if [[ -n "$GPU_LINE" ]]; then
    ok "GPU visible: $GPU_LINE"
    case "$GPU_LINE" in
      *GB10*|*Spark*|*B100*) ok "Looks like a DGX Spark (GB10 Grace Blackwell)" ;;
      *) warn "GPU name doesn't look like GB10 — proceeding, but this guide targets DGX Spark" ;;
    esac
  else
    bad "nvidia-smi present but returned no GPU — driver problem?"
  fi
else
  bad "nvidia-smi not found — NVIDIA driver not installed (DGX OS should ship with it)"
fi

# --- 3. Unified memory (need >= ~120 GB usable on a 128 GB Spark) ------------
MEM_KB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
MEM_GB=$(( MEM_KB / 1024 / 1024 ))
if (( MEM_GB >= 110 )); then
  ok "Unified memory: ${MEM_GB} GB total (128 GB pool expected on DGX Spark)"
elif (( MEM_GB >= 56 )); then
  warn "Unified memory: ${MEM_GB} GB — this stack targets the 128 GB DGX Spark (NVIDIA vLLM recipe requires it); on 64 GB use the llama.cpp fallback (runbook Appendix A)"
else
  bad "Unified memory: ${MEM_GB} GB — too small for this stack (need >= 64 GB, target 128 GB)"
fi

# --- 4. Disk space -----------------------------------------------------------
# Need: Qwen Q5_K_M ~23 GB + Nemotron Bolt GGUF+mmproj+drafter ~30 GB
#       + docker images ~10 GB + headroom => recommend >= 150 GB free on /opt or /
AVAIL_GB_ROOT=$(df -BG --output=avail / | tail -n1 | tr -dc '0-9')
AVAIL_GB_OPT=$AVAIL_GB_ROOT
[[ -d /opt ]] && AVAIL_GB_OPT=$(df -BG --output=avail /opt | tail -n1 | tr -dc '0-9')
if (( AVAIL_GB_OPT >= 150 )); then
  ok "Disk: ${AVAIL_GB_OPT} GB free for /opt (>= 150 GB recommended)"
elif (( AVAIL_GB_OPT >= 90 )); then
  warn "Disk: ${AVAIL_GB_OPT} GB free for /opt — enough for both models, little headroom"
else
  bad "Disk: ${AVAIL_GB_OPT} GB free for /opt — need >= 90 GB (150 GB recommended)"
fi

# --- 5. Docker + compose + NVIDIA container runtime --------------------------
if command -v docker >/dev/null 2>&1; then
  ok "docker: $(docker --version 2>/dev/null | head -n1)"
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose: $(docker compose version --short 2>/dev/null)"
  else
    bad "docker compose plugin missing (apt install docker-compose-plugin)"
  fi
  if docker info 2>/dev/null | grep -qi 'nvidia'; then
    ok "NVIDIA container runtime registered in Docker"
  else
    warn "NVIDIA container runtime NOT detected in 'docker info' — DGX OS ships it preinstalled; install nvidia-container-toolkit if the web layer needs GPU (this stack keeps inference native, so Docker GPU is optional)"
  fi
else
  bad "docker not found (DGX OS ships with Docker preinstalled)"
fi

# --- 6. CUDA toolkit (REQUIRED for the default engine path A: llama.cpp build
#         for sm_121; path B/vLLM-NGC does not need nvcc) ---------------------
# sudo resets PATH (secure_path) and DGX OS keeps the toolkit in /usr/local/cuda:
if ! command -v nvcc >/dev/null 2>&1; then
  for cand in /usr/local/cuda/bin /usr/local/cuda-*/bin; do
    if [[ -x "$cand/nvcc" ]]; then PATH="$cand:$PATH"; export PATH; echo "  (found CUDA at $cand — added to PATH)"; break; fi
  done
fi
if command -v nvcc >/dev/null 2>&1; then
  NVCC_VER="$(nvcc --version | awk -F'release ' '/release/ {print $2}' | cut -d, -f1)"
  ok "nvcc: CUDA $NVCC_VER"
  NVCC_MAJOR="${NVCC_VER%%.*}"; NVCC_MINOR="${NVCC_VER#*.}"
  if (( NVCC_MAJOR >= 13 || (NVCC_MAJOR == 12 && NVCC_MINOR >= 8) )); then
    ok "CUDA >= 12.8 — supports Blackwell sm_121 (GB10)"
  else
    bad "CUDA < 12.8 — cannot target sm_121; update CUDA on DGX OS first"
  fi
else
  bad "nvcc not found — required for the default engine (llama.cpp build, path A)"
  echo "      Если /usr/local/cuda пуст: sudo apt-get update && sudo apt-get install -y cuda-toolkit"
fi

# --- 7. Build tools ----------------------------------------------------------
for t in git cmake make gcc python3 curl; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t present"; else warn "$t missing (script 01 installs it)"; fi
done

# --- 8. Network reachability -------------------------------------------------
for url in https://ollama.com https://registry-1.docker.io/v2/ https://hf-mirror.com https://huggingface.co https://github.com https://ghcr.io; do
  if curl -sS -o /dev/null --max-time 8 "$url"; then ok "reachable: $url"
  else warn "NOT reachable: $url (check DNS/proxy — model downloads will fail)"; fi
done

echo
if (( FAIL == 0 )); then
  echo "=== Preflight PASSED — proceed to 01-prepare-system.sh ==="
  exit 0
else
  echo "=== Preflight FAILED — fix the ✗ items above before continuing ==="
  exit 1
fi
