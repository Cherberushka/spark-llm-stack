# SKILLS-INVENTORY — все скиллы проекта: где живут, что из них выросло, как перенести в dsh

Обновлено 30.10.2026. Источники фактов: `scripts/10-install-pi-skills.sh`,
`scripts/16-setup-code-line.sh`, `agents/orchestrator/orchestrator.py`,
`SKILLS-APPLIED.md`, `CHAT-RULES.md` (рабочая копия чата, не входит в пакет).

Скиллы в проекте живут в трёх слоях. Их часто путают, поэтому слои разделены явно:

| слой | где | как используется |
|---|---|---|
| **A. Скиллы-файлы** | `~/.pi/agent/skills/` у `agent-dev` (копии в `vendor-skills/`) | Pi читает индекс и подгружает тело по ходу задачи |
| **B. Скиллы-как-код** | роль-карточки `/etc/agents-roles/`, шаблоны задач в `orchestrator.py`, скрипты 00-21, runbook | правила скиллов **встроены** в промпты ролей и в код; как скиллы напрямую не вызываются |
| **C. Правила чата** | `CHAT-RULES.md` + `chat-skills/caveman/SKILL.md` | приняты вручную в диалоге Qwen ↔ пользователь (рантайма скиллов в чате нет) |

---

## A. Скиллы-файлы (16 шт., ставит `scripts/10-install-pi-skills.sh`, офлайн из `vendor-skills/`)

| имя у Pi | upstream | зачем в проекте | локальные зависимости |
|---|---|---|---|
| `mp-code-review` | mattpocock/skills · engineering/code-review | ревью diff по двум осям (стандарты / спецификация) | нет |
| `mp-diagnosing-bugs` | mattpocock/skills · engineering/diagnosing-bugs | цикл диагностики для troubleshooting-таблиц и разборов аварий | нет |
| `mp-tdd` | mattpocock/skills · engineering/tdd | тесты до кода; основа роли qa | нет |
| `mp-implement` | mattpocock/skills · engineering/implement | реализация по спецификации; основа роли implementer | нет |
| `mp-to-spec` | mattpocock/skills · engineering/to-spec | разговор → спецификация; основа planner-шага | нет |
| `mp-research` | mattpocock/skills · engineering/research | проверка фактов по первоисточникам (модель-карточки, блог vLLM, плейбук NVIDIA) | нет |
| `mp-handoff` | mattpocock/skills · productivity/handoff | передача контекста: STATE.md, формат диф-артефакта гейта | нет |
| `mp-writing-for-agents` | mattpocock/skills · productivity/writing-for-agents | структура INSTALL-RUNBOOK.md и OPS-CARD.md; основа роли docs | нет |
| `mp-wizard` | mattpocock/skills · engineering/wizard | `scripts/setup-wizard.sh` собран из его `template.sh` | bash |
| `ant-xlsx` | anthropics/skills · xlsx | отчёты/выгрузки МИС в Excel | python3 + openpyxl/pandas; LibreOffice для recalc |
| `ant-docx` | anthropics/skills · docx | Word-документы клиники | node + docx/markitdown; LibreOffice |
| `ant-pptx` | anthropics/skills · pptx | презентации/обучение | node + pptxgenjs/react-icons/sharp; LibreOffice |
| `ant-pdf` | anthropics/skills · pdf | PDF: чтение, сборка, OCR сканов | python3 + pypdf/pdfplumber; для OCR tesseract + pdf2image |
| `ant-doc-coauthoring` | anthropics/skills · doc-coauthoring | совместная работа над документом (процесс) | нет |
| `unlazy` | Leonxlnx/unlazy | gate «как выглядит готово», дерево глубины, доказательства чеками | нет |
| `taste-skill` | Leonxlnx/taste-skill (design-taste-frontend) | правила `stack/web/ops-dashboard.html` и борда задач | node 22+ и локальный Chrome; `assets.mjs` — интернет |

Опция `find-skills` (vercel-labs) доступна только при `FIND_SKILLS=1` и сети —
на закрытом периметре не ставится (см. `SKILLS-APPLIED.md` §3).

## B. Скиллы-как-код: что легло в основу субагентов оркестратора

Субагенты оркестратора — это **роли** (`agents_registry`) и **шаблоны задач**
(`SEED_TEMPLATES`). Прямых вызовов скиллов там нет: правила скиллов перенесены
в текст роль-карточек, в структуру шаблонов и в код. Соответствие:

| что в проекте | где живёт | из какого скилла выросло |
|---|---|---|
| роль `implementer` | `/etc/agents-roles/implementer.md` | `mp-implement` (минимальные правки по спеке) + принципы Karpathy (4 пункта, вшиты в текст) |
| роль `qa` | `/etc/agents-roles/qa.md` | `mp-tdd` (тесты обязательны) + `unlazy` (gate «как выглядит готово» дословно в карточке) |
| роль `reviewer` | `/etc/agents-roles/reviewer.md` | `mp-code-review` (вердикт APPROVE / REQUEST_CHANGES, замечания файл:строка) |
| роль `security` | `/etc/agents-roles/security.md` | собственный чек-лист (инъекции, секреты, CVE, path traversal); формат находок — по `mp-code-review` |
| роль `docs` | реестр оркестратора (карточки нет, P4 не развёрнут) | `mp-writing-for-agents` |
| роль `planner` | реестр + шаг `{"plan": true}` | `mp-to-spec` (декомпозиция разговора в граф шагов) |
| шаблоны `code-fix` / `code-review` / `code-feature` | `SEED_TEMPLATES` в `orchestrator.py` | `mp-code-review` (две оси) + `unlazy` (гейты до работы) + `mp-implement` |
| человеческий гейт и диф-артефакт `task-<id>-<role>.diff` | `w_pi` в `orchestrator.py` | `unlazy` (доказательство, а не «сделано») + `mp-handoff` (что читать следующему) |
| вердикты ролей `pi_verdict` (без «зелено по умолчанию») | `orchestrator.py` | `unlazy` |
| `scripts/setup-wizard.sh` | корень scripts | `mp-wizard` (стадии, прогресс, confirm-гейты, `write_env`) |
| INSTALL-RUNBOOK.md, OPS-CARD.md | корень проекта | `mp-writing-for-agents` (критерии готовности, progressive disclosure) + `mp-handoff` |
| таблицы Troubleshooting | INSTALL-RUNBOOK.md §13, гайды | `mp-diagnosing-bugs` (симптом → причина → лечение) |
| `stack/web/ops-dashboard.html`, борд `/tasks` | `stack/web`, `agents/orchestrator/web` | `taste-skill` → `references/app-screens.md` (цвет на данных, KPI-типографика, плоские поверхности, честные состояния) |
| проверка фактов о GB10/SM120/llama.cpp/vLLM | MODEL-ANALYSIS.md, §Источники | `mp-research` |
| роли `db-auditor` / `db-perf` / `db-migration` / `report` | реестр (P3, заглушки) | не из скиллов — собственные, под MSSQL и чек-лист DBA |

Отдельно: **принципы Karpathy** — не скилл из репозитория. Это четыре пункта
(не принимать молчаливых допущений, не усложнять, не трогать непонятное,
честная поверхность изменений), вшитые в три роль-карточки `scripts/16`.

## C. Правила чата (слой, который не устанавливается никуда)

| репозиторий | статус | что реально применяется |
|---|---|---|
| `JuliusBrussee/caveman` | принят целиком | стиль ответов; текст дословно в `chat-skills/caveman/SKILL.md` |
| `Leonxlnx/unlazy` | принят частично | дисциплина завершения: гейты до работы, доказательство чеками |
| `headroomlabs-ai/headroom` | не устанавливается | Rust-прокси/MCP для сжатия tool-выводов; вручную принят принцип «вывод команд ≤20 строк в чат, остальное в файлы» |
| `thedotmack/claude-mem` | не устанавливается | плагин на хуках для чужих харнессов; вручную принят принцип кэша состояния (`STATE.md`) |
| `Panniantong/Agent-Reach` | не принят | сетевой ридер соцсетей; на закрытом периметре не нужен |

---

## Экспорт в DeepSeek Harness (dsh)

Всё вышеперечисленное, что имеет смысл в dsh, собирается одной командой в
каталог, готовый к копированию в корень скиллов dsh:

```bash
cd ~/spark-llm-stack && bash scripts/21-export-dsh-skills.sh            # 22 скилла + zip + sha256
cd ~/spark-llm-stack && SET=code bash scripts/21-export-dsh-skills.sh   # только кодовый набор (11)
```

Состав экспорта (22): 16 скиллов слоя A под теми же именами + 6 новых.

| dsh-скилл (вызов) | из чего собран |
|---|---|
| `mp-code-review`, `mp-diagnosing-bugs`, `mp-tdd`, `mp-implement`, `mp-to-spec`, `mp-research`, `mp-handoff`, `mp-writing-for-agents`, `mp-wizard` | vendor-копии mattpocock, тела дословно |
| `ant-xlsx`, `ant-docx`, `ant-pptx`, `ant-pdf`, `ant-doc-coauthoring` | vendor-копии anthropics, тела дословно |
| `unlazy`, `taste-skill` | vendor-копии, тела дословно (у taste-skill имя каталога приведено к `taste-skill`, upstream-имя `design-taste-frontend` сохранено в `metadata`) |
| `spark-role-implementer`, `spark-role-qa`, `spark-role-reviewer`, `spark-role-security` | роль-карточки слоя B: текст из `/etc/agents-roles/*.md` (fallback — heredoc'и `scripts/16`), `user-invocable: true` |
| `spark-code-line` | процесс-скилл по `OPS-CARD.md` §3 и коду оркестратора: цикл задачи, порядок чтения артефактов, approve/reject, кто коммитит |
| `caveman` | слой C, `disable-model-invocation: true` — только явный `/caveman` |

Что в dsh перенести нельзя и почему:

* **роль-карточки — это не субагенты dsh.** dsh создаёт дочерних агентов
  инструментами `subagent`/`subagent_fork` и не читает `/etc/agents-roles`;
  поэтому роли экспортируются как скиллы-процедуры с явным вызовом `/имя`.
  Изолированный прогон — попросить модель вызвать `subagent` с текстом роли.
* `find-skills` — нужен интернет в момент использования.
* `headroom`, `claude-mem`, `Agent-Reach` — не скиллы-процедуры (библиотека/прокси,
  плагин на хуках чужих харнессов, сетевой ридер).
* полный клон mattpocock/skills (38 скиллов) — курируются 9; остальное ставится
  в Pi через `FULL=1` и в экспорт не входит.

Подробности установки, корни dsh, ограничения формата и грабли —
`agents/dsh/DEEPSEEK-HARNESS-GUIDE.md` §7.

## Гигиена

* После правки роль-карточек (`/etc/agents-roles/*.md`) экспорт собирается заново:
  `bash scripts/21-export-dsh-skills.sh` — тела скиллов `spark-role-*` обновятся.
* Раз в квартал (TUNING-GUIDE, шаг 6): удалить скиллы, которые никто не вызывал,
  и пересобрать экспорт.
* Тесты: `python3 research/test-dsh-export.py` (формат dsh, состав, install.sh),
  `python3 research/test-code-line-p4.py` (кодовая линия P4).
