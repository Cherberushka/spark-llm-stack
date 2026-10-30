# OPS-CARD — как работать с системой каждый день

> Одна страница эксплуатации: проверить → создать задачу → решить гейт → обновить пакет →
> что делать при аварии. Подробности — `INSTALL-RUNBOOK.md`, текущее состояние — `STATE.md`.
> Все команды рассчитаны на вход под админом на Spark (`user@spark-7aac`), `sudo` есть.

## 0. Карта: кто где живёт

| Слой | Имя | Адрес | Проверка |
|---|---|---|---|
| systemd | `qwen38-llamacpp` | :8000 (LAN, ufw) | `curl -s localhost:8000/v1/models \| jq -r '.data[].id'` |
| systemd | `ollama` (bge-m3) | :11434 | `curl -s localhost:11434/api/tags \| jq -r '.models[].name'` |
| systemd | `agents-orchestrator` | **127.0.0.1:8500** | `curl -s localhost:8500/api/templates \| jq -r '.[].name'` |
| systemd | `rag-svc` | **127.0.0.1:8502** | `curl -s localhost:8502/stats \| jq -c .` |
| docker | `spark-litellm` | :4000 (LAN) | `curl -s localhost:4000/health/liveliness` |
| docker | `spark-litellm-db` / `-redis` | 127.0.0.1:5432 / внутри сети | `sudo docker ps --format '{{.Names}}\t{{.Status}}'` |
| docker | `open-webui` + `spark-caddy` | :443 (web, `/ops`), :8443 (LiteLLM UI) | браузер |
| docker | `qdrant` | **127.0.0.1:6333** — так задумано | `curl -s localhost:6333/collections \| jq -c .` |

Пути, которые трогают руками: `/opt/orchestrator/orchestrator.py` (исполняемый код),
`/etc/orchestrator.env` (root-only: `ORCH_API_KEY`, `CODE_WORKER_KEY`, `RAG_BACKEND`),
`/etc/spark-llm.env`, `/etc/agents-roles/*.md`, `/etc/agents-repos.list`,
`/srv/agents/repos/demo`, `/srv/agents/artifacts/task-25/`, `/srv/agents/kb-inbox`,
`~/spark-llm-stack` (копия пакета — из неё скрипты 11/12/16 перезаписывают `/opt` и юниты).

Браузер с рабочей машины: `https://192.168.11.251/` (чат), `https://192.168.11.251/ops`
(дашборд), `https://192.168.11.251:8443/ui` (админка LiteLLM), `\\192.168.11.251\doctor` (шара).
Qdrant на :6333 из LAN **не откроется** — это не поломка, публикация только на loopback.

## 1. Утренний чек (30 секунд)

```bash
sudo systemctl is-active qwen38-llamacpp ollama agents-orchestrator rag-svc
sudo docker ps --format '{{.Names}}\t{{.Status}}'
curl -s localhost:4000/health/liveliness; echo
curl -s localhost:8502/stats | jq -c .
curl -s localhost:8500/api/tasks | jq -c '.[] | select(.status!="done") | {id,template,status,current_step}'
```

`systemctl is-active litellm` — **неправильное имя**: LiteLLM в docker, не в systemd.

## 2. Борд задач в браузере (SSH-туннель)

Оркестратор слушает только loopback, роута `/tasks/*` в Caddy нет. С рабочей машины:

```bash
ssh -N -L 8500:127.0.0.1:8500 user@192.168.11.251
```

и открыть `http://localhost:8500/` — там список задач, шаги, кнопки **approve/reject**
(попросят `X-Orch-Key` — это `ORCH_API_KEY`, см. §7). Кнопка и curl ниже делают одно и то же.

## 3. Кодовая линия P4: цикл одной задачи

**3.1 Создать** (демо-репо `demo`; для своего репо — §3.6):

```bash
OKEY=$(sudo grep -oP '(?<=^ORCH_API_KEY=).*' /etc/orchestrator.env)
curl -s -X POST localhost:8500/api/tasks -H "X-Orch-Key: $OKEY" -H 'Content-Type: application/json' \
  -d '{"template":"code-fix","payload":{"repo":"demo","text":"Добавить в calc.py функцию sub(a,b) и тесты в test_calc.py"},"requester":"admin","group":"development"}' | jq -c '{id,status}'
```

**3.2 Смотреть** (шаги: implementer → qa → reviewer → гейт):

```bash
TID=26
curl -s localhost:8500/api/tasks/$TID | jq -c '{status:.task.status, steps:[.steps[]|{id,role,status}]}'
curl -s localhost:8500/api/tasks/$TID | jq -r '.events[-8:][] | "\(.level) \(.message)"'
sudo ls -la /srv/agents/artifacts/task-$TID/
```

**3.3 Гейт — читать в таком порядке:** `task-26-reviewer.diff` (что реально изменилось,
включая новые файлы) → `OUT-26-*-reviewer.md` → `OUT-26-*-qa.md` → `OUT-26-*-security.md`.
В `.diff` шапка: `# база задачи:`, `# изменено файлов:`, `# коммитов после базы:`,
`# неотслеживаемых файлов:`, при сдвигах HEAD — `# примечание:`, при сбоях git —
`# ВНИМАНИЕ: git вернул ошибки:`. База — ревизия на старте **задачи**: её держит
маркер `base-rev.txt` в каталоге артефактов, поэтому коммит роли внутри задачи не
обнуляет диф гейта.

```bash
sudo cat /srv/agents/artifacts/task-$TID/task-$TID-reviewer.diff | head -40
```

**3.4 Решить** (`SID` — id шага-гейта из 3.2; `approve` продолжает задачу, `reject` останавливает):

```bash
SID=42
curl -s -X POST localhost:8500/api/tasks/$TID/steps/$SID/decision -H "X-Orch-Key: $OKEY" \
  -H 'Content-Type: application/json' -d '{"decision":"approve","comment":"ок, диф прочитан"}' | jq -c .
```

**3.5 После approve оркестратор НИЧЕГО не коммитит и не мержит.** Правки остаются в рабочей
копии; ветка с именем по номеру задачи (`task-26` для задачи 26) создаётся автоматически.
Финал — руками, от владельца репо:

```bash
sudo -u agent-dev git -C /srv/agents/repos/demo status --short --branch
sudo -u agent-dev git -C /srv/agents/repos/demo add -A
sudo -u agent-dev git -C /srv/agents/repos/demo commit -m "task 26: sub(a,b) + tests"
```

**3.6 Свой репозиторий:**

```bash
echo 'myrepo git@git.local:clinic/myrepo.git' | sudo tee -a /etc/agents-repos.list
sudo -u agent-dev git clone git@git.local:clinic/myrepo.git /srv/agents/repos/myrepo
```

и в payload задачи `"repo":"myrepo"`.

**3.7 Массово закрыть висячие гейты (reject).** Сначала список — убедиться, что
закрываются те задачи, которые нужно:

```bash
OKEY=$(sudo grep -oP '(?<=^ORCH_API_KEY=).*' /etc/orchestrator.env)
curl -s "localhost:8500/api/tasks?status=awaiting_approval" | jq -r '.[] | "задача \(.id) · \(.template) · обновлена \(.updated_at)"'
for TID in $(curl -s "localhost:8500/api/tasks?status=awaiting_approval" | jq -r '.[].id'); do
  echo "$TID → гейт-шаг $(curl -s localhost:8500/api/tasks/$TID | jq -r '[.steps[]|select(.status=="awaiting_approval")][0].id')"
done
```

Только после этого — сам reject. Действие необратимо: шаг становится `failed`,
задача получает `failed` и результат «отклонено на gate», повторного гейта у неё
не будет.

```bash
for TID in $(curl -s "localhost:8500/api/tasks?status=awaiting_approval" | jq -r '.[].id'); do
  SID=$(curl -s localhost:8500/api/tasks/$TID | jq -r '[.steps[]|select(.status=="awaiting_approval")][0].id')
  echo "задача $TID шаг $SID: $(curl -s -X POST localhost:8500/api/tasks/$TID/steps/$SID/decision \
    -H "X-Orch-Key: $OKEY" -H 'Content-Type: application/json' \
    -d '{"decision":"reject","comment":"массовое закрытие: проверка завершена"}' | jq -c .)"
done
curl -s "localhost:8500/api/tasks?status=awaiting_approval" | jq 'length'   # ждём 0
```

Reject не трогает репозиторий: правки задач остаются в рабочей копии
`/srv/agents/repos/<repo>` — нужное закоммитить руками по 3.5. Задачи в
`running`/`queued` так закрыть нельзя: cancel в API нет, шаг прервётся по своему
`timeout_s`.

## 4. Документ в базу (RAG)

```bash
sudo bash ~/spark-llm-stack/scripts/13-add-doc.sh /srv/agents/kb-inbox/kr-2026.pdf "Какая доза при ХСН?"
curl -s -X POST localhost:8502/query -H 'Content-Type: application/json' \
  -d '{"q":"доза при ХСН","top_k":3,"mode":"hybrid"}' | jq -c '{top1_score,recommendation}'
```

Скрипт сам: кладёт файл в inbox → задача `doc-convert` (OCR для сканов) → индексация →
контрольный вопрос. `RAG_BACKEND` сейчас `qdrant`, источник истины — SQLite-зеркало;
синхронизация без переэмбеддинга: `POST /reindex {"use_existing_vectors":true}`.

## 5. Обновление пакета (порядок нарушать нельзя)

Копия `~/spark-llm-stack` **всегда** из последнего zip: скрипты 11/12/16 перезаписывают
`/opt/orchestrator/*` и юниты из неё, иначе фиксы откатываются.

```bash
cd ~ && sha256sum -c <(grep v3.12.6 SHA256SUMS.txt)     # совпало — распаковываем
cd ~ && rm -rf spark-llm-stack && python3 -c "import zipfile;zipfile.ZipFile('spark-llm-stack-installer-v3.12.6.zip').extractall('.')"
PROJ=~/spark-llm-stack
sudo install -m 644 "$PROJ/agents/orchestrator/orchestrator.py" /opt/orchestrator/orchestrator.py
sudo systemctl restart agents-orchestrator rag-svc
sudo bash "$PROJ/scripts/16-setup-code-line.sh"        # идемпотентен, в конце смоук Pi
curl -s localhost:8500/api/templates | jq -r '.[].name'
```

`unzip` на машине может отсутствовать — строка с `python3 -c` выше заменяет его.

## 6. Аварии

| Симптом | Первая команда | Дальше |
|---|---|---|
| чат молчит / 502 | `sudo journalctl -u qwen38-llamacpp -n 50 --no-pager` | RUNBOOK §13, fallback путь A/B |
| задачи висят, Pi не отвечает | `sudo journalctl -u agents-orchestrator -n 80 --no-pager` | смоук Pi из §12c (`env -i` + `cd` в репо) |
| `No models available` | `sudo bash ~/spark-llm-stack/scripts/fix-pi-no-models.sh` | `models.headless.json`, ключ `!cat /home/agent-dev/.llm_key` |
| гейт без `.diff` | `sudo -u agent-dev git -C /srv/agents/repos/demo diff HEAD \| head` | v3.12.5+: git от владельца репо, §12c |
| поиск пустой/медленный | `curl -s localhost:8502/stats \| jq -c .` | `RAG-SCALE-PLAN.md`, порог Qdrant ≥200k чанков или p95>500 мс |
| откат RAG на SQLite | `sudo sed -i 's/^RAG_BACKEND=.*/RAG_BACKEND=sqlite/' /etc/orchestrator.env && sudo systemctl restart rag-svc` | данные не теряются, SQLite — зеркало истины |
| память/диск | `df -h /`, `sudo docker system df` | `scripts/20-log-hygiene.sh` (сначала без `--apply`) |

## 7. Инварианты (нарушать нельзя)

1. `ORCH_API_KEY` и `CODE_WORKER_KEY` живут только в `/etc/orchestrator.env` (root). В чат,
   в репозиторий, в скриншоты — не попадают. Читать через `sudo grep`.
2. git оркестратор зовёт **от владельца репозитория**. `safe.directory` в gitconfig не пишем
   никогда — только разовый `-c safe.directory=...` как fallback внутри кода.
3. Qdrant — только `127.0.0.1:6333`. SQLite-зеркало остаётся источником истины.
4. Роли Pi работают от `agent-dev`: без sudo, доступ к своему home и `/srv/agents/repos`.
   Давать ему sudo или ключи от БД — нельзя.
5. Гейт человека — последний шаг всегда. Авто-merge не включаем.
6. `ENABLE_SIGNUP=false` в Open WebUI; `spark.crt` раздаётся на рабочие места.
7. Массовый импорт КР (`scripts/19`) — только отдельным решением, после пробы на 1–2 специальностях.
8. Любое изменение кода/доков → `python3 build-installer.py vX.Y.Z` на машине сборки + новый
   sha256 в `SHA256SUMS.txt`. Правки прямо в `/opt/orchestrator` живут до первого прогона scripts/11.
9. Ошибка git определяется **кодом возврата**, а не текстом stderr: git на русской локали пишет
   «Сброс ветки «task-N»» при успехе, и это не ошибка.
10. База дифа задачи — ревизия на старте задачи, её хранит `base-rev.txt` в
    `/srv/agents/artifacts/task-<id>/`. У живой задачи маркер руками не править и не удалять.
11. Скиллы в dsh переносятся только `scripts/21-export-dsh-skills.sh` (он приводит frontmatter
    к формату dsh). Руками в `~/.agents/skills` не править — правка уходит в источник
    (`vendor-skills/`, `/etc/agents-roles/`, `agents/dsh/skills-src/`) и пересобирается.
