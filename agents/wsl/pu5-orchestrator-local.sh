#!/usr/bin/env bash
# PU5: PERSONAL orchestrator instance in WSL (own DB, own inbox; models via central gateway)
set -euo pipefail
source "$HOME/.spark-env"
command -v docker >/dev/null 2>&1 || { echo "✗ нужен Docker в WSL (Docker Desktop w/ WSL2 backend или docker-ce внутри WSL)"; exit 1; }
docker run -d --name pu-postgres -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=pu -e POSTGRES_PASSWORD=pu -e POSTGRES_DB=postgres postgres:16-alpine >/dev/null
sleep 3
docker exec pu-postgres psql -U pu -d postgres -tAc "select 1 from pg_database where datname='agents_tasks'" | grep -q 1 \
  || docker exec pu-postgres psql -U pu -d postgres -c "create database agents_tasks owner pu"
ORCH="$HOME/personal-orchestrator"; mkdir -p "$ORCH/web" "$HOME/agents-inbox" "$HOME/agents-artifacts"
SCP_SRC="user@$SPARK_IP:/opt/orchestrator"
scp "$SCP_SRC/orchestrator.py" "$ORCH/" 2>/dev/null || cp "$(dirname "$0")/../orchestrator/orchestrator.py" "$ORCH/"
scp "$SCP_SRC/web/tasks-dashboard.html" "$ORCH/web/" 2>/dev/null || cp "$(dirname "$0")/../orchestrator/web/tasks-dashboard.html" "$ORCH/web/"
python3 -m venv "$ORCH/venv"
"$ORCH/venv/bin/pip" install -q fastapi "uvicorn[standard]" "psycopg[binary]"
umask 077; cat > "$ORCH/env" <<E
DATABASE_URL=postgresql://pu:pu@127.0.0.1:5432/agents_tasks
ORCH_API_KEY=$(openssl rand -hex 24)
ORCH_LLM_KEY=$SPARK_LLM_KEY
GATEWAY_URL=$GATEWAY_URL
AGENTS_ART_DIR=$HOME/agents-artifacts
AGENTS_INBOX_DIR=$HOME/agents-inbox
E
cat > "$ORCH/start.sh" <<'E'
#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a; source ./env; set +a
nohup ./venv/bin/python -m uvicorn orchestrator:app --host 127.0.0.1 --port 8501 >> orchestrator.log 2>&1 &
echo $! > orchestrator.pid; echo "started pid $(cat orchestrator.pid), log: orchestrator.log"
E
cat > "$ORCH/stop.sh" <<'E'
#!/usr/bin/env bash
cd "$(dirname "$0")"; [[ -f orchestrator.pid ]] && kill "$(cat orchestrator.pid)" && rm orchestrator.pid && echo stopped
E
chmod +x "$ORCH/start.sh" "$ORCH/stop.sh"
"$ORCH/start.sh"
sleep 4; curl -s http://127.0.0.1:8501/api/templates | head -c 80; echo
echo "✓ personal orchestrator :8501 (dashboard: http://127.0.0.1:8501/ , key: ORCH_API_KEY из $ORCH/env)"
echo "PU5 done. Next: bash pu6-workers-local.sh"
