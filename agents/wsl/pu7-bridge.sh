#!/usr/bin/env bash
# PU7: bridge checks — personal network consumes CENTRAL models; org tasks go via central intake
set -euo pipefail
source "$HOME/.spark-env"
echo "-- ваш расход на центральном шлюзе:"
curl -s http://$SPARK_IP:4000/v1/models -H "Authorization: Bearer $SPARK_LLM_KEY" | jq -r '.data[].id'
echo "-- орг-задачи ставятся в ЦЕНТРАЛЬНЫЙ оркестратор (не в персональный):"
echo "   P2: Open WebUI Tool create_task; сейчас — через админа или curl c ORCH_API_KEY сервера"
echo "-- персональная сеть = песочница: орг-данные не монтируйте в ~/agents-inbox"
echo "PU7 done. Return path to global: bash pu8-return-global.sh"
