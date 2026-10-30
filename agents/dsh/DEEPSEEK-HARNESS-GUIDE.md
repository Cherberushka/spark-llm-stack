# DeepSeek Harness (dsh) → ваш Qwen3.8-27B на DGX Spark

dsh (DeepSeek Harness, MIT, developer-preview) — агент-харнесс с веб-UI и
headless-режимом; любой OpenAI-совместимый эндпоинт подключается как custom
provider. Наш эндпоинт — LiteLLM-шлюз Spark: `http://192.168.11.251:4000/v1`,
модель `qwen3.8-27b`, ключ — персональный виртуальный.

Роль в стеке: альтернативный харнесс «browser-first» + headless для CI/cron.
Основной терминальный харнесс по-прежнему **Pi**; IDE — Cline. Все трое через
один шлюз и одни ключи.

---

## 0. Разделение ролей: сервер vs персональная машина (Windows)

### Сервер (Spark) — админ, ~5 минут, НИЧЕГО не устанавливаем
1. Проверка доступности шлюза из org-подсетей: `sudo iptables -L DOCKER-USER -n | grep 4000`
   (RETURN для 192.168.8.0/22, DROP остальным; порт 4000 режет DOCKER-USER, не ufw).
2. Персональный виртуальный ключ владельцу машины:
   `POST /key/generate` с мастер-ключом: `{"key_alias":"dsh-win-<фамилия>","models":["qwen3.8-27b","bge-m3"],"rpm":30,"tpm":300000}`;
   передать лично. Опционально: бюджет/срок в UI :8443.
3. Контроль: `curl http://192.168.11.251:4000/v1/models -H "Authorization: Bearer sk-…"` → qwen3.8-27b, bge-m3.
Машины вне 192.168.8–11 — только через VPN.

### Персональная машина Windows — пользователь, ~15 минут
0. `Test-NetConnection 192.168.11.251 -Port 4000` → True.
1. `winget install OpenJS.NodeJS.LTS`, новый терминал, `node -v` ≥20.
2. `mkdir $HOME\dsh-work; cd $HOME\dsh-work; npm install @deepseek-ai/dsh@0.1.2-rc.1`
   (папка запуска = воркспейс агента).
3. `setx SPARK_LLM_KEY "sk-…"` + новый терминал.
4. `%USERPROFILE%\.dsh\settings.yaml` — провайдер `spark` (baseURL
   `http://192.168.11.251:4000/v1`, `api: openai-completions`, `apiKeyEnv: SPARK_LLM_KEY`,
   `defaultContextWindow: 131072`, модель `qwen3.8-27b` с `input: [text, image]`,
   `contextWindow: 131072`, `maxTokens: 32768`). Либо UI: Settings → Models →
   Add custom provider (id `spark`, протокол openai-completions, Fetch models).
5. `cd $HOME\dsh-work; .\node_modules\.bin\dsh.cmd web` → http://127.0.0.1:3080
   (старт до 2 мин без прогресс-бара); notice кликнуть; ключ DeepSeek — Configure later.
6. Смоук: `.\node_modules\.bin\dsh.cmd --profile headless "Ответь ровно: dsh-spark-ok"`;
   в UI — файловая задача + скриншот (зрение).
7. Правила: запуск из папки задачи; не от админа; permission-промпты включены;
   https-маршрут через Caddy только с `setx NODE_EXTRA_CA_CERTS …\spark.crt`.

Windows-troubleshooting: `.cmd`-шим вместо `dsh`; тишина при старте до 2 мин — норма;
401 → проверить env-ключ и новый терминал после setx; UNKNOWN_MODEL → id ровно
`qwen3.8-27b`; контекст/изображения → метаданные в settings.yaml (§3).

## 1. Установка (рабочая станция; Node ≥ 20)

```bash
mkdir ~/dsh-work && cd ~/dsh-work        # каталог запуска = корень воркспейса агента!
npm install @deepseek-ai/dsh@0.1.2-rc.1  # ставим отдельно от запуска и ПИНУЕМ версию
./node_modules/.bin/dsh web              # старт до ~2 мин БЕЗ прогресс-бара — это норма
# → dsh web: http://127.0.0.1:3080
```
Проверка «жив ли»: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3080/`
и `ps -o %cpu,rss,etime -p "$(pgrep -f 'dsh.*web' | head -1)"`.
Не запускайте dsh из каталога ценного репозитория «на пробу»: воркспес-рут =
каталог запуска (агент будет писать туда).

## 2. Провайдер «spark» (два способа)

### Способ А — UI
Первый вход: кликнуть тестовое уведомление; на вопросе ключа DeepSeek —
**Configure later**. Далее *Settings → Models → Add a custom provider*:

| Поле | Значение | Примечание |
|---|---|---|
| Provider ID | `spark` | **навсегда**: сессии/ключи привязаны к id, переименование = новый провайдер |
| Display name | DGX Spark gateway | |
| Base URL | `http://192.168.11.251:4000/v1` | |
| API protocol | `openai-completions` | |
| API key | виртуальный ключ LiteLLM | хранится в `~/.dsh/.credentials.yaml` |

Кнопка **Fetch available models** работает (шлюз отдаёт `GET /v1/models`) →
выбрать `qwen3.8-27b` → сделать моделью по умолчанию.

### Способ Б — `~/.dsh/settings.yaml` (точнее: сразу чинит метаданные)

```yaml
llm-pi-ai:
  providers:
    spark:
      apiKeyEnv: SPARK_LLM_KEY          # export SPARK_LLM_KEY=sk-... в_profile_ шелла
      api: openai-completions
      baseURL: http://192.168.11.251:4000/v1
      defaultContextWindow: 131072      # ВАЖНО, см. §3
      models:
        - id: qwen3.8-27b
          input: [text, image]          # ВАЖНО: рукописные модели иначе text-only
          contextWindow: 131072
          maxTokens: 32768
```

## 3. Три грабли dsh, которые нужно знать заранее

1. **Контекст**: для необъявленных моделей dsh предполагает окно **262144** —
   больше нашего слота (131072 при `--parallel 2`). Длинные сессии упрутся в
   ошибку бэкенда. Лечится `contextWindow: 131072` (или `defaultContextWindow`
   на маршрут) — как выше.
2. **Зрение**: модель, введённая руками, считается text-only и отклоняет
   вложения до отправки (`MODEL_DOES_NOT_SUPPORT_IMAGES`). Лечится
   `input: [text, image]` (наш движок зрение реально поддерживает).
3. **Reasoning**: не объявляйте `reasoningEfforts` — dsh будет считать модель
   «без рассуждений», что совпадает с нашим серверным дефолтом
   (`--reasoning-budget 0`). Для задач «подумать глубоко» используйте Pi/Open
   WebUI с явным `chat_template_kwargs`, не dsh.

## 4. Проверка и повседневное использование

```bash
# смоук: headless-прогон (CI/cron-формат)
./node_modules/.bin/dsh --profile headless "Ответь ровно: dsh-spark-ok"
```
В веб-UI: новая сессия → задача в воркспейсе; перед привилегированными
операциями dsh показывает **permission prompt** (не отключайте). Прикрепите
скриншот — проверка зрения. Логи сессий: `~/.dsh/storages/`.

## 5. Безопасность и границы

- dsh исполняет команды **на станции запуска** (как desktop-commander): те же
  правила изоляции (WINDOWS-MCP-GUIDE §10 — учётка-песочница для экспериментов);
- не публикуйте UI наружу: дефолтный bind 127.0.0.1; для доступа с других машин
  нужен bind 0.0.0.0 + `trustedHosts` в конфиге — не делайте этого без нужды;
- ключи: только персональные виртуальные (не мастер-ключ).

## 6. Честные ожидания (developer preview)

- rc-версии, ломающие изменения обещаны README; плагинов-«всё-заменяемо» много,
  качество пёстрое (775+ репо под тегом dsh-plugin за сутки после релиза);
- нет интерактивного TUI (headless есть, браузера — нет);
- холодный старт через npx медленный (npm-резолв) — потому установка отдельным
  шагом и пин версии;
- вывод: **игрушка-платформа для экспериментов и browser-first сценариев**,
  боевой харнесс дня — Pi/Cline. Если dsh понравится командой — держите его
  вторым инструментом, не заменяя Pi.

## 7. Скиллы в dsh: перенос из проекта

Полный список скиллов проекта (что установлено у Pi, что вшито в роли
оркестратора, что принято в чате) — `SKILLS-INVENTORY.md`. Здесь только механика
переноса в dsh.

### 7.1 Формат и корни (проверено по docs `dsh-skill-filesystem`, 2026-10)

Скилл = каталог `<name>/SKILL.md` (или плоский `<name>.md`) **ровно на один
уровень** под корнем сканирования; вложенные `**/SKILL.md` не обнаруживаются.
Frontmatter: `name` (kebab-case, 1-64) и `description` (1-1024) обязательны;
необязательны `license`, `compatibility`, `metadata`, `allowed-tools`,
`whenToUse`, `disable-model-invocation`, `user-invocable`.

| ранг | корень | путь |
|---|---|---|
| 100 | проектный dsh | `<projectRoot>/.dsh/skills` |
| 200 | проектный agents | `<projectRoot>/.agents/skills` |
| 300 | custom | `Config.customSkillDirs` |
| 400 | пользовательский dsh | `<dshHome>/skills` (`$DSH_HOME` или `~/.dsh`) |
| 500 | пользовательский agents | `<agentsHome>/skills` (`$DSH_AGENTS_HOME` или `~/.agents`) |

`projectRoot` — ближайший предок с `.git`, иначе cwd. Меньший ранг побеждает при
совпадении имён. Корни watched: перезапуск dsh не нужен. В системный промпт
попадают `name`+`description`, тело грузится инструментом `skill` по имени или
жестом `/name` в сообщении.

### 7.2 Экспорт на Spark и установка на станции

```bash
cd ~/spark-llm-stack && bash scripts/21-export-dsh-skills.sh
# скиллов: 22 → ~/dsh-skills-export
# zip: ~/dsh-skills-export-<дата>.zip (1-2 МБ)
# sha256 <…>
```

Перенести zip на станцию с dsh (scp/флешка), распаковать и поставить:

```bash
cd ~/dsh-skills-export && bash install.sh                      # → ~/.agents/skills (ранг 500)
cd ~/dsh-skills-export && bash install.sh --project ~/work/repo # → <repo>/.dsh/skills (ранг 100)
```

Windows (PowerShell): `.\install.ps1` или `.\install.ps1 -Project C:\work\repo`.

Проверка в сессии dsh: `/unlazy`, `/mp-code-review`, `/spark-role-qa` — тело
скилла подставляется в шаг. Файловая проверка:

```bash
ls ~/.agents/skills | wc -l
grep -L '^description:' ~/.agents/skills/*/SKILL.md || echo 'frontmatter у всех есть'
```

### 7.3 Наборы (экономия системного промпта)

Описания всех скиллов живут в системном промпте каждой сессии. Замер по экспорту:
22 скилла = 7961 символ описаний (сумма `desc_chars` из `manifest.json`); по
2-2,5 символа на токен в русском тексте это порядка 3-4 тыс. токенов на каждый
запрос — оценка, не замер токенизатора. Если жалко:

```bash
SET=code bash scripts/21-export-dsh-skills.sh     # 11 скиллов, 2689 символов описаний
ONLY=unlazy,mp-tdd,spark-role-qa bash scripts/21-export-dsh-skills.sh
NO_ZIP=1 OUT=/tmp/x bash scripts/21-export-dsh-skills.sh
```

Наборы: `code` (mp-* кодовые, unlazy, 4 роли), `docs` (ant-*, mp-handoff,
mp-writing-for-agents), `frontend` (taste-skill), `ops` (mp-wizard,
spark-code-line), `roles`, `chat` (caveman), `all`.

### 7.4 Грабли переноса

1. **Роль-карточки — не субагенты dsh.** dsh плодит детей инструментами
   `subagent`/`subagent_fork` и не читает `/etc/agents-roles`. Роли
   экспортируются как скиллы-процедуры (`/spark-role-qa`); для изоляции —
   попросить модель вызвать `subagent` с текстом роли как заданием.
2. **Зависимости ставятся на машину dsh, не на Spark.** `ant-*` нужны python/node
   библиотеки и LibreOffice, `taste-skill` — node 22+ и локальный Chrome. Fallback
   «если import упал — pip/npm install» в их SKILL.md требует сеть: на закрытой
   станции ставить заранее.
3. **`disable-model-invocation: true`** (у `mp-handoff`, `mp-implement`,
   `mp-to-spec`, `caveman`) — модель сама их не зовёт, только `/имя`.
4. **Лимит каталога**: при >100 записей (`maxIndexEntries`) лишние уходят из
   каталога модели и остаются только user-invocable; у нас 22 — запас есть.
5. **description обрезается до 1024** символов; факт обрезки печатается в
   предупреждениях экспорта и попадает в `MANIFEST.md`.
6. **Имя каталога = `name` во frontmatter** — экспортер приводит их к одному
   значению, иначе вызов `/имя` и поиск по каталогу расходятся.
7. Экспорт не трогает тела скиллов: правка vendor-копии = правка upstream-текста,
   собирайте заново и сверяйте `git diff` в `vendor-skills/`.

## Troubleshooting

| Симптом | Лечение |
|---|---|
| `UNKNOWN_MODEL` | id модели в dsh ≠ имени на шлюзе; сверьте с `curl …/v1/models` (должно быть `qwen3.8-27b`) |
| Ошибка контекста на длинной сессии | §3.1: contextWindow 131072 |
| Отказ прикрепить изображение | §3.2: `input: [text, image]` |
| Молчит при старте >5 мин | это npm-резолв (npx-путь); ставьте пакет отдельно (§1) |
| 401 от шлюза | ключ: `SPARK_LLM_KEY` в окружении / поле API key; проверьте ключ в LiteLLM UI |
| Скилл не виден в dsh | §7.1: лежит ровно на один уровень под одним из корней; `name` = имя каталога; frontmatter без ошибок (битый файл пропускается с warning'ом, а в каталоге выглядит как отсутствующий) |
| Скилл есть, но модель его не зовёт | `disable-model-invocation: true` → только `/имя`; либо запись за лимитом `maxIndexEntries` (§7.4.4) |
