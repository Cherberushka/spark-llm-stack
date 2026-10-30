# Кодовая линия P4 оркестратора задач (DGX Spark)

Процедура работы с оркестратором задач Spark: как устроен шаг, какие артефакты
читать у человеческого гейта, что делают approve и reject, кто коммитит.
Источники фактов: `agents/orchestrator/orchestrator.py`, `OPS-CARD.md` §2-§3,
`scripts/16-setup-code-line.sh`.

## Компоненты

| что | где |
|---|---|
| оркестратор (FastAPI, systemd `agents-orchestrator`) | `/opt/orchestrator/orchestrator.py`, слушает **только 127.0.0.1:8500** |
| борд задач (список, шаги, кнопки approve/reject) | `agents/orchestrator/web/tasks-dashboard.html` → `http://localhost:8500/` |
| ключ API | `ORCH_API_KEY` в `/etc/orchestrator.env` (читается только под sudo), заголовок `X-Orch-Key` |
| репозитории задач | `/srv/agents/repos/<имя>`, владелец `agent-dev`, список в `/etc/agents-repos.list` |
| артефакты задачи | `/srv/agents/artifacts/task-<id>/` |
| роль-карточки | `/etc/agents-roles/{implementer,qa,reviewer,security}.md` |
| исполнитель шага | Pi headless от `agent-dev` через `/usr/local/sbin/agent-run-pi.sh` |

Борд наружу не опубликован: доступ с рабочей машины —
`ssh -N -L 8500:127.0.0.1:8500 user@192.168.11.251`, затем `http://localhost:8500/`.

## Шаблоны кодовой линии

| шаблон | цепочка шагов |
|---|---|
| `code-fix` | implementer → qa → reviewer → **гейт человека** |
| `code-review` | security → reviewer → **гейт человека** |
| `code-feature` | planner → implementer → qa → reviewer → security → **гейт человека** |

## Цикл одной задачи

1. **Создать.** `POST /api/tasks` с `X-Orch-Key`, тело:
   `{"template":"code-fix","payload":{"repo":"demo","text":"что сделать"},
   "requester":"admin","group":"development"}` → `{"id":N}`.
2. **Наблюдать.** `GET /api/tasks/{id}` отдаёт `{task, steps, events}`; статус —
   в `.task.status` (не в корне ответа). `GET /api/tasks?status=awaiting_approval`
   — список висячих гейтов. Статусы: `running` → `awaiting_approval` → `done`
   или `failed`.
3. **Читать у гейта — в таком порядке:**
   `task-<id>-<role>.diff` (что реально изменилось) → `OUT-<id>-*-reviewer.md`
   → `OUT-<id>-*-qa.md` → `OUT-<id>-*-security.md`.
4. **Решить.** `POST /api/tasks/{tid}/steps/{sid}/decision`, тело
   `{"decision":"approve|reject","comment":"..."}`, где `sid` — id шага со
   статусом `awaiting_approval`.
   * `approve` → шаг `done`, задача продолжает цепочку и в конце становится `done`;
   * `reject` → шаг `failed`, задача `failed` с результатом «отклонено на gate».
     Решение необратимо: повторного гейта у этой задачи не будет.
5. **Закоммитить руками.** После approve оркестратор **ничего не коммитит и не
   мержит**; правки остаются в рабочей копии ветки `task-<id>`:
   `sudo -u agent-dev git -C /srv/agents/repos/<имя> status --short --branch`,
   затем `add -A` и `commit` от `agent-dev`.

## Артефакты задачи

| файл | что это |
|---|---|
| `TASK-<id>-<n>-<role>.md` | промпт шага (роль + задача + контекст цепочки, reviewer/security получают diff) |
| `OUT-<id>-<n>-<role>.md` | вывод роли Pi |
| `task-<id>-<role>.diff` | диф-артефакт гейта, пишется **всегда**, даже пустой |
| `base-rev.txt` | маркер базы задачи: путь репо + ревизия на старте задачи |

Шапка диф-артефакта:

```
# база задачи: <40hex>          # ревизия на старте ЗАДАЧИ, не шага
# роль: reviewer, шаг 2, задача 27
# примечание: HEAD на старте шага ... != база задачи   # только если роль коммитила
# изменено файлов: N             # считаем строки "diff --git "
# коммитов после базы: N         # git log <база>..HEAD
# ВНИМАНИЕ: git вернул ошибки:   # только при ненулевом коде возврата git
# неотслеживаемых файлов: N      # git diff их не видит — содержимое ниже в артефакте
```

Пустой диф — это факт для человека («роль ничего не изменила»), а не сбой
артефакта. Если `# изменено файлов: 0` при `# коммитов после базы: 0` и задача
была про правку кода — значит роль ничего не сделала: смотреть
`OUT-<id>-0-implementer.md` и код возврата Pi в `events`.

## Инварианты

* git зовётся от владельца репозитория (`sudo -n -u agent-dev git -C <repo>`,
  fallback — разовый `-c safe.directory=<repo>`): оркестратор работает от root,
  и git ≥ 2.35 на чужой репо отвечает `fatal: detected dubious ownership` с
  пустым stdout.
* Ошибка git определяется **кодом возврата**, а не текстом stderr: git пишет в
  stderr информационные сообщения, их текст зависит от локали.
* Роли reviewer и security — read-only: файлы не меняют.
* Вердикт без явной формулировки (`PASS`/`FAIL`, `APPROVE`/`REQUEST_CHANGES`)
  не считается положительным.
* Отменить `running`/`queued` задачу через API нельзя (нет cancel); шаг
  прерывается по своему `timeout_s`.
* Репозиторий задачи обязан лежать в `/srv/agents/repos` — выход за этот
  каталог отклоняется.
