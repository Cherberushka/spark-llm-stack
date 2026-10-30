#!/usr/bin/env bash
# 21-export-dsh-skills.sh — экспорт скиллов проекта в формат DeepSeek Harness (dsh).
#
# Что уходит в экспорт: 16 курируемых скиллов из vendor-skills/ (те же, что
# ставит scripts/10 для Pi), 4 роль-карточки кодовой линии P4 из
# /etc/agents-roles (в dsh — скиллы-процедуры, не субагенты), процесс-скилл
# spark-code-line и стиль caveman. Frontmatter приводится к требованиям dsh
# (name kebab-case 1-64, description 1-1024), тела не правятся.
#
# Запуск (на Spark или в копии проекта, нужен только python3):
#   bash scripts/21-export-dsh-skills.sh                 # всё → ~/dsh-skills-export + zip
#   SET=code bash scripts/21-export-dsh-skills.sh        # только кодовый набор
#   ONLY=unlazy,mp-tdd bash scripts/21-export-dsh-skills.sh
#   NO_ZIP=1 bash scripts/21-export-dsh-skills.sh
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${OUT:-$HOME/dsh-skills-export}"
ROLES_DIR="${ROLES_DIR:-/etc/agents-roles}"
ARGS=(--out "$OUT" --roles-dir "$ROLES_DIR" --set "${SET:-all}")
[[ -n "${ONLY:-}" ]] && ARGS+=(--only "$ONLY")
[[ "${NO_ZIP:-0}" == "1" ]] && ARGS+=(--no-zip)

echo "=== Экспорт скиллов в dsh: набор ${SET:-all} → $OUT ==="
[[ -d "$ROLES_DIR" ]] || echo "  ! $ROLES_DIR нет — роль-карточки возьмутся из scripts/16"
python3 agents/dsh/export-dsh-skills.py "${ARGS[@]}"

cat <<EOF

Дальше (машина, где запущен dsh):
  1) перенести каталог или zip (sha256 напечатан выше);
  2) bash install.sh                       # → ~/.agents/skills (ранг 500)
     bash install.sh --project /path/repo  # → <repo>/.dsh/skills (ранг 100)
     Windows: .\\install.ps1 [-Project C:\\work\\repo]
  3) в сессии dsh проверить вызов: /unlazy, /mp-code-review, /spark-role-qa
Подробности и состав: MANIFEST.md внутри экспорта;
agents/dsh/DEEPSEEK-HARNESS-GUIDE.md §7 — перенос в dsh и грабли.
EOF
