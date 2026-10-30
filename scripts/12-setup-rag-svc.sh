#!/usr/bin/env bash
# =============================================================================
# 12-setup-rag-svc.sh — P2: corpus search service (search_corpus/run_pipeline pair)
# Run as root: sudo bash scripts/12-setup-rag-svc.sh
# Хранилище v2: векторы одним непрерывным vectors.f32, текст+метаданные в SQLite,
# BM25 в FTS5, гибридный поиск (RRF), фильтры по specialty/doctype/current.
# arm64-safe (numpy + sqlite, без torch). Замеры и пороги перехода на Qdrant: RAG-SCALE-PLAN.md
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYBIN=/opt/rag-venv/bin/python

echo "=== [1/4] venv + зависимости ==="
if [[ ! -x /opt/rag-venv/bin/uvicorn ]]; then
  python3 -m venv /opt/rag-venv
  /opt/rag-venv/bin/pip install -q --upgrade pip
  /opt/rag-venv/bin/pip install -q fastapi "uvicorn[standard]" numpy
fi
# httpx нужен только для автономной самопроверки (TestClient); qdrant-client — для
# бэкенда Qdrant, ставится отдельно в scripts/18-vector-store-qdrant.sh
"$PYBIN" -c "import httpx" 2>/dev/null || "$PYBIN" -m pip install -q httpx
mkdir -p /srv/agents/rag-svc /srv/agents/rag-svc/tmp /srv/kb/clinrec
[[ -f /srv/agents/glossary.md ]] || install -m 644 "$REPO/agents/orchestrator/glossary.example.md" /srv/agents/glossary.md
install -m 644 "$REPO/agents/orchestrator/rag-svc.py"        /opt/orchestrator/rag-svc.py
install -m 644 "$REPO/agents/orchestrator/rag_migrate_v1.py" /opt/orchestrator/rag_migrate_v1.py
install -m 644 "$REPO/agents/orchestrator/rag_bulk_ingest.py" /opt/orchestrator/rag_bulk_ingest.py

echo "=== [2/4] env + systemd ==="
ENVF=/etc/orchestrator.env
grep -q '^RAG_SVC_URL=' "$ENVF" || echo 'RAG_SVC_URL=http://127.0.0.1:8502' >> "$ENVF"
# --- параметры хранилища v2 (значения по умолчанию обоснованы в RAG-SCALE-PLAN.md) ---
grep -q '^RAG_SVC_DIR='      "$ENVF" || echo 'RAG_SVC_DIR=/srv/agents/rag-svc'   >> "$ENVF"
grep -q '^RAG_BACKEND='      "$ENVF" || echo 'RAG_BACKEND=sqlite'                >> "$ENVF"
grep -q '^RAG_QUERY_MODE='   "$ENVF" || echo 'RAG_QUERY_MODE=hybrid'             >> "$ENVF"
grep -q '^RAG_DIM='          "$ENVF" || echo 'RAG_DIM=1024'                      >> "$ENVF"
grep -q '^RAG_CURRENT_ONLY=' "$ENVF" || echo 'RAG_CURRENT_ONLY=1'                >> "$ENVF"
grep -q '^RAG_DENSE_RESIDENT_MB=' "$ENVF" || echo 'RAG_DENSE_RESIDENT_MB=4096'   >> "$ENVF"
# порог, при котором /stats начнёт рекомендовать Qdrant (scripts/18-...):
grep -q '^RAG_T_CHUNKS_QDRANT=' "$ENVF" || echo 'RAG_T_CHUNKS_QDRANT=200000'     >> "$ENVF"
grep -q '^RAG_T_LATENCY_MS='    "$ENVF" || echo 'RAG_T_LATENCY_MS=500'           >> "$ENVF"
cat > /etc/systemd/system/rag-svc.service <<'EOS'
[Unit]
Description=Corpus search service (bge-m3 embeddings via gateway)
After=network-online.target agents-orchestrator.service

[Service]
Type=simple
EnvironmentFile=/etc/orchestrator.env
WorkingDirectory=/opt/orchestrator
ExecStart=/opt/rag-venv/bin/python -m uvicorn rag-svc:app --host 127.0.0.1 --port 8502
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOS
systemctl daemon-reload && systemctl enable rag-svc && systemctl restart rag-svc
# оркестратор тоже перечитает RAG_SVC_URL:
systemctl restart agents-orchestrator

echo "=== [3/4] health + самопроверка ==="
for i in $(seq 1 20); do curl -s http://127.0.0.1:8502/health && break; sleep 1; done; echo
curl -s http://127.0.0.1:8502/stats | jq -c '{backend,chunks,docs,recommendation}' || true
# автономный тест сервиса (без Ollama/GPU): ловит регрессии слотов, фильтров, гибрида
if [[ -f "$REPO/research/test-rag-svc-v2.py" ]]; then
  "$PYBIN" "$REPO/research/test-rag-svc-v2.py" --backend sqlite >/tmp/rag-selftest.log 2>&1 \
    && echo "✓ самопроверка rag-svc пройдена (/tmp/rag-selftest.log)" \
    || { echo "✗ самопроверка НЕ пройдена — последние строки:"; tail -12 /tmp/rag-selftest.log; }
fi

echo "=== [4/4] Ключ оркестратора + смоук (JSON строим через jq) ==="
# rag-svc шлёт эмбеддинги ключом ORCH_LLM_KEY — у него должно быть право на bge-m3:
MASTER="$(grep -oP '(?<=^LITELLM_MASTER_KEY=).*' /etc/spark-llm.env 2>/dev/null || true)"
OLL="$(grep -oP '(?<=^ORCH_LLM_KEY=).*' "$ENVF" 2>/dev/null || true)"
if [[ -n "$MASTER" && -n "$OLL" ]]; then
  curl -s -X POST http://127.0.0.1:4000/key/update -H "Authorization: Bearer $MASTER" \
    -H 'Content-Type: application/json' \
    -d "{\"key\":\"$OLL\",\"models\":[\"qwen3.8-27b\",\"bge-m3\"]}" >/dev/null \
    && echo "ключ оркестратора: добавлена модель bge-m3"
fi
printf 'Артериальная гипертензия. Первая линия — ингибиторы АПФ.\n\nХОБЛ: спирометрия ежегодно.\n' > /tmp/rag-test.md
jq -Rs '{name:"smoke-doc", text:.}' /tmp/rag-test.md \
  | curl -s -X POST http://127.0.0.1:8502/index -H 'Content-Type: application/json' -d @-
echo
curl -s -X POST http://127.0.0.1:8502/query -H 'Content-Type: application/json' \
  -d '{"q":"первая линия терапии при гипертензии?","k":2}' | head -c 300; echo
echo "P2 rag-svc ready. Open WebUI Tool: agents/orchestrator/openwebui-tool-tasks.py"
