#!/usr/bin/env bash
# =============================================================================
# 17-rag-v2-migrate.sh — миграция корпуса rag-svc v1 -> v2 БЕЗ пере-эмбеддинга
#
# v1: векторы лежали BLOB'ом внутри каждой строки SQLite, и каждый /query вычитывал
#     всю таблицу и пересобирал матрицу. Замер на 50k чанков: 0.99 с/запрос,
#     536 МБ на один запрос, 98% времени — однопоточный Python (20 ядер не помогают).
# v2: векторы одним непрерывным vectors.f32, текст+метаданные в SQLite, BM25 в FTS5,
#     фильтры по specialty/doctype/current. Тот же корпус: 0.012 с dense / 0.025 с hybrid.
#
# Эмбеддинги переносятся как есть -> миграция занимает минуты.
# Подробности и все замеры: RAG-SCALE-PLAN.md
#
# Run as root: sudo bash scripts/17-rag-v2-migrate.sh [--dry-run] [--rollback]
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAG_DIR="${RAG_SVC_DIR:-/srv/agents/rag-svc}"
DB="$RAG_DIR/index.db"
PYBIN=/opt/rag-venv/bin/python
MODE="apply"; [[ "${1:-}" == "--dry-run" ]] && MODE="dry"
[[ "${1:-}" == "--rollback" ]] && MODE="rollback"

[[ -x $PYBIN ]] || { echo "✗ нет $PYBIN — сначала sudo bash scripts/12-setup-rag-svc.sh"; exit 1; }
# Бэкап кода v1 — ДО перезаписи, иначе --rollback вернул бы v2-код на v1-базу
# (сервис v2 такую базу открыть отказывается, это защита).
if [[ -f /opt/orchestrator/rag-svc.py && ! -f /opt/orchestrator/rag-svc.py.v1 ]]; then
  if grep -q "emb blob\|frombuffer" /opt/orchestrator/rag-svc.py 2>/dev/null; then
    cp -a /opt/orchestrator/rag-svc.py /opt/orchestrator/rag-svc.py.v1
    echo "сохранён код v1: /opt/orchestrator/rag-svc.py.v1"
  fi
fi
# свежий код сервиса и мигратор — из репозитория (не из /opt, чтобы не зависеть от порядка)
install -m 644 "$REPO/agents/orchestrator/rag-svc.py"        /opt/orchestrator/rag-svc.py
install -m 644 "$REPO/agents/orchestrator/rag_migrate_v1.py" /opt/orchestrator/rag_migrate_v1.py
chmod 644 /opt/orchestrator/rag-svc.py /opt/orchestrator/rag_migrate_v1.py

if [[ $MODE == rollback ]]; then
  echo "=== откат к v1 ==="
  BAK="$(ls -1t "$DB".v1-*.bak 2>/dev/null | head -1 || true)"
  [[ -n "$BAK" ]] || { echo "✗ бэкап v1 не найден ($DB.v1-*.bak) — откат невозможен"; exit 1; }
  systemctl stop rag-svc
  rm -f "$RAG_DIR/vectors.f32"
  install -m 644 "$BAK" "$DB"
  # v1-сервис берётся из бэкапа установщика; если его нет — остаётся v2, он v1-базу не откроет
  if [[ -f /opt/orchestrator/rag-svc.py.v1 ]]; then
    install -m 644 /opt/orchestrator/rag-svc.py.v1 /opt/orchestrator/rag-svc.py
  else
    echo "! копия v1-сервиса не найдена: /opt/orchestrator/rag-svc.py.v1"
    echo "  сервис v2 откажется открывать базу v1 (это защита). Возьмите rag-svc.py v1"
    echo "  из установщика той версии, что была до миграции, и положите в /opt/orchestrator/."
  fi
  systemctl start rag-svc && sleep 3
  curl -s http://127.0.0.1:8502/health | jq -c . || true
  exit 0
fi

echo "=== [1/5] текущее состояние ==="
if [[ ! -f $DB ]]; then
  echo "нет $DB — мигрировать нечего. Новый корпус сразу создаётся в схеме v2:"
  echo "  sudo bash scripts/12-setup-rag-svc.sh"
  exit 0
fi
db_cols() { /usr/bin/python3 - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
t = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
print(" ".join(r[1] for r in c.execute("pragma table_info(chunks)")) if "chunks" in t else "")
PY
}
db_counts() { /usr/bin/python3 - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
n, d = c.execute("select count(*), count(distinct doc) from chunks").fetchone()
print(f"в v1-базе: {n} чанков, {d} документов")
PY
}
db_dim() { /usr/bin/python3 - "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
r = c.execute("select length(emb)/4 from chunks where emb is not null limit 1").fetchone()
print(r[0] if r and r[0] else "")
PY
}

COLS="$(db_cols "$DB")"
echo "колонки chunks: ${COLS:-<нет таблицы chunks>}"
if ! grep -qw emb <<<"$COLS"; then
  echo "✓ схема уже v2 (нет колонки emb) — миграция не нужна"
  systemctl is-active --quiet rag-svc && curl -s http://127.0.0.1:8502/stats \
    | jq -c '{backend,chunks,docs,recommendation}' || true
  exit 0
fi
db_counts "$DB"
DIM="$(db_dim "$DB")"
echo "dim эмбеддинга: ${DIM:-не определён}"
[[ -n $DIM ]] || { echo "✗ в базе нет эмбеддингов — мигрировать нечего"; exit 1; }

echo "=== [2/5] прогноз (dry-run, ничего не меняет) ==="
STAGE="$RAG_DIR/v2-stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
RAG_DENSE_RESIDENT_MB=0 "$PYBIN" /opt/orchestrator/rag_migrate_v1.py \
  --old "$DB" --new-dir "$STAGE" --module /opt/orchestrator/rag-svc.py --dim "$DIM"
echo "dry-run пройден: v1-база читается, векторы совпадают при контрольном поиске"
[[ $MODE == dry ]] && { rm -rf "$STAGE"; echo "(--dry-run: изменения не применялись)"; exit 0; }

echo "=== [3/5] миграция в v2 ==="
systemctl stop rag-svc || true
RAG_DENSE_RESIDENT_MB=0 "$PYBIN" /opt/orchestrator/rag_migrate_v1.py \
  --old "$DB" --new-dir "$STAGE" --module /opt/orchestrator/rag-svc.py --dim "$DIM" --commit

echo "=== [4/5] подмена файлов с бэкапом ==="
STAMP="$(date +%Y%m%d-%H%M%S)"
cp -a "$DB" "$DB.v1-$STAMP.bak"
echo "бэкап v1: $DB.v1-$STAMP.bak"
install -m 644 "$STAGE/index.db"     "$DB"
install -m 644 "$STAGE/vectors.f32"  "$RAG_DIR/vectors.f32"
rm -rf "$STAGE"
chown -R "$(stat -c '%u:%g' "$RAG_DIR")" "$DB" "$RAG_DIR/vectors.f32" 2>/dev/null || true
ls -lh "$DB" "$RAG_DIR/vectors.f32" | awk '{print "  "$5"\t"$9}'

echo "=== [5/5] env, запуск, проверка ==="
ENVF=/etc/orchestrator.env
grep -q '^RAG_SVC_DIR='    "$ENVF" || echo "RAG_SVC_DIR=$RAG_DIR"          >> "$ENVF"
grep -q '^RAG_BACKEND='    "$ENVF" || echo 'RAG_BACKEND=sqlite'            >> "$ENVF"
grep -q '^RAG_QUERY_MODE=' "$ENVF" || echo 'RAG_QUERY_MODE=hybrid'         >> "$ENVF"
grep -q '^RAG_DIM='        "$ENVF" || echo "RAG_DIM=$DIM"                  >> "$ENVF"
grep -q '^RAG_CURRENT_ONLY=' "$ENVF" || echo 'RAG_CURRENT_ONLY=1'          >> "$ENVF"
systemctl daemon-reload
systemctl restart rag-svc
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:8502/health >/dev/null && break; sleep 1; done
curl -s http://127.0.0.1:8502/health | jq -c . || { echo "✗ сервис не поднялся:"; journalctl -u rag-svc -n 30 --no-pager; exit 1; }
curl -s http://127.0.0.1:8502/stats  | jq -c '{backend,chunks,docs,dim,dense_resident,recommendation}'

echo
echo "✓ миграция v1 -> v2 завершена."
echo "  самопроверка:  $PYBIN $REPO/research/test-rag-svc-v2.py --backend sqlite"
echo "  откат:         sudo bash $0 --rollback"
echo "  дальше:        массовый импорт КР — sudo bash scripts/19-import-clinrec.sh --dry-run"
