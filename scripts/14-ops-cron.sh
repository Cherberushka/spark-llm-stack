#!/usr/bin/env bash
# =============================================================================
# 14-ops-cron.sh — P7-lite: nightly backups + morning digest + logrotate
# Run as root: sudo bash scripts/14-ops-cron.sh   (идемпотентен)
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
STACK="${STACK_DIR:-$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)/spark-llm-stack}"
mkdir -p /root/backup /var/log/spark-llm

cat > /usr/local/sbin/spark-llm-backup.sh <<'EOS'
#!/usr/bin/env bash
set -uo pipefail
D=/root/backup/$(date +%F); mkdir -p "$D"
docker exec spark-litellm-db pg_dump -U llmadmin litellm      | gzip > "$D/litellm.sql.gz"
docker exec spark-litellm-db pg_dump -U orchestrator agents_tasks | gzip > "$D/agents_tasks.sql.gz"
docker run --rm -v spark-llm-web_open-webui-data:/data -v "$D":/backup alpine \
  tar czf /backup/open-webui.tgz -C /data . 2>/dev/null
tar czf "$D/config.tgz" /etc/spark-llm.env /etc/orchestrator.env /etc/llama-server.env \
  /etc/systemd/system/qwen38-llamacpp.service /etc/systemd/system/agents-orchestrator.service \
  /etc/systemd/system/rag-svc.service "__STACK__/stack/.env" \
  "__STACK__/stack/litellm/config.yaml" "__STACK__/stack/caddy/Caddyfile" 2>/dev/null
# Корпус: index.db (тексты+метаданные, источник правды) и исходники — берём.
# vectors.f32 НЕ берём: он пересобирается из index.db (POST /reindex), а весит на
# 100k чанков ~430 МБ несжимаемых float32 — ночной бэкап вырос бы в разы.
# Том Qdrant (если включён) — тоже пересобираемый индекс, берётся отдельно и только
# при RAG_BACKup_QDRANT=1.
tar czf "$D/agents.tgz" \
  --exclude='rag-svc/vectors.f32' --exclude='rag-svc/tmp' \
  -C /srv/agents . 2>/dev/null
if [ "${BACKUP_QDRANT:-0}" = "1" ] && docker volume inspect spark-qdrant-data >/dev/null 2>&1; then
  docker run --rm -v spark-qdrant-data:/data -v "$D":/backup alpine \
    tar czf /backup/qdrant.tgz -C /data . 2>/dev/null
fi
find /root/backup -maxdepth 1 -type d -mtime +14 -exec rm -rf {} \;   # ротация 14 дней
echo "backup done: $D"
EOS

cat > /usr/local/sbin/spark-llm-digest.sh <<'EOS'
#!/usr/bin/env bash
set -uo pipefail
OUT=/var/log/spark-llm/digest-$(date +%F).txt
{
  echo "=== Spark LLM digest $(date '+%F %T') ==="
  echo "-- verify:"; bash "__STACK__/scripts/05-verify.sh" 2>&1 | tail -4
  echo "-- движок:"; K=$(grep -oP '(?<=^VLLM_API_KEY=).*' /etc/spark-llm.env)
  curl -s http://127.0.0.1:8000/metrics -H "Authorization: Bearer $K" \
    | grep -E "^llamacpp:(tokens_predicted_total|kv_cache_usage_perc|requests_deferred)" | head -3
  echo "-- задачи за сутки:"
  docker exec spark-litellm-db psql -U orchestrator -d agents_tasks -tAc \
    "select status, count(*) from tasks where created_at > now()-interval '1 day' group by 1" 2>/dev/null
  echo "-- проблемные задачи:"
  docker exec spark-litellm-db psql -U orchestrator -d agents_tasks -tAc \
    "select id, template, result from tasks where status='failed' and created_at > now()-interval '1 day' limit 5" 2>/dev/null
  echo "-- spend за сутки (LiteLLM):"
  M=$(grep -oP '(?<=^LITELLM_MASTER_KEY=).*' /etc/spark-llm.env)
  curl -s http://127.0.0.1:4000/global/spend -H "Authorization: Bearer $M" | head -c 200; echo
  echo "-- корпус (rag-svc):"
  curl -s --max-time 10 http://127.0.0.1:8502/stats | jq -c \
    '{backend,chunks,docs,dense_resident,fragmentation,db_mb:(.db_bytes/1048576|floor),p95_ms:.query_latency_ms.p95,recommendation}' 2>/dev/null \
    || echo "  rag-svc не отвечает"
  echo "-- импорт корпуса (последние записи журнала):"
  tail -3 /srv/agents/rag-svc/bulk-journal.jsonl 2>/dev/null | jq -c '{file,status,chunks,error}' 2>/dev/null || echo "  журнала нет"
  echo "-- диски:"; df -h / /opt | tail -2
} > "$OUT" 2>&1
echo "digest: $OUT"
EOS
sed -i "s|__STACK__|$STACK|g" /usr/local/sbin/spark-llm-backup.sh /usr/local/sbin/spark-llm-digest.sh
chmod +x /usr/local/sbin/spark-llm-{backup,digest}.sh

cat > /etc/cron.d/spark-llm-ops <<'EOC'
# m h dom mon mon dow user cmd
30 3 * * * root /usr/local/sbin/spark-llm-backup.sh >> /var/log/spark-llm/backup.log 2>&1
30 6 * * * root /usr/local/sbin/spark-llm-digest.sh >> /var/log/spark-llm/digest.log 2>&1
EOC

cat > /etc/logrotate.d/spark-llm <<'EOL'
/var/log/spark-llm/*.log /var/log/spark-llm/digest-*.txt {
  weekly
  rotate 8
  compress
  missingok
  notifempty
}
EOL

echo "=== ручной прогон для проверки ==="
/usr/local/sbin/spark-llm-backup.sh
/usr/local/sbin/spark-llm-digest.sh && tail -25 /var/log/spark-llm/digest-$(date +%F).txt
