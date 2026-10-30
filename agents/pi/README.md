# Pi-агент → DGX Spark (настройка рабочей станции разработчика)

Pi (pi.dev, `@earendil-works/pi-coding-agent`) — минималистичный агент-харнесс:
LSP, MCP, skills, extensions, prompt-templates. Подключаем его к нашему шлюзу
LiteLLM на Spark — и Pi работает с локальными моделями как с обычным
OpenAI-совместимым провайдером.

## 1. Установка Pi (на рабочей станции: Linux/macOS/WSL)

```bash
curl -fsSL https://pi.dev/install.sh | sh
# или: npm install -g --ignore-scripts @earendil-works/pi-coding-agent
```

## 2. Конфигурация провайдера

Скопируйте шаблон `models.json` из этой папки в конфигурационный каталог Pi
(обычно `~/.pi/agent/models.json`; точный путь — Pi docs → Configuration) и
замените `<SPARK_IP>` на адрес вашего Spark. Для headless-пользователя
(воркер `agent-dev` на Spark) берите `models.headless.json` — см. §4:

```bash
mkdir -p ~/.pi/agent
cp models.json ~/.pi/agent/models.json
sed -i "s/<SPARK_IP>/192.168.1.50/" ~/.pi/agent/models.json   # ваш IP
```

Либо запустите на Spark помощник, который сделает это сам (спросит IP и ключ):

```bash
bash scripts/06-setup-pi.sh
```

## 3. Ключ шлюза

Ключ не храним в json — используем интерполяцию `$SPARK_LLM_KEY`:

```bash
# ключ = LITELLM_MASTER_KEY из /etc/spark-llm.env на Spark
# (или виртуальный ключ вашего отдела, созданный админом в LiteLLM)
echo 'export SPARK_LLM_KEY=sk-...' >> ~/.bashrc && source ~/.bashrc
```

## 4. Запуск

```bash
pi --model spark/qwen3.8-27b      # модель стека (агентный кодинг)
pi --list-models spark            # проверить, что провайдер виден
```

Синтаксис `--model` в Pi 1.x: `провайдер/модель`, необязательный суффикс
`:уровень-thinking` — `spark/qwen3.8-27b:high`. Старая запись `spark:qwen3.8-27b`
(через двоеточие) работала только в Pi 0.10.x и в 1.4.0 даёт
`Model "spark:qwen3.8-27b" not found`.

### «Warning: No models available» — что это значит

Pi показывает в `/model` только те модели, чей провайдер **авторизован**.
Для кастомного провайдера из `models.json` авторизация = `apiKey`, а значение
`"$SPARK_LLM_KEY"` Pi раскрывает из окружения: нет переменной → провайдер
считается неавторизованным → моделей ноль (в `--print` это `exit 1`).

| Где запускаете Pi | Как отдавать ключ |
|---|---|
| рабочая станция (интерактивно) | `export SPARK_LLM_KEY=sk-…` в `~/.bashrc` + `"apiKey": "$SPARK_LLM_KEY"` (шаблон `models.json`) |
| headless/CI/воркер (unix-пользователь без профиля) | ключ в файле 0600 + `"apiKey": "!cat /путь/к/файлу"` (шаблон `models.headless.json`) — env не нужен |

`!команда` исполняется Pi в момент запроса (шелл, таймаут 10 с, вывод trim);
`$$` и `$!` — экранированные литералы. Приоритет источников ключа:
`--api-key` → `auth.json` → `apiKey` из `models.json` → env провайдера.

Диагностика за 30 секунд:

```bash
pi --list-models spark                 # пусто → нет авторизации провайдера
pi auth check --provider spark         # покажет источник ключа или причину
python3 -m json.tool ~/.pi/agent/models.json >/dev/null && echo "json ok"
```

Внутри Pi: `/model` — переключение, `/think` — управление режимом рассуждений
(Qwen3.8 thinking-capable), `/help` — всё остальное.

## 5. Рекомендации под Qwen3.8-27B (из карточки модели и практики)

- **Агентные циклы**: thinking выключен или `reasoning_effort: low` — экономия
  40–60% токенов при <1% потерь качества (модель «overthinks» по умолчанию).
- **Сложные одиночные задачи** (дебаг, архитектура): thinking включить.
- Сэмплинг: temperature 1.0, top_p 0.95, top_k 20, min_p 0.0 (рекомендация Qwen);
  не обрезать `max_tokens` на агентных задачах — карточка прямо просит
  «adequate output length».
- Инструменты: на vLLM включён `--tool-call-parser qwen3_xml` +
  `--enable-auto-tool-choice` — MCP/тулинг Pi работают из коробки.

## 6. Skills для Pi (из ваших репозиториев)

Pi поддерживает skills-каталоги; набор инженерных скиллов mattpocock/skills
ставится через skills.sh с флагом агента `-a pi` (точная команда — в README
репозитория github.com/mattpocock/skills, секция установки). Taste-skill
(дизайн-скилл) аналогично — для фронтенд-задач.

## Docs

- Pi: https://pi.dev/docs (models.json: `/docs/latest/models-configuration`;
  локальные модели: `/docs/latest/llama-cpp`; NVIDIA-рецепты:
  `/docs/latest/nvidia-rtx-llama-cpp`)
- Шлюз: LiteLLM на `http://<SPARK_IP>:4000` (модель `qwen3.8-27b`,
  эмбеддинги `bge-m3`)
