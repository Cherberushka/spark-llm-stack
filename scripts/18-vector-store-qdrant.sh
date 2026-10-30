#!/usr/bin/env bash
# =============================================================================
# 18-vector-store-qdrant.sh — включить Qdrant как поисковый движок rag-svc
#
# КОГДА ЭТО НУЖНО:
#   РЕШЕНИЕ ЗАКАЗЧИКА (28.10.2026): переход на Qdrant выполняется ДО массового
#   импорта всех клинических рекомендаций — чтобы весь корпус (~105k фрагментов,
#   рост к S2 ≈ 195k) сразу писался в целевой движок. Порядок: 17 → ЭТОТ СКРИПТ → 19.
#   Для справки, измеримые пороги (RAG-SCALE-PLAN.md §4.2), после перехода они
#   остаются контрольными точками /stats:
#   * chunks >= 200000  (RAG_T_CHUNKS_QDRANT), ИЛИ
#   * p95 латентность /query > 500 мс, ИЛИ
#   * корпус пишется из нескольких мест одновременно.
#   Замеры: все 777 действующих КР (~105 000 чанков) sqlite-бэкенд v2 тоже держит
#   (98 мс на гибридный запрос, 521 МБ RAM) — переход выполнен на вырост, а не
#   потому, что sqlite «не тянет» S1.
#
# Qdrant здесь — НЕ замена хранилища, а пересобираемый индекс: SQLite остаётся
# источником правды по текстам и метаданным, поэтому откат = одна строка env.
#
# Run as root:
#   sudo bash scripts/18-vector-store-qdrant.sh              # установить и переключить
#   sudo bash scripts/18-vector-store-qdrant.sh --sync       # только наполнить из SQLite
#   sudo bash scripts/18-vector-store-qdrant.sh --status
#   sudo bash scripts/18-vector-store-qdrant.sh --rollback   # вернуть RAG_BACKEND=sqlite
#   sudo bash scripts/18-vector-store-qdrant.sh --remove     # остановить и удалить контейнер
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
QDRANT_IMAGE="${QDRANT_IMAGE:-qdrant/qdrant:v1.20}"
CTR=spark-qdrant
VOL=spark-qdrant-data
ENVF=/etc/orchestrator.env
PYBIN=/opt/rag-venv/bin/python
ACTION="${1:---install}"

set_env() { # key value — идемпотентная правка /etc/orchestrator.env
  local k="$1" v="$2"
  [[ -f $ENVF ]] || touch "$ENVF"
  if grep -q "^${k}=" "$ENVF"; then sed -i "s|^${k}=.*|${k}=${v}|" "$ENVF"; else echo "${k}=${v}" >> "$ENVF"; fi
}

status() {
  echo "=== контейнер ==="
  docker ps -a --filter "name=$CTR" --format '  {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}' || true
  echo "=== env ==="
  grep -E '^(RAG_BACKEND|QDRANT_URL|QDRANT_COLLECTION|RAG_DIM)=' "$ENVF" 2>/dev/null | sed 's/^/  /' || echo "  (нет записей)"
  echo "=== Qdrant ==="
  curl -s --max-time 5 http://127.0.0.1:6333/collections | jq -c . 2>/dev/null || echo "  недоступен"
  echo "=== rag-svc ==="
  curl -s --max-time 5 http://127.0.0.1:8502/stats \
    | jq -c '{backend,chunks,docs,points_indexed,status,recommendation}' 2>/dev/null || echo "  недоступен"
}

case "$ACTION" in
  --status) status; exit 0 ;;
  --rollback)
    echo "=== откат на sqlite-бэкенд ==="
    set_env RAG_BACKEND sqlite
    systemctl restart rag-svc && sleep 3
    curl -s http://127.0.0.1:8502/health | jq -c .
    echo "✓ поисковый движок снова sqlite. Контейнер Qdrant оставлен (не мешает):"
    echo "  удалить: sudo bash $0 --remove"
    exit 0 ;;
  --remove)
    systemctl stop rag-svc || true
    set_env RAG_BACKEND sqlite
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    systemctl start rag-svc || true
    echo "✓ контейнер удалён, бэкенд = sqlite"
    echo "  том с данными остался: docker volume rm $VOL   (удалять только осознанно)"
    exit 0 ;;
  --sync) ;;
  --install) ;;
  *) echo "неизвестное действие: $ACTION"; exit 2 ;;
esac

if [[ $ACTION == --install ]]; then
  echo "=== [1/6] образ (Docker Hub, NGC не нужен) ==="
  docker pull "$QDRANT_IMAGE"
  ARCH="$(docker image inspect "$QDRANT_IMAGE" --format '{{.Architecture}}' 2>/dev/null || echo unknown)"
  echo "архитектура образа: $ARCH"
  if [[ $ARCH != arm64 && $ARCH != aarch64 ]]; then
    echo "✗ образ не arm64 — на DGX Spark (aarch64) он не запустится нативно."
    echo "  Доступные теги: https://hub.docker.com/r/qdrant/qdrant/tags"
    echo "  Можно указать другой: QDRANT_IMAGE=qdrant/qdrant:vX.Y sudo -E bash $0"
    exit 1
  fi

  echo "=== [2/6] контейнер: только loopback, свой том ==="
  docker volume create "$VOL" >/dev/null
  if docker ps -a --format '{{.Names}}' | grep -qx "$CTR"; then
    docker rm -f "$CTR" >/dev/null
  fi
  # 127.0.0.1:6333 — наружу не публикуем: Qdrant нужен только rag-svc на этом же хосте.
  # Поэтому правило DOCKER-USER не требуется (в отличие от 4000/8000).
  docker run -d --name "$CTR" --restart unless-stopped \
    -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
    -v "$VOL":/qdrant/storage \
    -e QDRANT__SERVICE__ENABLE_CORS=false \
    -e QDRANT__STORAGE__ON_DISK_PAYLOAD=true \
    -e QDRANT__STORAGE__MEMMAP_THRESHOLD_KB=65536 \
    "$QDRANT_IMAGE" >/dev/null
  for _ in $(seq 1 30); do curl -sf http://127.0.0.1:6333/healthz >/dev/null && break; sleep 1; done
  curl -s http://127.0.0.1:6333/ | jq -c '{title,version}' || { echo "✗ Qdrant не поднялся:"; docker logs --tail 30 "$CTR"; exit 1; }

  echo "=== [3/6] клиент в venv rag-svc ==="
  [[ -x $PYBIN ]] || { echo "✗ нет $PYBIN — сначала sudo bash scripts/12-setup-rag-svc.sh"; exit 1; }
  "$PYBIN" -c "import qdrant_client" 2>/dev/null || "$PYBIN" -m pip install -q "qdrant-client>=1.20,<2"
  "$PYBIN" -c "import importlib.metadata as m; print('  qdrant-client', m.version('qdrant-client'))"

  echo "=== [4/6] переключаем бэкенд ==="
  set_env RAG_BACKEND qdrant
  set_env QDRANT_URL http://127.0.0.1:6333
  set_env QDRANT_COLLECTION rag_chunks
  grep -q '^RAG_DIM=' "$ENVF" || set_env RAG_DIM 1024
  systemctl restart rag-svc
  for _ in $(seq 1 30); do curl -sf http://127.0.0.1:8502/health >/dev/null && break; sleep 1; done
  curl -s http://127.0.0.1:8502/health | jq -c . || { echo "✗ rag-svc не поднялся:"; journalctl -u rag-svc -n 30 --no-pager; exit 1; }
fi

echo "=== [5/6] наполняем Qdrant из SQLite БЕЗ пере-эмбеддинга ==="
CHUNKS_SQLITE="$(curl -s http://127.0.0.1:8502/stats | jq -r '.chunks // 0')"
echo "в SQLite-зеркале чанков: $CHUNKS_SQLITE"
if [[ "$CHUNKS_SQLITE" -gt 0 ]]; then
  echo "перенос готовых векторов (минуты, GPU не нагружается)..."
  time curl -s -X POST http://127.0.0.1:8502/reindex \
    -H 'Content-Type: application/json' -d '{"use_existing_vectors": true}' | jq -c .
else
  echo "корпус пуст — наполнять нечего (импорт: scripts/19-import-clinrec.sh)"
fi

echo "=== [6/6] проверка ==="
curl -s http://127.0.0.1:6333/collections/rag_chunks | jq -c '{points: .result.points_count, indexed: .result.indexed_vectors_count, status: .result.status}'
curl -s -X POST http://127.0.0.1:8502/query -H 'Content-Type: application/json' \
  -d '{"q":"первая линия терапии при артериальной гипертензии","k":2}' | jq -c '{n: (.results|length), top: .results[0].doc, search}'
status

echo
echo "✓ Qdrant включён как поисковый движок. SQLite по-прежнему источник правды."
echo "  откат одной строкой:  sudo bash $0 --rollback"
echo "  что изменилось для пользователей: ничего, контракт /query тот же."
echo "  резервная копия: том $VOL + /srv/agents/rag-svc — проверьте, что"
echo "  /usr/local/sbin/spark-llm-backup.sh их берёт (scripts/14-ops-cron.sh)."
