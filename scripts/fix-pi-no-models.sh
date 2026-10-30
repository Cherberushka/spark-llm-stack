#!/usr/bin/env bash
# =============================================================================
# fix-pi-no-models.sh — горячая правка «Warning: No models available» у agent-dev
# Запуск НА SPARK:  sudo bash scripts/fix-pi-no-models.sh
#
# Причина (проверено на исходниках Pi 1.4.0, npm @earendil-works/pi-coding-agent):
#  1) apiKey "$SPARK_LLM_KEY" в models.json: если переменной нет в окружении,
#     Pi считает провайдер неавторизованным и СКРЫВАЕТ все его модели
#     (composeApiKeyAuth.check → undefined → getAvailableSnapshot()=0).
#     У agent-dev в интерактивной сессии этой переменной нет.
#     Лечение: ключ командой из файла — Pi исполняет её в момент запроса.
#  2) "--model spark:qwen3.8-27b" — синтаксис Pi 0.10.2 (ноябрь 2025).
#     В Pi 1.x провайдер отделяется СЛЭШЕМ, а двоеточие — уровень thinking:
#     spark/qwen3.8-27b (опционально :high). Иначе «Model ... not found».
# =============================================================================
set -euo pipefail

AD=/home/agent-dev
KEYF="$AD/.llm_key"

echo "=== 1/6 Ключ воркера ==="
if [[ ! -s "$KEYF" ]]; then
  sudo grep -oP '(?<=^CODE_WORKER_KEY=).*' /etc/orchestrator.env | tr -d '\r\n' > "$KEYF" || true
fi
if [[ ! -s "$KEYF" ]]; then
  echo "✗ ключ пуст. Создайте его в LiteLLM и пропишите CODE_WORKER_KEY=... в /etc/orchestrator.env"
  exit 1
fi
chown agent-dev:agent-dev "$KEYF"; chmod 600 "$KEYF"
echo "✓ $KEYF ($(wc -c < "$KEYF") байт, права 600, владелец agent-dev)"

echo "=== 2/6 models.json для agent-dev (ключ командой, без env) ==="
mkdir -p "$AD/.pi/agent"
[[ -f "$AD/.pi/agent/models.json" ]] && cp -a "$AD/.pi/agent/models.json" "$AD/.pi/agent/models.json.bak.$(date +%s)"
cat > "$AD/.pi/agent/models.json" <<EOF
{
  "\$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "spark": {
      "name": "DGX Spark (LiteLLM gateway)",
      "baseUrl": "http://127.0.0.1:4000/v1",
      "apiKey": "!cat $KEYF",
      "api": "openai-completions",
      "models": [
        {
          "id": "qwen3.8-27b",
          "name": "Qwen3.8 27B NVFP4 (Spark)",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 262144,
          "maxTokens": 32768,
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
EOF
chown -R agent-dev:agent-dev "$AD/.pi"; chmod 600 "$AD/.pi/agent/models.json"
python3 -c 'import json,sys;json.load(open(sys.argv[1]));print("✓ JSON валиден")' "$AD/.pi/agent/models.json"

echo "=== 3/6 Раннер: --model spark/qwen3.8-27b (слэш) ==="
R=/usr/local/sbin/agent-run-pi.sh
if [[ -f "$R" ]]; then
  cp -a "$R" "$R.bak.$(date +%s)"
  sed -i 's|--model spark:qwen3.8-27b|--model spark/qwen3.8-27b|g' "$R"
  grep -q 'install/releases/\*/bin/pi' "$R" || sed -i 's|/home/agent-dev/bin/pi; do|/home/agent-dev/bin/pi \\\n         /home/agent-dev/.pi/agent/install/releases/*/bin/pi; do|' "$R"
  bash -n "$R" && echo "✓ раннер обновлён: $(grep -o -- '--model [^ ]*' "$R")"
else
  echo "⚠ $R нет — его ставит scripts/16-setup-code-line.sh"
fi

echo "=== 4/6 Бинарь pi ==="
PI_BIN=""
for c in /usr/local/bin/pi /usr/bin/pi "$AD/.pi/agent/bin/pi" "$AD/.local/bin/pi" "$AD/bin/pi" "$AD"/.pi/agent/install/releases/*/bin/pi; do
  [[ -x "$c" ]] && { PI_BIN="$c"; break; }
done
[[ -n "$PI_BIN" ]] || PI_BIN="$(sudo -u agent-dev bash -lc 'command -v pi' 2>/dev/null || true)"
if [[ -z "$PI_BIN" ]]; then
  echo "⚠ pi не найден. Установка: sudo -u agent-dev bash -lc 'curl -fsSL https://pi.dev/install.sh | sh'"
  exit 0
fi
echo "✓ pi: $PI_BIN"

echo "=== 5/6 Смоук: Pi видит модель БЕЗ SPARK_LLM_KEY в окружении ==="
if sudo -u agent-dev env -i HOME="$AD" USER=agent-dev LOGNAME=agent-dev TERM=dumb \
     PATH="$AD/.pi/agent/bin:$AD/.local/bin:/usr/local/bin:/usr/bin:/bin" \
     "$PI_BIN" --list-models spark 2>&1 | grep -q "qwen3.8-27b"; then
  echo "✓ spark/qwen3.8-27b в списке доступных"
else
  echo "✗ модели по-прежнему не видны. Смотрите вывод ниже и проверьте права на $KEYF:"
  sudo -u agent-dev env -i HOME="$AD" PATH=/usr/bin:/bin TERM=dumb "$PI_BIN" --list-models 2>&1 | tail -n 15
  exit 1
fi

echo "=== 6/6 Смоук: живой запрос через шлюз :4000 ==="
# cwd обязан быть читаем для agent-dev, иначе Pi падает на сессии и до модели не доходит.
SMOKE_CWD=/srv/agents/repos/demo
sudo -u agent-dev mkdir -p "$SMOKE_CWD" 2>/dev/null || SMOKE_CWD=/tmp
OUT="$(cd "$SMOKE_CWD" 2>/dev/null && timeout 120 sudo -u agent-dev env -i HOME="$AD" \
  USER=agent-dev LOGNAME=agent-dev PWD="$SMOKE_CWD" TERM=dumb \
  PATH="$AD/.pi/agent/bin:$AD/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  "$PI_BIN" --model spark/qwen3.8-27b --no-session --print 'Ответь ровно: PONG' 2>&1 || true)"
if printf '%s' "$OUT" | grep -qi PONG; then
  echo "✓ Pi → LiteLLM → llama.cpp работает"
else
  echo "✗ запрос не прошёл. Вывод Pi:"; printf '%s\n' "$OUT" | tail -n 10 | sed 's/^/    /'
  echo "  шлюз: curl -s http://127.0.0.1:4000/health/liveliness"
  echo "  ключ: curl -s http://127.0.0.1:4000/v1/models -H \"Authorization: Bearer \$(sudo cat $KEYF)\" | head -c 200"
  exit 1
fi

cat <<'DONE'

Готово. Дальше:
  sudo bash scripts/16-setup-code-line.sh     # смоук + демо-задача code-fix (из свежей копии v3.12.1)
  Интерактивно от agent-dev:  pi --model spark/qwen3.8-27b
  Внутри Pi: /model — выбрать, Ctrl+S — сделать моделью по умолчанию.
DONE
