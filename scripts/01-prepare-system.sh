#!/usr/bin/env bash
# =============================================================================
# 01-prepare-system.sh — Base system preparation on DGX Spark (DGX OS, arm64)
# Run as root: sudo bash scripts/01-prepare-system.sh
# - apt update + required packages
# - directories /opt/models, /opt/llama.cpp
# - ufw firewall: SSH + 80/443 open; service ports (11434/8000/4000) LAN-only
# Idempotent: safe to re-run.
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

echo "=== [1/5] apt update + packages ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  git build-essential cmake pkg-config \
  curl wget ca-certificates gnupg \
  python3 python3-venv python3-pip \
  ufw nvtop jq net-tools
# docker compose plugin: present on DGX OS; install only if missing
if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y docker-compose-plugin || true
fi

# Optional but recommended (run manually when convenient; may require reboot):
#   apt-get full-upgrade -y && reboot
echo "note: consider 'sudo apt full-upgrade && sudo reboot' to pull DGX OS updates"

echo "=== [2/5] directories ==="
mkdir -p /opt/models /opt/hf-venv
chmod 755 /opt/models

echo "=== [3/5] Hugging Face CLI (venv, Ubuntu 24.04 is PEP-668 managed) ==="
if [[ ! -x /opt/hf-venv/bin/hf ]]; then
  python3 -m venv /opt/hf-venv
  /opt/hf-venv/bin/pip install --upgrade pip >/dev/null
  /opt/hf-venv/bin/pip install --upgrade "huggingface_hub[cli]" >/dev/null
fi
/opt/hf-venv/bin/hf version && echo "hf CLI ready at /opt/hf-venv/bin/hf"

echo "=== [4/6] docker group for the invoking user ==="
# So that `docker logs/ps/compose` work without sudo afterwards (standard step
# on DGX Spark; NOTE: docker group ≈ root on this host — admins only).
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  if id -nG "$SUDO_USER" | grep -qw docker; then
    echo "$SUDO_USER already in the docker group"
  else
    usermod -aG docker "$SUDO_USER"
    echo "✓ added $SUDO_USER to the 'docker' group (applies after re-login or 'newgrp docker')"
  fi
else
  echo "(run via sudo by a real user to auto-add them to the docker group)"
fi

echo "=== [5/6] firewall: ufw + DOCKER-USER (org subnets) ==="
# Org LAN: 192.168.8.0 – 192.168.11.255 (x = 8..11). One contiguous /22 covers
# all four /24s. Override with:  LAN_NETS="192.168.8.0/24 10.5.0.0/16" sudo -E bash $0
LAN_NETS="${LAN_NETS:-192.168.8.0/22}"
echo "org LAN nets: $LAN_NETS"

# --- ufw: host-level services (INPUT path) -----------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp   comment 'LLM stack: HTTP -> HTTPS redirect'
ufw allow 443/tcp  comment 'LLM stack: Caddy (Open WebUI + APIs + /ops) — has own auth'
# remove old broad rules if this script ran with an older default:
for old in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
  ufw delete allow from "$old" to any port 11434 proto tcp >/dev/null 2>&1 || true
  ufw delete allow from "$old" to any port 8000  proto tcp >/dev/null 2>&1 || true
  ufw delete allow from "$old" to any port 4000  proto tcp >/dev/null 2>&1 || true
done
for net in $LAN_NETS 172.16.0.0/12; do   # 172.16/12 = docker bridges -> host services
  ufw allow from "$net" to any port 11434 proto tcp comment 'Ollama embeddings (org LAN + docker)'
done
# Containers reach the engine on :8000 via host-gateway (INPUT path, NOT covered
# by DOCKER-USER). Without this rule LiteLLM/Open WebUI hang on model calls:
ufw allow from 172.16.0.0/12 to any port 8000 proto tcp comment 'LLM engine :8000 from docker containers (host-gateway)'
# Org LAN may call the engine directly too (INPUT on host IP):
for net in $LAN_NETS; do
  ufw allow from "$net" to any port 8000 proto tcp comment 'LLM engine :8000 (org LAN)'
done
ufw --force enable

# --- DOCKER-USER: container-published ports ----------------------------------
# Docker-published ports (vLLM 8000, LiteLLM 4000) go through NAT/FORWARD and
# BYPASS ufw INPUT rules. The supported way to filter them is the DOCKER-USER
# chain. We restrict the API ports to the org subnets; 80/443 stay open (web
# UI + gateway have their own auth). Rules must be re-applied after reboot /
# docker restart -> persistent oneshot systemd unit.
mkdir -p /etc/default
cat > /etc/default/spark-firewall <<EOF
# Space-separated list of allowed source networks for Docker-published API ports
LAN_NETS="$LAN_NETS"
API_PORTS="8000 4000 8443"
EOF

cat > /usr/local/sbin/spark-docker-user-firewall.sh <<'EOF'
#!/usr/bin/env bash
# Applies org-LAN restrictions to Docker-published API ports via DOCKER-USER.
# Idempotent: flushes and rebuilds the chain (Docker keeps its own chains apart).
set -euo pipefail
. /etc/default/spark-firewall
iptables -N DOCKER-USER 2>/dev/null || true
iptables -F DOCKER-USER
iptables -A DOCKER-USER -s 127.0.0.0/8 -j RETURN        # localhost
iptables -A DOCKER-USER -s 172.16.0.0/12 -j RETURN      # docker bridges / host-gateway
for net in $LAN_NETS; do
  for port in $API_PORTS; do
    iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -s "$net" -j RETURN
  done
done
for port in $API_PORTS; do
  iptables -A DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -j DROP
done
iptables -A DOCKER-USER -j RETURN
echo "DOCKER-USER applied: API ports {$API_PORTS} restricted to {$LAN_NETS}"
EOF
chmod +x /usr/local/sbin/spark-docker-user-firewall.sh

cat > /etc/systemd/system/spark-docker-user.service <<'EOF'
[Unit]
Description=Restrict Docker-published LLM API ports to org LAN subnets
After=docker.service network-online.target
Wants=docker.service
Requires=iptables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/spark-docker-user-firewall.sh

[Install]
WantedBy=multi-user.target
EOF
# iptables.service may not exist on all images — drop the hard requirement then
systemctl cat iptables.service >/dev/null 2>&1 || sed -i '/Requires=iptables.service/d' /etc/systemd/system/spark-docker-user.service
systemctl daemon-reload
systemctl enable spark-docker-user.service
systemctl restart spark-docker-user.service || echo "⚠ DOCKER-USER apply failed now (docker may be down) — it will run at boot"

ufw status numbered | head -n 16
iptables -L DOCKER-USER -n --line-numbers | head -n 14

echo "=== [5/5] done ==="
echo "Base system prepared. Next: sudo bash scripts/02-install-ollama-embeddings.sh"
