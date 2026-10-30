# STATE — кэш состояния стека (обновлять после каждого этапа)

> Зачем: чтобы новая сессия/новый инженер начинал с 1 экрана, а не с перечитывания
> всех планов. Всё остальное — по ссылкам в конце. Обновлён: 30.10.2026
> (P4: задачи #24–#27 прошли цепочку Pi-ролей, диф-артефакты пишутся — факт с машины
> по задаче #27. Закрыты два дефекта базы дифа: база теперь задача-уровня (маркер
> `base-rev.txt`), ошибка git определяется кодом возврата, а не лексикой stderr.
> Добавлен перенос скиллов в DeepSeek Harness: `SKILLS-INVENTORY.md`,
> `scripts/21-export-dsh-skills.sh`, `agents/dsh/` §7. Пакет v3.12.7, тесты:
> кодовая линия 116 проверок / 0 провалов, экспорт скиллов 34 / 0.
> `OPS-CARD.md` — ежедневная карта эксплуатации, §3.7 массовый reject гейтов).

## 1. Что работает сейчас

| Компонент | Состояние |
|---|---|
| llama.cpp (Qwen3.8-27B, UD-Q4_K_XL + DFlash2 + mmproj) | :8000, ctx 262144, parallel 2, thinking OFF, ~50 tok/s |
| bge-m3 (Ollama) | :11434, dim 1024 |
| LiteLLM + Postgres + Redis | :4000, ключи выданы |
| Open WebUI + Caddy | :443 (web) / :8443 (LiteLLM admin UI) |
| Оркестратор + /tasks | :8500, 18 задач выполнено / 2 провалено (исторически) |
| rag-svc v2 | :8502, **backend=qdrant**, collection `rag_chunks`, mode=hybrid, dim=1024 |
| Qdrant | контейнер `spark-qdrant`, только 127.0.0.1:6333 (наружу не публикуется — by design) |
| Шара Samba `doctor` | работает, транслит-санитайзер |
| Бэкап + утренний дайджест | cron 03:30 / 06:30, зелёные |

**Корпус знаний сейчас ПУСТ** (chunks:0). Импорт КР (`scripts/19`) осознанно НЕ запускался —
решение от 28.10.2026: сначала Qdrant, потом массовый импорт отдельным шагом.
`docs:1` в зеркале SQLite — фантомная строка документа без чанков; лечится `/reindex`
или повторным добавлением документа.

## 2. Линии работ

- **P1 (движок/гейтвей/web/firewall)** — готово, verify 11/11.
- **P2 (RAG)** — готово: v2 (dual backend, hybrid RRF, фильтры, supersede, compact, reindex), MCP-сервер, тесты зелёные.
- **P2.5 (Qdrant)** — готово 28.10.2026: контейнер, collection, `/reindex use_existing_vectors`, откат `RAG_BACKEND=sqlite`.
- **P3 (MSSQL read-only Text-to-SQL)** — **заблокировано на данных от DBA**: host/port, GRANT для `spark_ro`, 2–10 таблиц со смыслом, словарь данных, staging-копия, права на Query Store/dm_*, TZ/collation.
- **P4 (кодовая линия: Pi headless + роли + гейты)** — на Spark установлены пользователь `agent-dev`, Pi 1.4.0, роль-карточки `/etc/agents-roles/`, раннер `/usr/local/sbin/agent-run-pi.sh`, ключ `CODE_WORKER_KEY`. **Задачи #24 и #25 (`code-fix`, repo `demo`) прошли полную цепочку Pi-ролей**: #24 — approve на гейте, статус `done`; #25 — implementer/qa/reviewer `done`, гейт (step id 39) `awaiting_approval` (30.10.2026).
  - Теперь реализовано: pi-worker в оркестраторе (`w_pi`) + шаблоны `code-fix` / `code-review` / `code-feature`; роли `implementer`/`qa`/`reviewer`/`security` исполняются Pi от `agent-dev` в `/srv/agents/repos/<repo>`; артефакты `TASK-*.md`, `OUT-*.md`, `<id>-<role>.diff`; вердикт роли парсится из её вывода (нет явного PASS/APPROVE → шаг не «зелёный»); жёсткий бюджет шага с добиванием группы процессов; гейт человека — последний шаг.
  - Тесты: `research/test-code-line-p4.py` — **116 проверок, 0 провалов** (было 102 до v3.12.7) (без Pi/LiteLLM/Postgres, на фейковом раннере и фейковом DB-курсоре). Регрессии нет: `test-rag-svc-v2.py`, `test-mcp-e2e.py` и `test-dsh-export.py` (34 проверки) зелёные.
  - **29.10.2026, исправлено по факту сбоя у `agent-dev`** (проверено на npm-тарболе `@earendil-works/pi-coding-agent@1.4.0`, запуск `ModelRuntime`+`resolveCliModel`): (1) `apiKey: "$SPARK_LLM_KEY"` — без переменной в окружении `composeApiKeyAuth.check` возвращает `undefined`, провайдер неавторизован, `getAvailableSnapshot()` = 0 → «Warning: No models available» в TUI и `exit 1` в `--print`; добавлен шаблон `agents/pi/models.headless.json` с `apiKey: "!cat /home/agent-dev/.llm_key"` (команда исполняется Pi в момент запроса, env не нужен; `hasConfiguredAuth=true`, `getAuth` отдаёт ключ). (2) `--model spark:qwen3.8-27b` — синтаксис Pi 0.10.2 (2025-11-26); в 1.4.0 `resolveCliModel` с `thinkingSuffix:"strict"` отвечает `Model "spark:qwen3.8-27b" not found`, верно `spark/qwen3.8-27b` (опционально `:high`). Раннер, скрипт 06, визард, доки и чек-лист переведены на слэш; в `scripts/16` добавлен смоук Pi (`--list-models spark` + эхо-запрос `PONG` под `env -i`), валидация JSON, отказ на пустом `CODE_WORKER_KEY`; в раннере — поиск pi в managed-установке и `exit 126` на пустом ключе. Горячая правка без переустановки: `sudo bash scripts/fix-pi-no-models.sh`. Пакет **v3.12.1** (320 файлов); `research/test-code-line-p4.py` на тот момент — 81 проверка, 0 провалов (сейчас 102, пакет v3.12.5; sha256 — только в `SHA256SUMS.txt`, в STATE не дублируем).
  - **30.10.2026, факт с машины:** `scripts/16` отработал до конца, шаг [7/7] создал задачу **#24** (`code-fix`, repo `demo`): `implementer`/`qa`/`reviewer` — `done`, последний шаг-гейт — `awaiting_approval`. То есть раннер `/usr/local/sbin/agent-run-pi.sh` и Pi 1.4.0 под `agent-dev` **работают**; прежнее «демо-задачи нет» устарело.
  - **Ложный провал шага [6/7] (эхо-смоук) — исправлен.** Причина: смоук запускался из cwd скрипта (`"$ARENA_WORKSPACE"/spark-llm-stack`), который `agent-dev` не может прочитать → Pi печатал «Stored session working directory does not exist» и до модели не доходил. Второе: диагностика через `--mode rpc` **виснет** — rpc ждёт JSONL из stdin, one-shot бывает только `--print`. Лечение в `scripts/16` и `scripts/fix-pi-no-models.sh`: смоук идёт из `/srv/agents/repos/demo` (fallback `/tmp`) с `--no-session`; `--no-session` добавлен и в раннер (сессии ролей не копятся). Проверено на тарболе 1.4.0: `--no-session` = «Don't save session (ephemeral)», `--print/-p` = non-interactive.
  - **30.10.2026, задача #25 — найден корень «гейт без дифа» (исправлено, v3.12.5).** Факт с машины: `sudo -u agent-dev git -C /srv/agents/repos/demo diff HEAD` диф показывает (`+def mul`, `+def divide`, тесты), а `/srv/agents/artifacts/task-25/` содержит только `TASK-*`/`OUT-*` — `.diff` нет, и reviewer в промпте видел «diff пуст». Причина №1: юнит `agents-orchestrator.service` без `User=` → оркестратор работает от root, а `/srv/agents/repos/*` принадлежат `agent-dev`; git ≥ 2.35 на такой расклад отвечает `fatal: detected dubious ownership`, **stdout пуст** → `is_git=False`, ветка `task-<id>` не создавалась, диф не снимался, артефакт не писался. Причина №2: `.diff` писался только при непустом `git diff HEAD`, т.е. «изменений нет» и «git не отработал» выглядели одинаково. Причина №3: база дифа — `HEAD`, а не ревизия на старте задачи: если роль закоммитила правку, гейт остался бы слепым. Лечение в `w_pi`: git зовётся от владельца репо (`sudo -n -u agent-dev git`, fallback — разовый `-c safe.directory=<repo>`, **без записи в gitconfig**; `safe.directory` в конфиг не добавляем принципиально), база дифа = `head_before` (пустое дерево `4b825dc…`, если коммитов нет), `.diff` пишется **всегда** — с шапкой (база, число изменённых файлов, коммиты после базы, ошибки git), при пустом дифе текст «диф пуст» + список коммитов, ошибки git больше не теряются молча (информационный stderr вроде «Switched to a new branch» ошибкой не считается), добавлен раздел «НЕОТСЛЕЖИВАЕМЫЕ ФАЙЛЫ» с содержимым новых файлов — `git diff` их не показывает, а роль могла их создать (лимит `AGENTS_PI_MAX_UNTRACKED=50`, 4000 знаков на файл / 12000 на раздел). Тесты: `research/test-code-line-p4.py` — **102 проверки, 0 провалов** (добавлены: shim git/sudo под «dubious ownership», fallback без sudo, репо без коммитов, пустой диф, untracked-файл). Горячая правка на Spark без переустановки — §12c RUNBOOK (скопировать `orchestrator.py` + `systemctl restart agents-orchestrator`).
  - Статус задачи читается через `.task.status` (`GET /api/tasks/{id}` отдаёт `{task, steps, events}`) — запрос `.status` даёт `null`, это не сбой.
  - **30.10.2026, задача #27 — диф-артефакты работают (факт с машины), но найдены два дефекта базы (исправлено, v3.12.7).** В `/srv/agents/artifacts/task-27/` есть все три `.diff` (`implementer`/`qa`/`reviewer`) с шапкой — значит v3.12.5/v3.12.6 закрыли «гейт без дифа». Что осталось кривым: (1) в шапке `task-27-reviewer.diff` ложное `# ВНИМАНИЕ: git вернул ошибки: checkout -B task-27: Сброс ветки «task-27»` — `_git_err` фильтровал stderr по английским словам («switched to»), а git на русской локали пишет другое; признак ошибки перенесён в `_git_out` на **код возврата** (rc=0 → stderr не ошибка, rc≠0 → stderr + `[exit N]`), лексика больше не анализируется. (2) `# изменено файлов: 0` и `# коммитов после базы: 0` у reviewer при живой цепочке: база дифа считалась **на старте шага**, поэтому коммит роли внутри задачи сдвигал базу и гейт слеп. Теперь база — задача-уровня: первый Pi-шаг пишет маркер `/srv/agents/artifacts/task-<id>/base-rev.txt` (`<путь репо>\t<ревизия>`), следующие шаги берут её оттуда, `git log` считается от неё же, а расхождение с текущим HEAD показывается строкой `# примечание:`. Мёртвая ревизия в маркере и маркер чужого репо → база пересчитывается с примечанием, шаг не падает. Проверить причину пустого дифа у конкретной задачи: `sudo head -8 /srv/agents/artifacts/task-<id>/task-<id>-implementer.diff` и `sudo -u agent-dev git -C /srv/agents/repos/<repo> log --oneline -5`.
  - Осталось: массово закрыть висячие гейты (`OPS-CARD.md` §3.7, reject необратим) → выложить v3.12.7 (§12c RUNBOOK: `orchestrator.py` + `systemctl restart agents-orchestrator`) → контрольная задача `code-fix` и проверка, что `.diff` reviewer'а непустой при коммите роли → закоммитить правки `demo` от `agent-dev` → по выводам qa/reviewer откалибровать роль-карточки → подключить настоящий репозиторий (`/etc/agents-repos.list` + `sudo -u agent-dev git clone …`).
- **P6 (скиллы → DeepSeek Harness)** — экспорт собран 30.10.2026: `scripts/21-export-dsh-skills.sh` + `agents/dsh/export-dsh-skills.py` складывают 22 скилла (16 vendor как у Pi, 4 роль-карточки P4 как скиллы-процедуры, `spark-code-line`, `caveman`) в каталог под корни dsh (`~/.agents/skills` ранг 500 / `<repo>/.dsh/skills` ранг 100) с нормализацией frontmatter (name kebab-case, description ≤1024, неизвестные ключи → `metadata`), `MANIFEST.md`, `install.sh`, `install.ps1` и zip+sha256. Инвентаризация всех трёх слоёв скиллов (файлы у Pi / «скиллы-как-код» в ролях и шаблонах / правила чата) — `SKILLS-INVENTORY.md`; механика переноса и грабли — `agents/dsh/DEEPSEEK-HARNESS-GUIDE.md` §7. На Spark dsh не ставится: он работает на станции и ходит в шлюз :4000.
- **P5 (headroom-прокси, экономия контекста)** — не развёрнуто, отложено.
- **P6/P7-lite (MCP для клиентов, Pi/dsh/WSL-треки)** — документированы, дистрибуция на рабочие места по заявке.

## 3. Ключевые измерения (не переспрашивать, не перемерять)

- v1 узкое место: 0.99 с/запрос и 536 МБ **на каждый** запрос, 98% однопоточный Python → 20 ядер Spark дают ~1.0×.
- v2 на реальном коде, **104 910 чанков (= все 777 активных КР)**: dense 32.6 мс, hybrid 97.8 мс, bm25 20.6 мс, с фильтром 101.2 мс, RSS 521 МБ (memmap), диск 260 МБ БД + 476 МБ векторов; ingest 1865–3500 чанков/с.
- Порог «точно нужен Qdrant»: ≥200 000 чанков ИЛИ p95 > 500 мс ИЛИ несколько писателей. Перешли раньше — по решению заказчика.
- `RAG_DENSE_RESIDENT_MB=0` безопасно: резидентная матрица не даёт выигрыша при тёплом page cache (25.3 против 25.2 мс на 50k).
- FTS5 без русского стеммера → префиксный поиск (`"термин"*`); в qdrant-режиме hybrid = Qdrant dense ANN + BM25 из SQLite-зеркала (RRF).
- 1 КР ≈ 40 стр. ≈ 100k знаков ≈ 130 чанков (chunk 900 / overlap 150). S1 = 777 КР ≈ 101k чанков; S2 ≈ 195k; S3 (международные, 4395) ≈ 680k — **отклонено**.

## 4. Действующие решения

- NGC из РФ недоступен → путь A: llama.cpp + unsloth UD-Q4_K_XL, веса через hf-mirror.com.
- Nemotron — исключён (22.10.2026), история в `MODEL-ANALYSIS.md`.
- Скиллы: только курируемый набор (~16), офлайн-установка из `vendor-skills/` (`OFFLINE=1`), `find-skills` на закрытом периметре НЕ ставится. Скиллы читаются из `~/.pi/agent/skills` пользователя, под которым запущен Pi: воркеру `agent-dev` их ставит `scripts/16` шаг 2b (до v3.12.2 их у него не было — Pi отвечал «скиллы не установлены»).
- AnythingLLM/LanceDB — только личные тетради; организационная БЗ — только rag-svc.
- Qdrant — только loopback, никогда не публиковать в LAN.
- Любое изменение → пересборка инсталлятора `build-installer.py <ver>` + `SHA256SUMS.txt`.

## 5. Быстрые входы

```bash
# состояние RAG (бэкенд, чанки, рекомендация по масштабированию)
curl -s localhost:8502/stats | jq
curl -s localhost:8502/health | jq

# синхронизация Qdrant из SQLite-зеркала БЕЗ переэмбеддинга
curl -s -X POST localhost:8502/reindex -H 'Content-Type: application/json' -d '{"use_existing_vectors":true}' | jq

# откат на SQLite
sudo sed -i 's/^RAG_BACKEND=.*/RAG_BACKEND=sqlite/' /etc/orchestrator.env && sudo systemctl restart rag-svc

# задачи оркестратора (в т.ч. гейт P4)
curl -s localhost:8500/api/tasks | jq '.[] | {id,template,status,current_step}'
curl -s localhost:8500/api/templates | jq -r '.[].name'   # должны быть code-fix/code-review/code-feature

# P4: демо-задача кодовой линии (после обновления кода оркестратора, см. §12c)
curl -s -X POST localhost:8500/api/tasks -H "X-Orch-Key: $(sudo grep -oP '(?<=^ORCH_API_KEY=).*' /etc/orchestrator.env)" \
  -H 'Content-Type: application/json' -d '{"template":"code-fix","payload":{"repo":"demo","text":"Добавить в calc.py функцию divide(a,b) с проверкой деления на ноль (ValueError) + тесты в test_calc.py"},"requester":"admin","group":"development"}' | jq

# гигиена логов (сначала dry-run)
sudo bash ~/spark-llm-stack/scripts/20-log-hygiene.sh
sudo bash ~/spark-llm-stack/scripts/20-log-hygiene.sh --apply
```

## 6. Открытые пункты (коротко)

1. P4: массово закрыть висячие гейты (`OPS-CARD.md` §3.7) → выложить v3.12.7 (§12c, порядок в `OPS-CARD.md` §5) → контрольная задача `code-fix` и проверка непустого `.diff` у reviewer при коммите роли → подключить реальный репозиторий (`/etc/agents-repos.list` + clone под `agent-dev`) → калибровка ролевых промптов. P4.1 (живые логи шагов + cancel в `/tasks/`) предложено, не сделано.
2. P6: собрать экспорт скиллов на Spark (`scripts/21`) и поставить на станцию с dsh; решить, нужен ли набор `code` вместо `all` (экономия системного промпта, `DEEPSEEK-HARNESS-GUIDE.md` §7.3).
3. P3: ждём чек-лист от DBA.
4. Гигиена: подтверждение `ENABLE_SIGNUP=false`, раздача `spark.crt` на рабочие места, калибровка `RAG_MIN_SCORE` на реальных скорингах bge-m3, золотой набор K12, ответственный за `supersede`.
5. Массовый импорт КР (`scripts/19`) — отдельным решением, после проверки на 1–2 специальностях.

## 7. Документы

`OPS-CARD.md` (ежедневная эксплуатация: чек, задачи P4, гейт, обновление, аварии) ·
`SKILLS-INVENTORY.md` (все скиллы трёх слоёв + экспорт в dsh) ·
`RAG-SCALE-PLAN.md` (измерения + решение по Qdrant) · `CHECKLIST.md` (K1–K14) ·
`INSTALL-RUNBOOK.md` (§12b: 12→17→18→19; §12c: горячая правка оркестратора) · `RAG-GUIDE.md` ·
`TUNING-GUIDE.md` · `AGENT-NETWORK-PLAN.md` (P1–P7) · `SKILLS-APPLIED.md` ·
`agents/dsh/DEEPSEEK-HARNESS-GUIDE.md` (§7 — скиллы в dsh) · `KNOWLEDGE-WIKI.md` · `MODEL-ANALYSIS.md`
