#!/usr/bin/env bash
# =============================================================================
# 04-deploy-web.sh — Deploy gateway + web layer: LiteLLM + Open WebUI + Caddy
# Run as root: sudo bash scripts/04-deploy-web.sh
#
# - copies stack/.env.example -> stack/.env (if missing)
# - syncs keys from /etc/spark-llm.env (VLLM_API_KEY,
#   LITELLM_MASTER_KEY) into stack/.env
# - docker compose up -d (litellm + open-webui + caddy)
# - waits for: LiteLLM :4000 and the HTTPS front door :443
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(dirname "$SCRIPT_DIR")/stack"
cd "$STACK_DIR"

echo "=== [1/4] .env ==="
[[ -f .env ]] || cp .env.example .env
upsert_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then sed -i "s|^${key}=.*|${key}=${val}|" .env
  else echo "${key}=${val}" >> .env; fi
}

ENV_FILE=/etc/spark-llm.env
[[ -f "$ENV_FILE" ]] || { echo "✗ $ENV_FILE missing — run scripts/03 first"; exit 1; }
# LiteLLM admin UI / virtual keys require Postgres — generate its password once:
grep -q '^LITELLM_DB_PASSWORD=' "$ENV_FILE" || echo "LITELLM_DB_PASSWORD=$(openssl rand -hex 16)" >> "$ENV_FILE"
grep -q '^LITELLM_REDIS_PASSWORD=' "$ENV_FILE" || echo "LITELLM_REDIS_PASSWORD=$(openssl rand -hex 16)" >> "$ENV_FILE"
for K in VLLM_API_KEY LITELLM_MASTER_KEY LITELLM_DB_PASSWORD LITELLM_REDIS_PASSWORD; do
  if grep -q "^${K}=" "$ENV_FILE"; then
    upsert_env "$K" "$(grep -oP "(?<=^${K}=).*" "$ENV_FILE")"
  fi
done
chmod 600 .env
echo "keys synced into stack/.env"

echo "=== [2/5] self-signed cert (SAN: localhost + hostname + LAN IP; works without SNI) ==="
CERT_DIR="$STACK_DIR/caddy/certs"
if [[ -f "$CERT_DIR/spark.crt" ]]; then
  echo "cert exists: $(openssl x509 -in "$CERT_DIR/spark.crt" -noout -ext subjectAltName 2>/dev/null | tail -1)"
else
  mkdir -p "$CERT_DIR"
  HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  HOST_NAME="$(hostname -s 2>/dev/null || echo spark)"
  SAN="DNS:localhost,DNS:${HOST_NAME},IP:127.0.0.1"
  [[ -n "$HOST_IP" ]] && SAN="$SAN,IP:${HOST_IP}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/spark.key" -out "$CERT_DIR/spark.crt" \
    -subj "/CN=Spark LLM" -addext "subjectAltName=$SAN"
  chmod 600 "$CERT_DIR/spark.key"
  echo "✓ generated cert, SAN: $SAN"
fi

echo "=== [3/5] pull images ==="
docker compose pull

echo "=== [4/5] start containers ==="
docker compose up -d
docker compose ps

echo "=== [5/5] wait for gateway + front door ==="
for i in $(seq 1 90); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4000/health/liveliness || true)"
  [[ "$CODE" == "200" ]] && break; sleep 2
done
echo "LiteLLM :4000 -> $CODE"
for i in $(seq 1 90); do
  CODE="$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/ || true)"
  [[ "$CODE" =~ ^(200|307|308)$ ]] && break; sleep 2
done
echo "HTTPS front door -> $CODE"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

────────────────────────────────────────────────────────────────
 Deployed. Entry points:

   Chat + admin panel :  https://${IP}/          (first registered user = ADMIN)
   Ops dashboard      :  https://${IP}/ops
   Unified LLM API    :  http://${IP}:4000/v1    (LiteLLM; key: LITELLM_MASTER_KEY)
                         models: qwen3.8-27b (chat/vision/tools) · bge-m3 (embeddings)
   Direct APIs (LAN)  :  vLLM :8000 · Ollama :11434

 Next: sudo bash scripts/05-verify.sh
       then Pi on your workstation: bash scripts/06-setup-pi.sh
────────────────────────────────────────────────────────────────
EOF
