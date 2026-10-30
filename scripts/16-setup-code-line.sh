#!/usr/bin/env bash
# =============================================================================
# 16-setup-code-line.sh — P4: кодинг-линия агентной сети (Pi headless с ролями)
# Run as root: sudo bash scripts/16-setup-code-line.sh
#
# Изоляция: код исполняет unix-пользователь agent-dev (без sudo, доступ только
# к /srv/agents/repos и своему home). Роли = промпт-карточки /etc/agents-roles/.
# Ключ шлюза у воркера свой: CODE_WORKER_KEY (alias code-worker, rpm/tpm-лимиты).
# Идемпотентен: повторный запуск безопасен.
# =============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0"; exit 1; }
ENVF=/etc/orchestrator.env

echo "=== [1/7] Пользователь agent-dev и каталоги ==="
id -u agent-dev >/dev/null 2>&1 || useradd -m -s /bin/bash agent-dev
mkdir -p /srv/agents/repos /etc/agents-roles
chown -R agent-dev:agent-dev /srv/agents/repos

echo "=== [2/7] Pi для agent-dev + models.json + ключ воркера ==="
if ! grep -q '^CODE_WORKER_KEY=' "$ENVF"; then
  MASTER="$(grep -oP '(?<=^LITELLM_MASTER_KEY=).*' /etc/spark-llm.env)"
  K="$(curl -s -X POST http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $MASTER" \
      -H 'Content-Type: application/json' \
      -d '{"key_alias":"code-worker","models":["qwen3.8-27b"],"rpm":10,"tpm":400000}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("key",""))')"
  [[ -n "$K" ]] || { echo "✗ LiteLLM не выдал ключ code-worker (шлюз :4000 недоступен или мастер-ключ неверен)."; echo "  проверьте: curl -s http://127.0.0.1:4000/health/liveliness"; exit 1; }
  echo "CODE_WORKER_KEY=$K" >> "$ENVF"
fi
# Pi ставится ТОЛЬКО если бинаря реально нет (проверка по путям, как в шагах 4/7 и 6/7).
# `command -v pi` в неlogin-шелле здесь врёт: install.sh кладёт бинарь в каталог,
# который попадает в PATH только через ~/.bashrc / ~/.profile → скрипт лез в сеть
# за `curl | sh` на каждом запуске (и тянул latest = дрейф версии).
PI_EXIST=""
for c in /usr/local/bin/pi /usr/bin/pi \
         /home/agent-dev/.pi/agent/bin/pi /home/agent-dev/.local/bin/pi \
         /home/agent-dev/bin/pi \
         /home/agent-dev/.pi/agent/install/releases/*/bin/pi; do
  [[ -x "$c" ]] && { PI_EXIST="$c"; break; }
done
[[ -n "$PI_EXIST" ]] || PI_EXIST="$(sudo -u agent-dev bash -lc 'command -v pi' 2>/dev/null || true)"
if [[ -n "$PI_EXIST" ]]; then
  echo "  pi уже установлен: $PI_EXIST (переустановки нет)"
elif [[ "${OFFLINE_PI:-0}" == "1" ]] || ! curl -fsS -m 6 -o /dev/null https://pi.dev/install.sh; then
  echo "✗ pi для agent-dev не установлен и сети до pi.dev нет."
  echo "  Варианты: OFFLINE_PI=1 bash $0 (пропустить шаг 2 и продолжить) или"
  echo "  вручную на машине с сетью: npm pack @earendil-works/pi-coding-agent, перенести и"
  echo "  sudo -u agent-dev npm install -g --ignore-scripts ./earendil-works-pi-coding-agent-*.tgz"
  [[ "${OFFLINE_PI:-0}" == "1" ]] || exit 1
else
  echo "  pi не найден — ставлю один раз (curl -fsSL https://pi.dev/install.sh | sh)"
  sudo -u agent-dev bash -c 'curl -fsSL https://pi.dev/install.sh | sh'
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYFILE=/home/agent-dev/.llm_key
grep -oP '(?<=^CODE_WORKER_KEY=).*' "$ENVF" | tr -d '\r\n' > "$KEYFILE"
[[ -s "$KEYFILE" ]] || { echo "✗ CODE_WORKER_KEY в $ENVF пуст — модели у Pi не появятся (Pi скрывает провайдер без ключа)."; exit 1; }
chmod 600 "$KEYFILE"; chown agent-dev:agent-dev "$KEYFILE"
mkdir -p /home/agent-dev/.pi/agent
# Headless-шаблон: ключ берётся командой "!cat $KEYFILE", а не из $SPARK_LLM_KEY.
# С "$SPARK_LLM_KEY" Pi 1.4.0 в сессии без этой переменной считает провайдер
# неавторизованным и печатает "Warning: No models available" (проверено на
# dist/core/provider-composer.js: composeApiKeyAuth.check → undefined).
sed -e "s|<SPARK_IP>|127.0.0.1|" -e "s|<KEY_FILE>|$KEYFILE|" \
  "$SCRIPT_DIR/../agents/pi/models.headless.json" > /home/agent-dev/.pi/agent/models.json
chmod 600 /home/agent-dev/.pi/agent/models.json
python3 -c 'import json,sys;json.load(open(sys.argv[1]))' /home/agent-dev/.pi/agent/models.json \
  || { echo "✗ models.json невалиден — Pi проигнорирует провайдер spark."; exit 1; }
chown -R agent-dev:agent-dev /home/agent-dev/.pi

echo "=== [2b/7] Скиллы Pi для agent-dev ==="
# Скиллы Pi читаются из ~/.pi/agent/skills ТОГО пользователя, под которым
# запущен Pi. Воркер agent-dev — отдельный пользователь, поэтому скиллы
# человека его не касаются. По умолчанию офлайн (vendor-skills из пакета).
PI_SKILLS_DIR=/home/agent-dev/.pi/agent/skills OFFLINE="${OFFLINE_SKILLS:-1}" \
  bash "$SCRIPT_DIR/10-install-pi-skills.sh" \
  || echo "  ! скиллы не установились — роли работают без них (не фатально)"
chown -R agent-dev:agent-dev /home/agent-dev/.pi 2>/dev/null || true
echo "  скиллов у agent-dev: $(find /home/agent-dev/.pi/agent/skills -maxdepth 2 -name SKILL.md 2>/dev/null | wc -l)"

echo "=== [3/7] Роль-промпты (правьте под свои стандарты кода) ==="
cat > /etc/agents-roles/implementer.md <<'ROLE_IMP'
Роль: implementer. Внеси МИНИМАЛЬНЫЕ правки по спецификации TASK. Не трогай несвязанный код.
Стиль и структуры — как в окружающем коде. После правок запусти сборку/линтер, если есть.
Выход: список изменённых файлов + суть изменений по каждому (1-2 строки).
Принципы Karpathy (обязательны):
1) Не принимай молчаливых допущений: неясность > 5 мин — остановись и спроси в выводе; покажи tradeoffs, если варианты равноправны.
2) Не усложняй: минимальная реализация без раздутых абстракций; мёртвый код не оставлять.
3) Не меняй код/комментарии, которые не понимаешь или которые ортогональны задаче.
4) Поверхность изменений честная: в выводе перечисли ВСЕ изменённые файлы, включая непреднамеренные правки.
ROLE_IMP
cat > /etc/agents-roles/qa.md <<'ROLE_QA'
Роль: qa. Прогони тесты проекта (команды ищи в README/CI/packaging). Если тестов на изменённый код нет — напиши минимальные.
Gate «как выглядит готово» (unlazy): тесты зелёные; граничные случаи покрыты; нет TODO/заглушек в diff; регрессий нет.
Выход: вердикт PASS/FAIL + список дефектов с файлами/строками. ЗАДАЧА НЕ ГОТОВА, пока FAIL.
Принципы Karpathy (обязательны):
1) Не принимай молчаливых допущений: неясность > 5 мин — остановись и спроси в выводе; покажи tradeoffs, если варианты равноправны.
2) Не усложняй: минимальная реализация без раздутых абстракций; мёртвый код не оставлять.
3) Не меняй код/комментарии, которые не понимаешь или которые ортогональны задаче.
4) Поверхность изменений честная: в выводе перечисли ВСЕ изменённые файлы, включая непреднамеренные правки.
ROLE_QA
cat > /etc/agents-roles/reviewer.md <<'ROLE_REV'
Роль: reviewer (read-only: НЕ меняй файлы). Прочитай git diff и логи qa.
Оцени: корректность, безопасность (инъекции/секреты/входные данные), стиль по проекту, достаточность тестов.
Выход: вердикт APPROVE или REQUEST_CHANGES + нумерованные замечания (файл:строка).
Принципы Karpathy (обязательны):
1) Не принимай молчаливых допущений: неясность > 5 мин — остановись и спроси в выводе; покажи tradeoffs, если варианты равноправны.
2) Не усложняй: минимальная реализация без раздутых абстракций; мёртвый код не оставлять.
3) Не меняй код/комментарии, которые не понимаешь или которые ортогональны задаче.
4) Поверхность изменений честная: в выводе перечисли ВСЕ изменённые файлы, включая непреднамеренные правки.
ROLE_REV
cat > /etc/agents-roles/security.md <<'ROLE_SEC'
# имя файла = роль в реестре оркестратора: security (не security-reviewer)
Роль: security-reviewer (read-only). Чек-лист: инъекции (SQL/команды/шаблоны), секреты в коде/логах,
небезопасные зависимости (известные CVE по именам пакетов), обработка входа/загрузок файлов, права/пути (path traversal).
Выход: находки с уровнем (crit/high/med/low) и рекомендацией; если пусто — «замечаний нет» + что проверено.
ROLE_SEC
chmod 755 /etc/agents-roles; chmod 644 /etc/agents-roles/*.md

echo "=== [4/7] Раннер Pi-ролей ==="
cat > /usr/local/sbin/agent-run-pi.sh <<'RUNNER_EOF'
#!/usr/bin/env bash
# usage: agent-run-pi.sh <role> <workdir> <taskfile> <outfile> [timeout_s]
set -uo pipefail
ROLE="$1"; WD="$2"; TF="$3"; OUT="$4"; TO="${5:-3600}"
KEY="$(cat /home/agent-dev/.llm_key 2>/dev/null)"
PROMPT="$(mktemp)"
{
  echo "== РОЛЬ/ПРАВИЛА =="
  cat "/etc/agents-roles/${ROLE}.md" 2>/dev/null || echo "(нет карточки роли)"
  echo
  echo "== ЗАДАЧА =="
  cat "$TF"
  echo
  echo "== КОНТЕКСТ =="
  echo "workdir: $WD; git: $(git -C "$WD" rev-parse --is-inside-work-tree 2>/dev/null || echo no)"
} > "$PROMPT"
cd "$WD" || exit 1
# pi ставится установщиком Pi в ~/.pi/agent/bin и в PATH попадает не всегда
# (особенно в неинтерактивной оболочке) — ищем бинарь явно и зовём по пути.
PI_BIN=""
for c in /usr/local/bin/pi /usr/bin/pi \
         /home/agent-dev/.pi/agent/bin/pi /home/agent-dev/.local/bin/pi \
         /home/agent-dev/bin/pi \
         /home/agent-dev/.pi/agent/install/releases/*/bin/pi; do
  [[ -x "$c" ]] && { PI_BIN="$c"; break; }
done
if [[ -z "$PI_BIN" ]]; then
  PI_BIN="$(sudo -u agent-dev bash -lc 'command -v pi' 2>/dev/null || true)"
fi
if [[ -z "$PI_BIN" ]]; then
  { echo "pi не найден: установите для agent-dev (scripts/16, шаг 2)"; echo "pi exit=127"; } > "$OUT"
  rm -f "$PROMPT"; exit 127
fi
if [[ ! -s /home/agent-dev/.llm_key ]]; then
  { echo "ключ воркера пуст: /home/agent-dev/.llm_key (перезапустите scripts/16)"; echo "pi exit=126"; } > "$OUT"
  rm -f "$PROMPT"; exit 126
fi
timeout "$TO" sudo -u agent-dev env HOME=/home/agent-dev SPARK_LLM_KEY="$KEY" \
  PATH="/home/agent-dev/.pi/agent/bin:/usr/local/bin:/usr/bin:/bin" \
  "$PI_BIN" --model spark/qwen3.8-27b --no-session --print "$(cat "$PROMPT")" > "$OUT" 2>&1
RC=$?
rm -f "$PROMPT"
echo "pi exit=$RC" >> "$OUT"
exit $RC
RUNNER_EOF
chmod 755 /usr/local/sbin/agent-run-pi.sh

echo "=== [5/7] Реестр репозиториев (опционально) ==="
touch /etc/agents-repos.list   # формат: <имя> <git-url>, по строке
cat <<'HOWTO'
  Заполните /etc/agents-repos.list и клонируйте рабочей копией agent-dev:
    while read -r n u; do [ -n "$n" ] && sudo -u agent-dev git clone "$u" "/srv/agents/repos/$n"; done < /etc/agents-repos.list
HOWTO

echo "=== [6/7] Смоук Pi: модель видна и отвечает (без env-переменных) ==="
PI_CHECK=""
for c in /usr/local/bin/pi /usr/bin/pi /home/agent-dev/.pi/agent/bin/pi \
         /home/agent-dev/.local/bin/pi /home/agent-dev/bin/pi \
         /home/agent-dev/.pi/agent/install/releases/*/bin/pi; do
  [[ -x "$c" ]] && { PI_CHECK="$c"; break; }
done
[[ -n "$PI_CHECK" ]] || PI_CHECK="$(sudo -u agent-dev bash -lc 'command -v pi' 2>/dev/null || true)"
if [[ -z "$PI_CHECK" ]]; then
  echo "⚠ бинарь pi не найден — смоук пропущен (установка: scripts/16, шаг 2)"
else
  echo "  pi: $PI_CHECK"
  if sudo -u agent-dev env -i HOME=/home/agent-dev USER=agent-dev LOGNAME=agent-dev \
       PATH="/home/agent-dev/.pi/agent/bin:/usr/local/bin:/usr/bin:/bin" TERM=dumb \
       "$PI_CHECK" --list-models spark 2>&1 | grep -q "qwen3.8-27b"; then
    echo "  ✓ spark/qwen3.8-27b видна без SPARK_LLM_KEY в окружении"
  else
    echo "  ✗ Pi НЕ видит модель. Диагностика:"
    echo "      sudo -u agent-dev cat /home/agent-dev/.pi/agent/models.json"
    echo "      sudo -u agent-dev wc -c /home/agent-dev/.llm_key   # должно быть > 0"
    echo "      sudo -u agent-dev env -i HOME=/home/agent-dev PATH=/usr/bin:/bin '$PI_CHECK' --list-models"
    echo "    Причина почти всегда одна: apiKey в models.json ссылается на \$SPARK_LLM_KEY,"
    echo "    а в сессии этой переменной нет — Pi скрывает провайдер. Лечится headless-шаблоном"
    echo "    agents/pi/models.headless.json (apiKey: "!cat /home/agent-dev/.llm_key")."
  fi
  # Смоук обязан идти из каталога, который ЧИТАЕТ agent-dev, и без сессии:
  # из-под root-cwd Pi печатал «Stored session working directory does not exist»
  # и до модели не доходил — смоук падал при живом шлюзе. --mode rpc здесь НЕЛЬЗЯ:
  # он ждёт JSONL из stdin и висит; one-shot — только --print.
  SMOKE_CWD=/srv/agents/repos/demo
  sudo -u agent-dev mkdir -p "$SMOKE_CWD" 2>/dev/null || SMOKE_CWD=/tmp
  ECHO_OUT="$(cd "$SMOKE_CWD" 2>/dev/null && timeout 120 sudo -u agent-dev env -i \
    HOME=/home/agent-dev USER=agent-dev LOGNAME=agent-dev PWD="$SMOKE_CWD" \
    PATH="/home/agent-dev/.pi/agent/bin:/usr/local/bin:/usr/bin:/bin" TERM=dumb \
    "$PI_CHECK" --model spark/qwen3.8-27b --no-session --print 'Ответь ровно: PONG' 2>&1 || true)"
  if printf '%s' "$ECHO_OUT" | grep -qi "PONG"; then
    echo "  ✓ echo-запрос через шлюз :4000 прошёл (cwd $SMOKE_CWD)"
  else
    echo "  ✗ echo-запрос не прошёл. Вывод Pi (последние строки):"
    printf '%s\n' "$ECHO_OUT" | tail -n 6 | sed 's/^/      /'
    echo "    повтор вручную из читаемого cwd:"
    echo "      cd /srv/agents/repos/demo && sudo -u agent-dev env -i HOME=/home/agent-dev \\"
    echo "        PATH=/home/agent-dev/.pi/agent/bin:/usr/bin:/bin '$PI_CHECK' \\"
    echo "        --model spark/qwen3.8-27b --no-session --print 'Ответь ровно: PONG'"
    echo "    проверка шлюза: curl -s http://127.0.0.1:4000/health/liveliness"
  fi
  if printf '%s' "$ECHO_OUT" | grep -qi "PONG"; then
    echo "  ✓ echo-запрос через шлюз :4000 прошёл"
  else
    echo "  ✗ echo-запрос не прошёл. Вывод Pi (последние строки):"
    printf '%s\n' "$ECHO_OUT" | tail -n 6 | sed 's/^/      /'
    echo "    проверка шлюза: curl -s http://127.0.0.1:4000/health/liveliness"
  fi
fi

echo "=== [7/7] Смоук: демо-репо + задача code-fix ==="
D=/srv/agents/repos/demo
if [[ ! -d "$D/.git" ]]; then
  sudo -u agent-dev bash -c "mkdir -p $D && cd $D && git init -q \
    && git config user.email agent@spark && git config user.name agent \
    && printf 'def add(a, b):\n    return a + b\n' > calc.py \
    && printf 'def test_add():\n    assert add(2, 2) == 4\n' > test_calc.py \
    && git add -A && git commit -qm init"
fi
OKEY="$(grep -oP '(?<=^ORCH_API_KEY=).*' "$ENVF")"
# Смоук создаёт задачу ТОЛЬКО если оркестратор жив и знает шаблон code-fix.
# Без этой проверки скрипт молча печатал "task #null", а админ шёл искать задачу,
# которой не существует. Шаблоны code-fix/code-review/code-feature сеет
# оркестратор при старте (agents/orchestrator/orchestrator.py, SEED_TEMPLATES).
if ! systemctl is-active --quiet agents-orchestrator; then
  echo "⚠ agents-orchestrator не активен — демо-задача НЕ создана."
  echo "  запуск: systemctl start agents-orchestrator (или scripts/11-setup-orchestrator.sh)"
elif ! curl -s http://127.0.0.1:8500/api/templates | grep -q '"code-fix"'; then
  echo "⚠ шаблон code-fix не найден: код оркестратора в /opt/orchestrator устарел."
  echo "  обновите: install -m 644 <копия проекта>/agents/orchestrator/orchestrator.py /opt/orchestrator/orchestrator.py && systemctl restart agents-orchestrator"
else
  TID="$(curl -s -X POST http://127.0.0.1:8500/api/tasks -H "X-Orch-Key: $OKEY" -H 'Content-Type: application/json' \
    -d '{"template":"code-fix","payload":{"repo":"demo","text":"Добавить в calc.py функцию divide(a,b) с проверкой деления на ноль (ValueError) + тесты в test_calc.py"},"requester":"p4-setup","group":"development"}' | jq -r .id)"
  if [[ "$TID" =~ ^[0-9]+$ ]]; then
    echo "code-fix task #$TID: implementer → qa → reviewer → gate(человек)"
    echo "  статус:  curl -s http://127.0.0.1:8500/api/tasks/$TID | jq -r '.steps[] | \"\\(.role): \\(.status)\"'"
    echo "  артефакты: /srv/agents/artifacts/task-$TID/ (TASK-*.md, OUT-*.md, *.diff)"
    echo "  gate: после шага reviewer задача встанет в awaiting_approval — решение в /tasks/"
  else
    echo "⚠ задачу создать не удалось (ответ API: '$TID'). Смотрите: journalctl -u agents-orchestrator -n 50 --no-pager"
  fi
fi
