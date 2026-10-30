#!/usr/bin/env bash
# =============================================================================
# 19-import-clinrec.sh — массовый импорт корпуса (клинические рекомендации и пр.)
#
# Зачем отдельный сценарий, а не 777 задач doc-convert через оркестратор:
#   * импорт КР — часы, а не минуты: нужны resume, журнал и ПРОГНОЗ до старта;
#   * VLM-OCR сканов идёт через тот же llama.cpp, что обслуживает врачей, — поэтому
#     очередь с nice/ionice, лимитом страниц и ночным окном;
#   * оркестратор на каждый документ создал бы задачу и события в Postgres — мусор.
#
# Раскладка источника (специальность берётся из имени каталога):
#   /srv/kb/clinrec/кардиология/Артериальная гипертензия 2024.pdf
#   /srv/kb/clinrec/эндокринология/Сахарный диабет 2 типа 2025.pdf
#   /srv/kb/orders/Приказ 642н.pdf                -> --doctype order
#   /srv/kb/sop/СОП забор крови.docx              -> --doctype sop
#
# Run as root:
#   sudo bash scripts/19-import-clinrec.sh --dry-run                 # только прогноз
#   sudo bash scripts/19-import-clinrec.sh                           # импорт, авто-режим OCR
#   sudo bash scripts/19-import-clinrec.sh --night-only              # только 00:00-07:00
#   sudo bash scripts/19-import-clinrec.sh --limit 20                # прогон на 20 файлах
#   sudo bash scripts/19-import-clinrec.sh --src /srv/kb/orders --doctype order
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN=/opt/rag-venv/bin/python
INGEST=/opt/orchestrator/rag_bulk_ingest.py
SRC=/srv/kb/clinrec
EXTRA=()
DRY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY="--dry-run"; shift ;;
    --src)       SRC="$2"; shift 2 ;;
    --night-only) EXTRA+=("--night-only"); shift ;;
    --limit)     EXTRA+=("--limit" "$2"); shift 2 ;;
    --ocr)       EXTRA+=("--ocr" "$2"); shift 2 ;;
    --doctype)   EXTRA+=("--doctype" "$2"); shift 2 ;;
    --force)     EXTRA+=("--force"); shift ;;
    --help|-h)   sed -n '2,30p' "$0"; exit 0 ;;
    *)           EXTRA+=("$1"); shift ;;
  esac
done

[[ -x $PYBIN ]] || { echo "✗ нет $PYBIN — сначала sudo bash scripts/12-setup-rag-svc.sh"; exit 1; }
install -m 644 "$REPO/agents/orchestrator/rag_bulk_ingest.py" "$INGEST"
mkdir -p /srv/kb/clinrec /srv/agents/rag-svc/tmp /var/log/spark-llm

echo "=== [1/4] состояние поискового сервиса ==="
curl -sf http://127.0.0.1:8502/health | jq -c . \
  || { echo "✗ rag-svc не отвечает на 127.0.0.1:8502 — запустите scripts/12-setup-rag-svc.sh"; exit 1; }
curl -s http://127.0.0.1:8502/stats | jq -c '{backend,chunks,docs,dense_resident,recommendation}'

echo "=== [2/4] источник: $SRC ==="
if [[ ! -d $SRC ]]; then
  echo "✗ каталога нет. Создайте и положите туда PDF/DOCX:"
  echo "   mkdir -p $SRC/<специальность> && cp '/путь/к/КР.pdf' $SRC/<специальность>/"
  exit 1
fi
find "$SRC" -type f \( -iname '*.pdf' -o -iname '*.docx' -o -iname '*.odt' -o -iname '*.rtf' \
     -o -iname '*.md' -o -iname '*.html' -o -iname '*.txt' \) | wc -l | sed 's/^/  файлов: /'
du -sh "$SRC" | sed 's/^/  объём:  /'
echo "  записей в журнале импорта: $(wc -l < /srv/agents/rag-svc/bulk-journal.jsonl 2>/dev/null || echo 0)"

echo "=== [3/4] свободные ресурсы (импорт не должен уронить inference) ==="
awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2}
     END {printf "  RAM: %.1f ГБ свободно из %.1f ГБ\n", a/1048576, t/1048576}' /proc/meminfo
df -h /srv | tail -1 | awk '{print "  диск /srv: свободно "$4" из "$2}'
LLAMACPP_ACTIVE="$(systemctl is-active qwen38-llamacpp 2>/dev/null || echo unknown)"
echo "  движок qwen38-llamacpp: $LLAMACPP_ACTIVE (OCR сканов пойдёт через него)"
curl -s --max-time 5 http://127.0.0.1:8000/metrics -H "Authorization: Bearer $(grep -oP '(?<=^VLLM_API_KEY=).*' /etc/spark-llm.env 2>/dev/null)" \
  | grep -E '^llamacpp:(requests_processing|requests_deferred|kv_cache_usage_perc)' | sed 's/^/  /' || true

echo "=== [4/4] запуск ==="
echo "лог: /var/log/spark-llm/bulk-ingest.log   (Ctrl-C безопасно: журнал позволяет продолжить --resume)"
echo
# nice+ionice: импорт — фоновая работа, врачи не должны почувствовать
exec nice -n 19 ionice -c3 "$PYBIN" "$INGEST" \
  --src "$SRC" --specialty-from-dir --resume --ocr auto \
  --logfile /var/log/spark-llm/bulk-ingest.log \
  $DRY "${EXTRA[@]}"
