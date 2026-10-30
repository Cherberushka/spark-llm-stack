#!/usr/bin/env bash
# =============================================================================
# 06-setup-pi.sh — Configure the Pi coding agent (pi.dev) for this stack
# Run ON THE WORKSTATION where Pi will run (Linux/macOS), or on the Spark itself:
#   bash scripts/06-setup-pi.sh
#
# Pi connects to the LiteLLM gateway (single OpenAI-compatible API) via
# ~/.pi/agent/models.json — provider "spark", model "qwen3.8-27b".
# =============================================================================
set -euo pipefail

# Pi config is PER-USER (~/.pi/agent). If invoked via sudo, write to the
# invoking user's home, not /root:
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  TARGET_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  TARGET_USER="$SUDO_USER"; TARGET_GROUP="$(id -gn "$SUDO_USER")"
else
  TARGET_HOME="$HOME"; TARGET_USER=""; TARGET_GROUP=""
fi
PI_DIR="${TARGET_HOME}/.pi/agent"
CFG="${PI_DIR}/models.json"

# --- Spark address & key -----------------------------------------------------
if [[ -f /etc/spark-llm.env ]]; then
  DEFAULT_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  DEFAULT_KEY="$(grep -oP '(?<=^LITELLM_MASTER_KEY=).*' /etc/spark-llm.env || true)"
else
  DEFAULT_IP=""; DEFAULT_KEY=""
fi
read -r -p "DGX Spark IP [${DEFAULT_IP:-<ip>}]: " SPARK_IP;  SPARK_IP="${SPARK_IP:-$DEFAULT_IP}"
read -r -p "LiteLLM key (LITELLM_MASTER_KEY) [hidden]: " -s GW_KEY; echo
GW_KEY="${GW_KEY:-$DEFAULT_KEY}"
[[ -n "$SPARK_IP" && -n "$GW_KEY" ]] || { echo "Need Spark IP and gateway key (on server: /etc/spark-llm.env). Aborting."; exit 1; }

# --- install Pi if missing (ALWAYS as the target user, never root) ----------
PI_BIN="${PI_DIR}/bin/pi"
if [[ ! -x "$PI_BIN" ]] && { [[ -n "$TARGET_USER" ]] || ! command -v pi >/dev/null 2>&1; }; then
  echo "Pi not found for ${TARGET_USER:-$USER}. Install options:"
  echo "  curl -fsSL https://pi.dev/install.sh | sh"
  echo "  npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
  read -r -p "Run the official install.sh now (as ${TARGET_USER:-$USER})? [y/N] " A
  if [[ "$A" =~ ^[Yy] ]]; then
    if [[ -n "$TARGET_USER" ]]; then
      sudo -u "$TARGET_USER" bash -c 'curl -fsSL https://pi.dev/install.sh | sh'
    else
      curl -fsSL https://pi.dev/install.sh | sh
    fi
  fi
fi

# --- models.json -------------------------------------------------------------
mkdir -p "$PI_DIR"
if [[ -f "$CFG" && "${FORCE:-0}" != "1" ]]; then
  echo "⚠ $CFG exists — re-run with FORCE=1 to overwrite. Aborting."
  exit 1
fi
cat > "$CFG" <<EOF
{
  "\$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "spark": {
      "name": "DGX Spark (LiteLLM gateway)",
      "baseUrl": "http://${SPARK_IP}:4000/v1",
      "apiKey": "\$SPARK_LLM_KEY",
      "api": "openai-completions",
      "models": [
        {
          "id": "qwen3.8-27b",
          "name": "Qwen3.8 27B NVFP4 (Spark)",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 262144,
          "maxTokens": 32768,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
EOF
chmod 600 "$CFG"
[[ -n "$TARGET_USER" ]] && chown -R "$TARGET_USER:$TARGET_GROUP" "${TARGET_HOME}/.pi"
echo "✓ wrote $CFG (for user ${TARGET_USER:-$(whoami)})"

cat <<EOF

────────────────────────────────────────────────────────────────
 Finish setup:

 1) Put the gateway key into the USER shell profile (not into the json):
      echo 'export SPARK_LLM_KEY=${GW_KEY}' >> ${TARGET_HOME}/.bashrc
      (выполните от имени пользователя ${TARGET_USER:-$(whoami)}; on zsh use ~/.zshrc)

 2) Run Pi with the Spark model (Pi ≥ 1.0: провайдер/модель через СЛЭШ;
    двоеточие — это уровень thinking, например spark/qwen3.8-27b:high):
      pi --model spark/qwen3.8-27b

 3) Optional — engineering skills for Pi (from mattpocock/skills):
      see that repo's README → skills.sh install for Pi (-a pi)

 Docs: https://pi.dev/docs  (models.json reference: /docs/latest/models-configuration)
 Tip:  in agent loops keep thinking off/low (chat_template_kwargs) — see runbook §8.
────────────────────────────────────────────────────────────────
EOF
