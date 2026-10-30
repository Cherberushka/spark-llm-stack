#!/usr/bin/env bash
# =============================================================================
# 13-add-doc.sh — добавить документ в корпус и (опционально) сразу проверить
# Usage:
#   sudo bash scripts/13-add-doc.sh /путь/к/файлу.pdf
#   sudo bash scripts/13-add-doc.sh /путь/к/файлу.pdf "Вопрос, ответ на который вы знаете"
# Поддерживаются: pdf (текст/скан), docx, odt, rtf, html, htm, md
# =============================================================================
set -euo pipefail
SRC="${1:?путь к файлу}"; Q="${2:-}"
INBOX=/srv/agents/kb-inbox
OKEY="$(grep -oP '(?<=^ORCH_API_KEY=).*' /etc/orchestrator.env)"
BASE="$(basename "$SRC")"

wait_task() { # id timeout_s
  local tid="$1" st=""
  for _ in $(seq 1 "${2:-90}"); do
    st="$(curl -s http://127.0.0.1:8500/api/tasks/$tid | jq -r .task.status)"
    [[ "$st" == done || "$st" == failed ]] && break; sleep 2
  done
  echo "$st"
}

echo "== [1/3] inbox + задача doc-convert: $BASE"
if [[ "$(realpath "$SRC")" == "$(realpath "$INBOX/$BASE" 2>/dev/null || echo -)" ]]; then
  echo "файл уже в inbox — копирование пропущено"
else
  install -m 644 "$SRC" "$INBOX/$BASE"
fi
T1="$(curl -s -X POST http://127.0.0.1:8500/api/tasks -H "X-Orch-Key: $OKEY" -H 'Content-Type: application/json' \
  -d "{\"template\":\"doc-convert\",\"payload\":{\"file\":\"$BASE\"},\"requester\":\"cli\",\"group\":\"administration\"}" | jq -r .id)"
ST1="$(wait_task "$T1" 180)"   # до 6 мин: длинные сканы OCR'ятся дольше
echo "task #$T1: $ST1"
curl -s http://127.0.0.1:8500/api/tasks/$T1 | jq -r '.task.artifacts[]?, (.steps[]? | "  шаг \(.idx) \(.role): \(.status)")'
[[ "$ST1" == done ]] || { echo "✗ конвертация не удалась — смотрите .steps[].output"; exit 1; }
curl -s http://127.0.0.1:8502/health | jq -c .

if [[ -n "$Q" ]]; then
  echo "== [2/3] doc-qa: $Q"
  T2="$(curl -s -X POST http://127.0.0.1:8500/api/tasks -H "X-Orch-Key: $OKEY" -H 'Content-Type: application/json' \
    -d "{\"template\":\"doc-qa\",\"payload\":{\"question\":\"$Q\"},\"requester\":\"cli\",\"group\":\"administration\"}" | jq -r .id)"
  ST2="$(wait_task "$T2" 60)"
  echo "task #$T2: $ST2"
  echo "== [3/3] ответ:"
  curl -s http://127.0.0.1:8500/api/tasks/$T2 | jq -r '.steps[-1].output'
else
  echo "== [2/3] вопрос не задан — проверка: curl -s http://127.0.0.1:8502/health"
  echo "== [3/3] пропущено"
fi
