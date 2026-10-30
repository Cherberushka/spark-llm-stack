#!/usr/bin/env bash
# =============================================================================
# 20-log-hygiene.sh — гигиена логов/журналов стека (безопасно, идемпотентно)
#
# По умолчанию DRY-RUN: только показывает, что будет сделано и сколько места.
# Реальная очистка:  sudo bash scripts/20-log-hygiene.sh --apply
# Настройка глубины: KEEP_DAYS=14 sudo bash scripts/20-log-hygiene.sh --apply
#
# НЕ ТРОГАЕТ: Postgres/Redis (данные), SQLite-корпус RAG и vectors.f32,
#             /srv/agents/artifacts (артефакты задач), /root/backup (бэкапы),
#             веса моделей, конфиги.
# =============================================================================
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
LOGDIR=/var/log/spark-llm
KEEP_DAYS="${KEEP_DAYS:-7}"

run() {
  if [[ $APPLY -eq 1 ]]; then eval "$@"; else echo "  [dry] $*"; fi
}

echo "=== 20. Гигиена логов (apply=$APPLY, KEEP_DAYS=$KEEP_DAYS) ==="
echo "-- до:"
du -sh "$LOGDIR" /var/log/journal /var/lib/docker/containers 2>/dev/null || true

echo "-- 1) digest/backup-логи старше ${KEEP_DAYS} дней -> gzip, .gz старше 30 дней -> удалить"
run "find $LOGDIR -maxdepth 1 \\( -name 'digest-*.txt' -o -name '*.log' \\) -mtime +$KEEP_DAYS -exec gzip -f {} \\; 2>/dev/null || true"
run "find $LOGDIR -maxdepth 1 -name '*.gz' -mtime +30 -delete 2>/dev/null || true"

echo "-- 2) journald: вакуум до 200 МБ"
run "journalctl --vacuum-size=200M >/dev/null 2>&1 || true"

echo "-- 3) временные файлы тестов и импорта"
run "rm -rf /tmp/rag-v1test /tmp/rag-v2test /tmp/rag-migratetest /tmp/mcp-e2e* 2>/dev/null || true"
run "find /srv/agents/rag-svc/tmp -type f -mtime +$KEEP_DAYS -delete 2>/dev/null || true"

echo "-- 4) журнал массового импорта: архив + оставить хвост 200 строк"
J=/srv/agents/rag-svc/bulk-journal.jsonl
if [[ -s $J ]]; then
  run "cp $J $J.$(date +%F).bak && tail -200 $J > $J.tmp && mv $J.tmp $J"
  run "find /srv/agents/rag-svc -maxdepth 1 -name 'bulk-journal.jsonl.*.bak' -mtime +14 -delete 2>/dev/null || true"
else
  echo "  журнала нет — пропускаем"
fi

echo "-- 5) события задач оркестратора старше 30 дней (Postgres)"
run "sudo -u postgres psql -d spark -tAc \"DELETE FROM task_events WHERE created_at < now() - interval '30 days'\" >/dev/null 2>&1 || true"

echo "-- 6) docker: висячие образы + лог-файлы контейнеров крупнее 50 МБ"
run "docker image prune -f >/dev/null 2>&1 || true"
run "find /var/lib/docker/containers -name '*-json.log' -size +50M -exec truncate -s 0 {} \\; 2>/dev/null || true"

echo "-- после:"
du -sh "$LOGDIR" /var/log/journal /var/lib/docker/containers 2>/dev/null || true
echo
if [[ $APPLY -eq 1 ]]; then echo "Готово."; else echo "DRY-RUN. Для реальной очистки: sudo bash scripts/20-log-hygiene.sh --apply"; fi
