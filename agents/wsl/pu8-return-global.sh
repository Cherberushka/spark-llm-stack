#!/usr/bin/env bash
# PU8: fold personal experiments back into the GLOBAL network, then decommission personal instance
set -euo pipefail
source "$HOME/.spark-env"
ORCH="$HOME/personal-orchestrator"
STAMP=$(date +%F-%H%M)
mkdir -p "$HOME/pu-export-$STAMP"
docker exec pu-postgres pg_dump -U pu agents_tasks | gzip > "$HOME/pu-export-$STAMP/agents_tasks.sql.gz"
tar czf "$HOME/pu-export-$STAMP/artifacts.tgz" -C "$HOME/agents-artifacts" . 2>/dev/null || true
cp "$ORCH/orchestrator.log" "$HOME/pu-export-$STAMP/" 2>/dev/null || true
scp -r "$HOME/pu-export-$STAMP" "user@$SPARK_IP:/srv/agents/import/personal-$STAMP"
echo "✓ экспорт залит на Spark: /srv/agents/import/personal-$STAMP"
echo "  Promotion worker'а/роли в глобальный реестр — вручную админом:"
echo "    INSERT/UPDATE agents_registry ... (карточка роли по образцу GET /api/registry)"
echo "  Декомиссия персональной копии:"
"$ORCH/stop.sh" 2>/dev/null || true
docker rm -f pu-postgres >/dev/null && echo "  pu-postgres removed"
echo "  Персональный виртуальный ключ отзовите в LiteLLM UI (:8443) или оставьте для станции."
echo "PU8 done → возвращаемся к глобальной разметке: P2/P4 (TUNING/AGENT-NETWORK-PLAN)."
