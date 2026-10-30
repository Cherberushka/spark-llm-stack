#!/usr/bin/env bash
# =============================================================================
# 11-setup-orchestrator.sh — P1: каркас оркестратора агентной сети
# Run as root: sudo bash scripts/11-setup-orchestrator.sh
#
# Ставит: pandoc/ocrmypdf/tesseract (doc-линия), venv FastAPI+psycopg,
# БД agents_tasks в существующем postgres (litellm-db, loopback-публикация),
# systemd-юнит agents-orchestrator (:8500 loopback), маршрут Caddy /tasks/*
# с basic_auth, ключи в /etc/orchestrator.env, смоук-тест echo-задачей.
# Идемпотентен: повторный запуск безопасен.
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVF=/etc/orchestrator.env

echo "=== [1/9] Пакеты конвертации документов ==="
export DEBIAN_FRONTEND=noninteractive
apt-get install -y pandoc poppler-utils ocrmypdf tesseract-ocr tesseract-ocr-rus >/dev/null
echo ok

echo "=== [2/9] Каталоги агентной сети ==="
mkdir -p /srv/agents/{artifacts,kb-inbox,reports-out,repos}
chmod 775 /srv/agents/kb-inbox   # сотрудники смогут класть файлы через шару позже
echo ok

echo "=== [3/9] Python-окружение оркестратора ==="
if [[ ! -x /opt/orch-venv/bin/uvicorn ]]; then
  python3 -m venv /opt/orch-venv
  /opt/orch-venv/bin/pip install -q --upgrade pip
  /opt/orch-venv/bin/pip install -q fastapi "uvicorn[standard]" "psycopg[binary]" python-multipart
fi
echo ok

echo "=== [4/9] База задач в postgres (контейнер spark-litellm-db) ==="
# loopback-публикация postgres, чтобы хост-сервис оркестратора достучался:
if ! grep -q '"127.0.0.1:5432:5432"' "$REPO/stack/docker-compose.yml"; then
  sed -i 's|    volumes:\n      - litellm-db-data|&|' "$REPO/stack/docker-compose.yml"  # no-op guard
  python3 - "$REPO/stack/docker-compose.yml" <<'PY'
import sys
p=sys.argv[1]; s=open(p).read()
if '127.0.0.1:5432:5432' not in s:
    s=s.replace('''    volumes:
      - litellm-db-data:/var/lib/postgresql/data''','''    ports:
      - "127.0.0.1:5432:5432"   # loopback only: orchestrator on host
    volumes:
      - litellm-db-data:/var/lib/postgresql/data''')
    open(p,'w').write(s); print("compose: postgres loopback port added")
PY
  (cd "$REPO/stack" && docker compose up -d litellm-db)
fi
# postgres может стартовать после recreate — ретраи вместо гонки
for i in $(seq 1 15); do
  docker exec spark-litellm-db psql -U llmadmin -d postgres -tAc "select 1" >/dev/null 2>&1 && break
  sleep 2
done
touch "$ENVF"; chmod 600 "$ENVF"
grep -q '^ORCH_DB_PASS=' "$ENVF" || echo "ORCH_DB_PASS=$(openssl rand -hex 16)" >> "$ENVF"
DBPASS="$(grep -oP '(?<=^ORCH_DB_PASS=).*' "$ENVF")"
docker exec spark-litellm-db psql -U llmadmin -d postgres -tAc \
  "select 1 from pg_roles where rolname='orchestrator'" | grep -q 1 || \
  docker exec spark-litellm-db psql -U llmadmin -d postgres -c "create user orchestrator password '$DBPASS'"
docker exec spark-litellm-db psql -U llmadmin -d postgres -tAc \
  "select 1 from pg_database where datname='agents_tasks'" | grep -q 1 || \
  docker exec spark-litellm-db psql -U llmadmin -d postgres -c "create database agents_tasks owner orchestrator"
grep -q '^DATABASE_URL=' "$ENVF" || echo "DATABASE_URL=postgresql://orchestrator:$DBPASS@127.0.0.1:5432/agents_tasks" >> "$ENVF"
echo ok

echo "=== [5/9] Ключи: API оркестратора + LLM-ключ планировщика ==="
grep -q '^ORCH_API_KEY=' "$ENVF" || echo "ORCH_API_KEY=$(openssl rand -hex 24)" >> "$ENVF"
if ! grep -q '^ORCH_LLM_KEY=' "$ENVF"; then
  MASTER="$(grep -oP '(?<=^LITELLM_MASTER_KEY=).*' /etc/spark-llm.env)"
  K="$(curl -s -X POST http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $MASTER" \
       -H 'Content-Type: application/json' \
       -d '{"key_alias":"orchestrator-planner","models":["qwen3.8-27b"],"rpm":20,"tpm":400000}' | python3 -c 'import sys,json;print(json.load(sys.stdin).get("key",""))')"
  [[ -n "$K" ]] && echo "ORCH_LLM_KEY=$K" >> "$ENVF" || echo "ORCH_LLM_KEY=" >> "$ENVF"
fi
grep -q '^GATEWAY_URL=' "$ENVF" || echo "GATEWAY_URL=http://127.0.0.1:4000/v1" >> "$ENVF"
grep -q '^TASKS_UI_PASS=' "$ENVF" || echo "TASKS_UI_PASS=$(openssl rand -hex 8)" >> "$ENVF"
echo ok

echo "=== [6/9] Код оркестратора в /opt/orchestrator ==="
mkdir -p /opt/orchestrator/web
install -m 644 "$REPO/agents/orchestrator/orchestrator.py" /opt/orchestrator/orchestrator.py
install -m 644 "$REPO/agents/orchestrator/web/tasks-dashboard.html" /opt/orchestrator/web/tasks-dashboard.html
install -m 644 /dev/null /opt/orchestrator/web/.keep 2>/dev/null || true
cat > /etc/systemd/system/agents-orchestrator.service <<'EOF'
[Unit]
Description=Agent network orchestrator (tasks, planner, registry, gates)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/orchestrator.env
WorkingDirectory=/opt/orchestrator
ExecStart=/opt/orch-venv/bin/python -m uvicorn orchestrator:app --host 0.0.0.0 --port 8500
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable agents-orchestrator && systemctl restart agents-orchestrator
for i in $(seq 1 30); do
  curl -s -o /dev/null http://127.0.0.1:8500/api/templates && break; sleep 1
done
curl -s http://127.0.0.1:8500/api/templates | head -c 120; echo

echo "=== [7/9] ufw: docker-контейнеры → :8500 ==="
ufw allow from 172.16.0.0/12 to any port 8500 proto tcp comment 'orchestrator from caddy container' >/dev/null || true
echo ok

echo "=== [8/9] Caddy: /tasks/* с basic_auth ==="
cd "$REPO/stack"
if ! grep -q 'handle_path /tasks/\*' caddy/Caddyfile; then
  PASS="$(grep -oP '(?<=^TASKS_UI_PASS=).*' /etc/orchestrator.env)"
  HASH="$(docker compose exec -T caddy caddy hash-password --plaintext "$PASS")"
  python3 - "$HASH" <<'PY'
import sys
h=sys.argv[1]
p='caddy/Caddyfile'; s=open(p).read()
block = '''	handle_path /tasks/* {
		basic_auth {
			admin %s
		}
		reverse_proxy host.docker.internal:8500
	}

	handle {''' % h
s = s.replace('''	handle {''', block, 1)
open(p,'w').write(s); print("Caddyfile: /tasks route added")
PY
  docker compose restart caddy
fi
echo ok

echo "=== [9/9] Смоук: echo-задача полного цикла ==="
OKEY="$(grep -oP '(?<=^ORCH_API_KEY=).*' /etc/orchestrator.env)"
TID="$(curl -s -X POST http://127.0.0.1:8500/api/tasks -H "X-Orch-Key: $OKEY" -H 'Content-Type: application/json' \
      -d '{"template":"echo","payload":{"text":"p1-smoke"},"requester":"setup","group":"everyone"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 30); do
  ST="$(curl -s http://127.0.0.1:8500/api/tasks/$TID | python3 -c 'import sys,json;print(json.load(sys.stdin)["task"]["status"])')"
  [[ "$ST" == "done" || "$ST" == "failed" ]] && break; sleep 1
done
echo "task #$TID status: $ST"
PASS="$(grep -oP '(?<=^TASKS_UI_PASS=).*' /etc/orchestrator.env)"
CODE="$(curl -sk -o /dev/null -w '%{http_code}' -u "admin:$PASS" https://127.0.0.1/tasks/)"
echo "dashboard https://<host>/tasks/ -> $CODE (login admin / $PASS)  [ожидаем 200]"

cat <<EOF

────────────────────────────────────────────────────────────────
 P1 каркас поднят. Точки входа:
   Дашборд задач : https://<IP>/tasks/   (basic_auth: admin / TASKS_UI_PASS)
   API           : http://127.0.0.1:8500/api/...  (запись — заголовок X-Orch-Key)
   Ключи         : sudo cat /etc/orchestrator.env
   Реестр ролей  : GET /api/registry  (роли из AGENT-NETWORK-PLAN §7)
 Создать задачу  : curl -X POST .../api/tasks -H "X-Orch-Key: ..." -d '{"template":"doc-convert","payload":{"file":"имя.pdf"},"requester":"adm","group":"administration"}'
 Файлы для конвертации кладите в /srv/agents/kb-inbox/
 Следующий шаг   : P2 — doc-worker на RAG-Anything + Open WebUI Tool create_task
────────────────────────────────────────────────────────────────
EOF
