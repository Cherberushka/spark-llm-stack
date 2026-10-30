#!/usr/bin/env bash
# PU2: connectivity + personal key (run INSIDE WSL)
# Needs: SPARK_IP (default 192.168.11.251) and a PERSONAL virtual key from admin
set -euo pipefail
SPARK_IP="${SPARK_IP:-192.168.11.251}"
read -r -p "Spark IP [$SPARK_IP]: " I; SPARK_IP="${I:-$SPARK_IP}"
read -r -s -p "Персональный виртуальный ключ LiteLLM (sk-...): " KEY; echo
[[ -n "$KEY" ]] || { echo "✗ ключ обязателен (админ: LiteLLM UI :8443 → Virtual Keys)"; exit 1; }
curl -sf --max-time 8 "http://$SPARK_IP:4000/v1/models" -H "Authorization: Bearer $KEY" >/dev/null \
  || { echo "✗ шлюз недоступен или ключ невалиден (проверьте подсеть 192.168.8-11 / VPN)"; exit 1; }
umask 077; cat > "$HOME/.spark-env" <<E
export SPARK_IP=$SPARK_IP
export SPARK_LLM_KEY=$KEY
export GATEWAY_URL=http://$SPARK_IP:4000/v1
E
echo 'grep -q .spark-env ~/.bashrc || echo "source ~/.spark-env" >> ~/.bashrc' | bash
echo "✓ gateway ok, ключ в ~/.spark-env (600). Next: bash pu3-pi.sh"
