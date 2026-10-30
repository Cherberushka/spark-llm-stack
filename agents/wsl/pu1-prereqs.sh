#!/usr/bin/env bash
# PU1: WSL Ubuntu prerequisites (run INSIDE WSL)
set -euo pipefail
grep -qi microsoft /proc/version || { echo "✗ Запустите внутри WSL Ubuntu"; exit 1; }
. /etc/os-release; echo "Ubuntu: $VERSION_ID"
sudo apt-get update -y
sudo apt-get install -y git curl jq python3-venv python3-pip pandoc poppler-utils \
  ocrmypdf tesseract-ocr tesseract-ocr-rus build-essential openssh-client
ls /mnt/c >/dev/null && echo "✓ Windows-диски видны (/mnt/c) — но репо держите в FS WSL ($HOME): так в разы быстрее"
echo "PU1 done. Next: bash pu2-connect.sh"
