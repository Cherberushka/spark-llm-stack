# SKILLS-APPLIED — какие скиллы собраны из предоставленных GitHub-баз и как они применены

> Полный инвентарь (включая правила, вшитые в роли оркестратора и в правила чата,
> и экспорт всего этого в DeepSeek Harness) — **`SKILLS-INVENTORY.md`**.
> Здесь — только разбор предоставленных GitHub-баз и сетевой аудит.

Перед выполнением задачи из указанных репозиториев собраны (склонированы в
`mattpocock-skills/`, `taste-skill/`) и изучены следующие скиллы. Здесь —
что именно взято в работу и где это видно в результатах.

## 1. mattpocock/skills — «Skills for Real Engineers»

| Скилл | Путь в репо | Что дал задаче |
|---|---|---|
| **wizard** | `skills/engineering/wizard/` (+ `template.sh`) | Прямо по назначению: «интерактивный bash-мастер для provisioning инфраструктуры». `scripts/setup-wizard.sh` собран из **нетронутой библиотеки** `template.sh` (стадии, прогресс, confirm-гейты, `write_env`, `open_url`, финальное summary) + авторские стадии под наш стек. Соблюдены принципы скилла: подтверждение перед необратимыми действиями (ufw, загрузки ~55 ГБ), идемпотентный `.env`-upsert, статическая проверка `bash -n` вместо сквозного прогона. |
| **writing-for-agents** | `skills/productivity/writing-for-agents/` | Структура `INSTALL-RUNBOOK.md`: шаги с явными **критериями готовности** («Критерий готовности: …»), progressive disclosure (основной путь — в теле; альтернативы вынесены в Приложения А/Б; шпаргалка — В), co-location (всё про память — в одном разделе, всё про ключи — в одном), борьба со sprawl. |
| **research** | `skills/engineering/research/` | Метод проверки фактов по первоисточникам до написания инструкций: модели-карточки HF (включая секции Test Hardware → «NVIDIA DGX Spark», Software Integration → SM120/SM121, точные vLLM/llama.cpp-рецепты), официальный блог vLLM про DGX Spark, NVIDIA playbook build.nvidia.com/spark. Все утверждения в runbook со ссылками на источники (§ Источники). |
| **handoff** | `skills/productivity/handoff/` | Формат передачи: README как точка входа, runbook самодостаточен для следующего админа/агента, чувствительные значения (API-ключи) не хранятся в репозитории — только генерация на машине. |
| code-review / diagnosing-bugs | `skills/engineering/…` | Подход таблицы Troubleshooting: симптом → причина → конкретное лечение (диагностический цикл, а не «погуглите»). |

## 2. Leonxlnx/taste-skill — «design-taste-frontend» (tasteskill v2)

| Часть скилла | Путь в репо | Что дало задаче |
|---|---|---|
| **App screens run** (dashboards/admin) | `skills/taste-skill/references/app-screens.md` | Правила, по которым построен `stack/web/ops-dashboard.html`: **цвет на данных, а не на хроме** (единственный data-hue — зелёный NVIDIA #76B900 только на статусах/данных); **KPI — типографика, а не «плитки»** (никаких иконок в цветных кружках и спарклайнов); **плоские поверхности** (1px линейки, без теней); **без hero-заголовков**; **таблицы — линейки и выравнивание** (без зебры, числа справа, таблярные цифры); **честные состояния** (прочерк «—» вместо выдуманных значений, ноль — это ноль, цвет статуса = хорошо/плохо, а не вверх/вниз); **лицо продукта** (имя «Spark Ops», монограмма-молния в марке, scope-строка «DGX Spark · GB10 · 128 GB unified», профессиональные слова в подписях: Resident / Serving / Deferred); **первый экран отвечает на главный вопрос** («обе модели живы и раздают?») и **главное действие в один клик** («Open chat ↗» в шапке); всё помещается в 1440×900 без прокрутки. |
| House style / no-slop | `skills/taste-skill/SKILL.md` | Никаких градиентов, glow-эффектов, иконок в тонированных квадратах, радужных графиков и vanity-метрик; системный шрифтовой стек + моноширинный для чисел; без внешних ресурсов (дашборд работает офлайн на LAN). |

## Как это связано с задачей

1. **Сбор фактов (research)** → проверенные первоисточники: обе модели-карточки,
   GGUF-рецепты NVIDIA для Spark 128 ГБ, vLLM-блог про DGX Spark, плейбук NVIDIA
   про Open WebUI на Spark. Никаких «придуманных» флагов: команды llama-server —
   дословный официальный рецепт; vLLM — дословный рецепт карточки + осознанные
   поправки под единый пул памяти Spark (задокументированы в Приложении А).
2. **Проектирование поставки (wizard)** → установка оформлена как нумерованные
   идемпотентные скрипты 00–07 + интерактивный мастер поверх них — ровно паттерн
   «wizard для provisioning инфраструктуры» из mattpocock/skills.
3. **Документация (writing-for-agents, handoff)** → runbook со шагами, критериями
   готовности и прогрессивным раскрытием; читается и человеком, и агентом.
4. **Веб-интерфейс (taste-skill app-screens)** → готовая админка Open WebUI
   (функционал) + собственный ops-дашборд, построенный по измеримым правилам
   app-screens, а не «среднему шаблону дашборда».

## Воспроизвести сбор скиллов

```bash
git clone --depth 1 https://github.com/mattpocock/skills.git mattpocock-skills
git clone --depth 1 https://github.com/leonxlnx/taste-skill.git taste-skill
# ключевые файлы:
#   mattpocock-skills/skills/engineering/wizard/{SKILL.md,template.sh}
#   mattpocock-skills/skills/productivity/writing-for-agents/SKILL.md
#   mattpocock-skills/skills/engineering/research/SKILL.md
#   taste-skill/skills/taste-skill/SKILL.md
#   taste-skill/skills/taste-skill/references/app-screens.md
```

Оба клона лежат рядом в `dgx-spark-project/` — можно читать первоисточники.

## 3. Сетевой аудит скиллов (проведён 2026-10-28)

Все 16 курируемых скиллов проверены grep-сканированием на сетевые вызовы
(`https?://`, `curl/wget`, `npx/npm install`, `pip install`, `fetch/requests/urllib`)
по свежим копиям репозиториев. Вердикт по каждому:

| Скилл | Сеть в рантайме | Комментарии |
|---|---|---|
| mp-code-review, mp-diagnosing-bugs, mp-tdd, mp-research, mp-to-spec, mp-implement, mp-handoff, mp-writing-for-agents | **не нужна** | Чистый markdown. Единственные `http`-совпадения — примеры кода в документации (`fetch('/users/…')` в mocking.md, `localhost:3000` в шаблоне HITL-цикла) — агент их не исполняет. |
| mp-wizard | **не нужна** | `template.sh` содержит `open_url "https://dashboard.stripe.com/…"` — срабатывает только если пользователь сам запускает Stripe-шаблон; наш `setup-wizard.sh` внешних URL не открывает. |
| unlazy | **не нужна** | Один SKILL.md, чистый текст-чеклист. Ссылки на arXiv — только в README (цитаты исследований). |
| taste-skill (ядро: draw/lint/inspect/scenes) | **не нужна** | Локальный Node 22+ и локальный Chrome/Edge/Chromium; inspect/scenes поднимают сервер на `127.0.0.1`. Требует установленный браузер, не интернет. |
| taste-skill → `scripts/assets.mjs` | **НУЖНА** | Поиск стоковых фото/видео через API Openverse/Wikimedia/NASA (без ключей). Сам SKILL.md это декларирует («assets.mjs also needs network access»). Функция опциональна (шаг 6) — без интернета скилл работает, фото-поиск недоступен. |
| taste-skill → генерируемые страницы | деградируют офлайн | `app-draw.mjs` вставляет ссылку на Google Fonts, `draw.mjs` — плейсхолдеры picsum.photos. Без интернета шрифты откатятся к системным, плейсхолдеры не загрузятся — на сам скилл не влияет. |
| ant-xlsx, ant-docx, ant-pptx | **не нужна** | Скрипты работают локально (openpyxl/pandas/markitdown; npm docx/pptxgenjs/react-icons/sharp; LibreOffice `soffice` для recalc/accept-changes). `http`-совпадения — XML-namespace'ы OOXML-схем, не обращения в сеть. **Внимание:** fallback в их SKILL.md («если import упал — pip/npm install») требует интернет или локальное зеркало; на Spark зависимости надо ставить заранее. |
| ant-pdf | **не нужна** | pypdf/pdfplumber локально. Для OCR-ветки нужны `pytesseract pdf2image` + бинарь tesseract (ставятся один раз локально). |
| ant-doc-coauthoring | **не нужна** | Чистый markdown-процесс; единственная ссылка (claude.ai) — упоминание в тексте, не вызов. |
| find-skills (опция FIND_SKILLS=1) | **НУЖНА в момент использования** | `npx skills find` обращается к реестру skills.sh. На закрытом периметре не ставить — уже задокументировано в скрипте 10. |

### Установка скиллов офлайн (v3.9.5+)

Раньше скрипт `10-install-pi-skills.sh` требовал интернет в момент установки
(`git clone` ×4). Теперь в инсталлятор входит `vendor-skills/` — полные офлайн-копии
всех курируемых скиллов, и скрипт:

- при `OFFLINE=1` ставит строго из вендорских копий (проверено: 16/16 без сети);
- при наличии сети clone'ует свежие версии, а при неудаче clone автоматически
  падает на вендорские копии;
- `FULL=1` (весь репозиторий mattpocock) и `FIND_SKILLS=1` — по-прежнему только с сетью.

```bash
cd ~/spark-llm-stack && OFFLINE=1 bash scripts/10-install-pi-skills.sh
```

### Контроль на Spark (копия аудита по фактически установленным скиллам)

```bash
grep -rIl -E "curl |wget |npx |requests\.get|urllib|fetch\(['\"]https?" ~/.pi/agent/skills/ || echo "исполняемых сетевых вызовов нет"
```

Итог для периметра клиники: **в рантайме интернет требуется только двум опциональным
вещам** — `find-skills` (не ставим) и `assets.mjs` из taste-skill (фото-поиск;
для медицинских отчётов не используется). Всё остальное работает полностью локально.
