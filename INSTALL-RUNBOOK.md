# Локальный LLM-стек на NVIDIA DGX Spark — полный runbook системного администратора

**Назначение организации:** агентный кодинг (Pi-агент) и информационный
оптимизатор частной клиники — составление отчётов, преобразование документов,
поиск по документации (RAG), работа с БД (Text-to-SQL).

> **Состав стека (решение заказчика от 22.10.2026): одна чат-модель — Qwen3.8-27B.
> Nemotron исключён полностью** (не поддерживает русский язык; Qwen3.8-27B
> закрывает оба сценария с лучшим качеством — см. MODEL-ANALYSIS.md).

| Роль | Модель | Движок | Скорость (измерено NVIDIA на DGX Spark) |
|---|---|---|---|
| **Единственная чат-модель**: кодинг, документы, отчёты, зрение, tools | **Qwen3.8-27B NVFP4** + DFlash2-драфтер | vLLM :8000 (официальный рецепт NVIDIA) | **37,3 tok/s**, concurrency 8, ctx 262K |
| **Эмбеддинги RAG** | bge-m3 (100+ языков, RU+EN) | Ollama :11434 | — |
| **Единый API** | переключение/ключи/логи | LiteLLM :4000 | ключи отделов, лимиты |
| **Веб** | чат + админ-панель + Knowledge/RAG | Open WebUI за Caddy :443 | `/ops`-дашборд |

> **Быстрый старт:** `sudo bash scripts/setup-wizard.sh` (6 стадий с подтверждениями).
> RAG для клиники — **[RAG-GUIDE.md](RAG-GUIDE.md)**. Обоснование выбора модели — **[MODEL-ANALYSIS.md](MODEL-ANALYSIS.md)**.

---

## 1. Архитектура и принятые решения

```
  Pi (рабочие станции) · браузеры · интеграции МИС · cron-отчёты
        │  один OpenAI-совместимый API (поле "model")
        ▼
 ┌────────────────────────────┐
 │ LiteLLM Gateway :4000      │  вирт. ключи отделов, лимиты, логи, retries
 └──────────┬─────────────────┘
            ▼
      vLLM :8000 ── Qwen3.8-27B NVFP4 + DFlash2
      37,3 tok/s · ctx 262K · 8 users · tools(qwen3_xml) · reasoning(qwen3)
            │
      Ollama :11434 ── bge-m3 (эмбеддинги RAG)
            │
   Open WebUI (чат+админка+Knowledge) ← Caddy :443 → /ops-дашборд
        ── всё в едином пуле 128 ГБ LPDDR5x (GB10, sm_121) ──
```

| Решение | Обоснование |
|---|---|
| **Qwen3.8-27B — единственная чат-модель** | Terminal-Bench 2.1: 73,0 (лучший локальный агентный кодинг); OmniDocBench 91,1 (преобразование документов); CoWorkBench 70,7 + IFBench 79,5 (отчёты/форматы); OSWorld 84,3 (автоматизация МИС); зрение; 262K ctx; русский язык (Qwen3-линейка ~119 языков); Apache 2.0 |
| **Nemotron исключён** | Решение заказчика. Технические причины согласуются: RU не в списке языков Bolt; по агентным/документным бенчмаркам Qwen3.8-27B впереди; один движок = проще администрирование и больше свободной памяти |
| Движок — **vLLM NVFP4 по официальному рецепту NVIDIA** | Каталог `nvidia/local-ai-inference-recipes` (валидирован на DGX Spark 19.10.2026): 37,3 tok/s — вдвое быстрее GGUF-пути на Spark; нативный FP4 Blackwell; concurrency 8 (несколько сотрудников); встроенные парсеры tools/reasoning |
| **LiteLLM** — единый API | Один эндпоинт для Pi, интеграций МИС, cron-отчётов; виртуальные ключи отделов с лимитами и отзывом; журнал всех запросов (аудит) |
| **Pi** — агент-харнесс | Выбор заказчика; `agents/pi/models.json` → шлюз; thinking off/low в циклах |
| RAG: **bge-m3 + Open WebUI Knowledge** | Мультиязычные эмбеддинги (RU↔EN кросс-лингвально), встроенное векторное хранилище, цитаты, группы доступа; рост → Qdrant + reranker (RAG-GUIDE §7) |

**Порт-карта:** 443/80 — Caddy (наружу, со своей аутентификацией); 4000
(LiteLLM), 8000 (vLLM), 11434 (Ollama) — только подсети организации
192.168.8.0–192.168.11.255 (ufw для хостового 11434; DOCKER-USER для
проброшенных портов контейнеров — Docker NAT минует ufw, см. §4).

## 2. Требования и ресурсный бюджет

**Железо:** DGX Spark — GB10 Grace Blackwell (sm_121), **128 ГБ** LPDDR5x
(~273 ГБ/с), DGX OS (Ubuntu 24.04, aarch64). Рецепт NVIDIA валидирован на:
Ubuntu 24.04.4, драйвер 580.126.20, CUDA 13.4 — держите DGX OS обновлённым.

**Диск (≥ 90 ГБ свободно):** образ vLLM ~20–25 ГБ; веса NVFP4 + DFlash2
(HF-кэш) ~18–20 ГБ; веб-образы (LiteLLM, Open WebUI, Caddy) ~6–8 ГБ;
Ollama + bge-m3 ~2 ГБ; логи/бэкапы — запас.

**Память (единый пул 128 ГБ):**

| Компонент | Путь A (llama.cpp) | Путь B (vLLM NVFP4) |
|---|---|---|
| Движок Qwen3.8-27B | ~22–26 ГБ (веса 17,6 + драфтер 1,1 + mmproj 0,9 + KV на 160K) | ~77 ГБ (`util 0.60`: веса ~14 + драфтер ~4 + fp8-KV 262K×8) |
| ОС + Docker + веб + LiteLLM | ~10 ГБ | ~10 ГБ |
| Ollama + bge-m3 | ~1,5 ГБ | ~1,5 ГБ |
| **Итого** | **~35–40 ГБ** (запас ~90 ГБ) | **~88–90 ГБ** (запас ~38 ГБ) |

⚠ Рецепт NVIDIA: **не поднимать util выше 0.60–0.70** (при 0.85 узел зависал
на autotune). Скрипт 03 использует 0.60.

## 3. Стадия 1 — Preflight

```bash
sudo bash scripts/00-check-prereqs.sh
```
aarch64; GB10; ≥110 ГБ памяти; диск; Docker + **NVIDIA container runtime**
(обязателен для vLLM); сеть: ollama.com, huggingface.co, github.com, ghcr.io,
**nvcr.io**. nvcc — только информация (нужен лишь для fallback из Приложения А).
**Критерий:** `Preflight PASSED`.

## 4. Стадия 2 — Подготовка DGX OS

```bash
sudo bash scripts/01-prepare-system.sh
# подсети организации можно переопределить:
# LAN_NETS="192.168.8.0/24 192.168.9.0/24 192.168.10.0/24 192.168.11.0/24" sudo -E bash scripts/01-...
```
Пакеты (jq, nvtop, python3-venv, ufw…); каталоги `/opt/models`, `/opt/hf-venv`
(HF CLI); **файрвол под подсети организации 192.168.8.0 – 192.168.11.255**
(по умолчанию `LAN_NETS=192.168.8.0/22` — ровно четыре /24, x = 8..11).

Защита двухуровневая (важный нюанс Docker):
1. **ufw (INPUT)** — хостовые сервисы: SSH + 80/443 (веб, со своей аутентификацией)
   открыты; **11434 (Ollama)** — только подсети организации + docker-мосты.
2. **DOCKER-USER (iptables)** — проброшенные порты контейнеров (vLLM **8000**,
   LiteLLM **4000**) проходят через NAT/FORWARD и **минуют ufw** — их ограничивает
   цепочка DOCKER-USER: разрешены только `192.168.8.0/22` и docker-подсети,
   остальным DROP. Правила применяют юнит `spark-docker-user.service`
   (переживает перезагрузки/docker-restart), конфиг — `/etc/default/spark-firewall`.

**Критерий:** `ufw status numbered` + `iptables -L DOCKER-USER -n` показывают
правила; с машины вне 192.168.8–11 `curl http://<IP>:4000/health/liveliness`
не отвечает, из подсетей организации — отвечает.

> Смена подсетей позже: правите `LAN_NETS` в `/etc/default/spark-firewall` →
> `systemctl restart spark-docker-user` и при необходимости `ufw`-правила 11434.

## 5. Стадия 3 — Ollama + bge-m3 (эмбеддинги RAG)

```bash
sudo bash scripts/02-install-ollama-embeddings.sh
```
Ollama обслуживает только эмбеддинги: bge-m3 (~1,2 ГБ), `OLLAMA_KEEP_ALIVE=-1`
(резидентно — мгновенные RAG-запросы). Смоук-тест: вектор 1024 dim на русской
фразе. **Критерий:** `✓ bge-m3 embeddings work`.

## 6. Стадия 4 — Qwen3.8-27B: два официальных пути NVIDIA

Оба пути дают один контракт для остального стека: **OpenAI-совместимый API на
:8000, Bearer-ключ `VLLM_API_KEY` из `/etc/spark-llm.env`, имя модели в
запросах — `qwen3.8-27b`**. LiteLLM/Open WebUI/Pi не замечают разницы.

> ⚠ **Не используйте Docker Hub `vllm/vllm-openai:*` с флагами рецепта.**
> Патчи NVIDIA (PR #54614 `nvfp4_dynamic`, #59021, #59025, #59244, #60252)
> на 22.10.2026 **не влиты в upstream** (open PRs) — upstream-образ падает на
> `--kernel-config` (literal_error: nvfp4_dynamic), а DFlash+prefix-caching
> там может портить вывод (#60174 не исправлен). Отсюда два рабочих пути:

### 6.1. Путь A (по умолчанию, работает из РФ без NGC) — llama.cpp

```bash
sudo bash scripts/03b-deploy-qwen38-llamacpp.sh
```
Реализует официальный llama.cpp-рецепт NVIDIA для Qwen3.8-27B
(`models/qwen/qwen3.8-27b/geforce-rtx/.../vision-dflash2/recipe.yaml`,
llama.cpp b11574; на GeForce замерено 159,6 tok/s → на Spark ожидаем
**~25–30 tok/s**):
- сборка llama.cpp master (CUDA sm_121) в `/opt/llama.cpp`;
- файлы через HF-зеркало (`HF_ENDPOINT=https://hf-mirror.com` по умолчанию):
  `unsloth/Qwen3.8-27B-GGUF` → **UD-Q4_K_XL** (17,6 ГБ; выбор кванта — §1) +
  `mmproj-F16` (зрение) + `incoai/Qwen3.8-27B-DFlash2-GGUF` → Q4_K_M (драфтер
  1,1 ГБ; fallback-зеркало z-lab);
- systemd-юнит `qwen38-llamacpp` на :8000: `--spec-type draft-dflash
  --spec-draft-n-max 7 --backend-sampling --ctx-size 163840 --jinja --metrics
  --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0` + `--reasoning-budget 0`
  (thinking выключен на уровне движка — агентный режим; включается
  `REASONING_BUDGET=-1` или per-request `chat_template_kwargs`);
- ctx 163840 дёшев по памяти: у Qwen3.8 гибридное внимание (GDN/линейные
  слои) — KV-кэш маленький; footprint движка ~22–26 ГБ.

**Критерий:** `curl -s http://127.0.0.1:8000/health -H "Authorization: Bearer
$(sudo grep -oP '(?<=^VLLM_API_KEY=).*' /etc/spark-llm.env)"` → 200.

### 6.2. Путь B (точный рецепт, 37,3 tok/s) — vLLM NVFP4 с NGC-образом

```bash
sudo bash scripts/03-deploy-qwen38-vllm.sh
```
Образ `nvcr.io/nvidia/vllm:26.10.pre1-py3` (единственный, где есть патчи),
чекпоинт `nvidia/Qwen3.8-27B-NVFP4` + драфтер `z-lab/Qwen3.8-27B-DFlash2`,
флаги рецепта дословно: fp8 KV, 262K ctx, 8 seq, `nvfp4_dynamic`, DFlash
7 токенов, парсеры `--reasoning-parser qwen3 --tool-call-parser qwen3_xml`,
`--gpu-memory-utilization 0.60` (0.85 вешал узел — предупреждение NVIDIA),
монтируется **только** `~/.cache/huggingface`, `HF_ENDPOINT` прокидывается в
контейнер. Измерено NVIDIA на DGX Spark: **37,3 tok/s**.

**NGC из РФ недоступен** → образ импортируется офлайн (Приложение А.1):
pull на машине с VPN → `docker save` → перенос → `docker load` → скрипт 03
подхватит локальный образ.

**Критерий:** `/health` → 200; `/v1/models` → `qwen3.8-27b`.

## 7. Стадия 5 — Шлюз + веб-слой (LiteLLM, Open WebUI, Caddy)

```bash
sudo bash scripts/04-deploy-web.sh
```
- **LiteLLM** :4000 (`stack/litellm/config.yaml`): модели `qwen3.8-27b` и
  `bge-m3`; мастер-ключ из `/etc/spark-llm.env`; виртуальные ключи отделов —
  в UI шлюза (`https://<host>:8443/ui`) или `POST /key/generate`.
- **Open WebUI**: чат-модели и RAG-эмбеддинги — через шлюз (env контейнера).
- **Caddy** :443: `/` (чат/админка), `/ops`, `/gateway/*`, `/vllm/*`, `/ollama/*`.

- **Redis 7** (`spark-litellm-redis`, ephemeral) — общие для воркеров LiteLLM
  счётчики лимитов/бюджетов и кэш роутера (без него при `--num_workers 2`
  лимиты применяются «на воркер» и баннер «No Redis configured»);
- **PostgreSQL 16** (`spark-litellm-db`, том `litellm-db-data`) — хранилище
  LiteLLM: виртуальные ключи, бюджеты, spend-логи. Без БД шлюз проксирует
  запросы, но админка `:8443/ui` не работает («Not connected to DB!»).
  Пароль `LITELLM_DB_PASSWORD` генерирует скрипт 04 в `/etc/spark-llm.env`.

**Критерий:** `docker compose ps` — все healthy (включая `spark-litellm-db`);
`https://<IP>/` и `https://<IP>/ops` открываются; вход в `https://<IP>:8443/ui`
с мастер-ключом работает.

## 8. Единый API: использование, thinking-режимы, Pi

```bash
GW=http://<IP-SPARK>:4000/v1
KEY=$(sudo grep -oP '(?<=^LITELLM_MASTER_KEY=).*' /etc/spark-llm.env)

curl $GW/models -H "Authorization: Bearer $KEY"        # qwen3.8-27b, bge-m3

# Чат (агентный режим — thinking ВЫКЛ, быстрее и дешевле):
curl $GW/chat/completions -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "model": "qwen3.8-27b",
  "messages": [{"role":"user","content":"Составь SQL: число приёмов по врачам за месяц"}],
  "chat_template_kwargs": {"enable_thinking": false}
}'
# Зрение (скан документа): content: [{"type":"text",...},{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]
# Эмбеддинги:  curl $GW/embeddings -d '{"model":"bge-m3","input":"текст"}'
```

**Thinking-режим Qwen3.8** (экономия токенов):
- по умолчанию thinking **включён** (`reasoning_effort: xhigh`);
- выключить: `"chat_template_kwargs": {"enable_thinking": false}`;
- уровни глубины: `{"reasoning_effort": "low" | "medium" | "xhigh"}`;
- практика: агентные циклы — off/low (экономия 40–60% токенов при <1% потерь);
  сложные одиночные задачи (архитектура, трудный дебаг, ответственные отчёты) — xhigh.

**Ключи:** мастер-ключ — только админ; отделам/сервисам — виртуальные ключи
LiteLLM (лимиты, отзыв, видность в логах).

**Pi-агент** (рабочие станции): `bash scripts/06-setup-pi.sh` →
`pi --model spark/qwen3.8-27b` (Pi 1.x: провайдер/модель через слэш; подробности — `agents/pi/README.md`).

## 9. Стадия 6 — Проверка

```bash
sudo bash scripts/05-verify.sh
```
Шесть групп тестов: эмбеддинги bge-m3; текст Qwen3.8 (thinking off) + скорость;
**зрение** (тестовый PNG намеренно 64×64 — Qwen3.8 vision отклоняет изображения
мельче минимума патча токенизатора 28×28/56×56 с `ValueError: image size too
small` → HTTP 500); **проба длинного контекста** (~2900 токенов с маркером в
конце — ловит тихое обрезание на 2048 из-за `model_max_length` в
tokenizer_config.json некоторых сборок NVFP4); шлюз LiteLLM (чат + эмбеддинги);
фронт-дверь Caddy, `/ops`, `/gateway`.
**Критерий:** `RESULT: N passed, 0 failed`. Ориентир скорости: 35–40 tok/s
(измерение NVIDIA 37,3); первый токен на очень длинном промпте — секунды
(префилл), на 262K — минуты.

## 10. Админка Open WebUI

1. `https://<IP>/` → **первая регистрация = админ**. Затем `ENABLE_SIGNUP=false`
   в `stack/.env` → `cd stack && docker compose up -d`.
2. Модель из шлюза переименуйте в *Admin → Settings → Models*:
   «Qwen3.8 27B — основная»; назначьте дефолтной.
3. **Группы и права**: `Врачи`, `Регистратура`, `Разработка`, `Администрация` —
   разным группам разные базы знаний (Knowledge) и права.
4. **RAG/Knowledge**: эмбеддер bge-m3 уже подключён (env контейнера); настройка,
   загрузка документов и регламент обновления КР — **[RAG-GUIDE.md](RAG-GUIDE.md)**.
5. **Логи/Usage**: журнал запросов (аудит); **Arena** — эксперименты A/B.
6. **Tools** для БД: *Workspace → Tools* — Text-to-SQL функция с read-only
   подключением к МИС (схема — RAG-GUIDE §8).

## 11. Эксплуатация

```bash
docker ps                                 # vllm-qwen38, spark-litellm, open-webui, spark-caddy
docker logs -f vllm-qwen38                # основной движок
docker compose -f stack/docker-compose.yml logs -f
journalctl -u ollama -f                   # эмбеддинги
nvtop                                     # память/GPU в реальном времени
https://<host>/ops                        # веб-дашборд
docker restart vllm-qwen38                # перезапуск движка
cd stack && docker compose restart        # веб-слой
# Prometheus: vLLM :8000/metrics (Bearer VLLM_API_KEY)
```

**Бэкап** (состояние, не веса): том `spark-llm-web_open-webui-data` (пользователи,
чаты, **векторные индексы Knowledge**), том `spark-llm-web_litellm-db-data`
(виртуальные ключи и spend-логи; `docker exec spark-litellm-db pg_dump -U llmadmin litellm > litellm-$(date +%F).sql`), `/etc/spark-llm.env`, `stack/.env`,
`stack/litellm/config.yaml`, `stack/caddy/Caddyfile`:

```bash
docker run --rm -v spark-llm-web_open-webui-data:/data -v /root/backup:/backup \
  alpine tar czf /backup/open-webui-$(date +%F).tgz -C /data .
tar czf /root/backup/config-$(date +%F).tgz /etc/spark-llm.env <путь>/dgx-spark-project/stack
```
Cron ежедневно 03:30. Веса восстанавливаются скриптами 02/03.

**Обновления:** DGX OS — `apt full-upgrade` (сверяйте драйвер/CUDA с
валидированными в рецепте: 580.126.20 / 13.4); vLLM-образ — пин
`26.10.pre1-py3`, обновлять осознанно (проверить, что рецепт не сломан);
веб-слой — `docker compose pull && up -d`; Ollama — переустановочный скрипт;
bge-m3 — `ollama pull bge-m3`.

## 12. Безопасность

- **Периметр (подсети организации 192.168.8–11):** ufw — SSH/80/443 всем,
  11434 только org-LAN + docker; DOCKER-USER — API-порты 8000/4000 только
  org-LAN (Docker NAT минует ufw, поэтому отдельная цепочка; юнит
  `spark-docker-user.service`, конфиг `/etc/default/spark-firewall`).
  Наружу organization-периметра — только через VPN.
- Все API под ключами (`/etc/spark-llm.env`, 600, root). Отделам — виртуальные
  ключи LiteLLM; ротация: новые ключи → `/etc/spark-llm.env` → `docker restart
  vllm-qwen38` + `cd stack && docker compose up -d`.
- HTTPS: статический самоподписанный сертификат `stack/caddy/certs/spark.crt`
  (SAN: localhost, hostname, 127.0.0.1, LAN IP; срок 10 лет). Доверие на
  клиентах: импортировать spark.crt в корневые (Windows: «Доверенные корневые
  ЦС»; Linux: /usr/local/share/ca-certificates + update-ca-certificates).
  Для домена + Let's Encrypt: убрать строку `tls …` в Caddyfile и задать
  SITE_ADDRESS=llm.corp.local; для корпоративного CA — заменить файлы сертификата. Pi ходит по `http://<IP>:4000/v1` (LAN-restricted) или
  `https://<IP>/gateway/v1` (если CA добавлен в доверенные на workstation).
- Медданные: периметр локальный; ПДн пациентов — не в общие Knowledge-базы
  (RAG-GUIDE §3/§9); аудит-логи ≥6 мес.
- `ENABLE_SIGNUP=false` после заведения учётных записей; роли минимальные.
- **LiteLLM UI**: заведите персональную админ-учётку (Internal Users → Admin,
  свой пароль), проверьте вход ею, затем в `stack/litellm/config.yaml`
  добавьте `disable_env_credential_login: true` и `docker compose restart litellm`
  — вход по мастер-ключу/env в UI закроется (для API мастер-ключ останется).

## 12a. Оркестратор: повторный прогон скриптов и юниты

Повторный запуск `scripts/11-setup-orchestrator.sh` **перезаписывает** systemd-юнит
и код оркестратора из копии проекта на машине. Держите копию проекта
актуальной из последнего zip (иначе фиксы откатываются — кейс 27.10: юнит
вернулся на `--host 127.0.0.1` → 502 на `/tasks/`). После обновления копии
повторный прогон 11 идемпотентен и безопасен.

То же касается правок самого оркестратора: код исполняется из `/opt/orchestrator`,
поэтому после замены `agents/orchestrator/orchestrator.py` в копии проекта нужен
`install` в `/opt/orchestrator` + `systemctl restart agents-orchestrator` (§12c).
Шаблоны задач (`SEED_TEMPLATES`) пересиживаются при каждом старте — миграции БД не требуют.

## 12b. Корпус базы знаний: rag-svc v2, миграция, массовый импорт, Qdrant

Полное обоснование с замерами — **RAG-SCALE-PLAN.md**; повседневные операции —
**KNOWLEDGE-WIKI.md §5–6**. Здесь только порядок действий.

**После обновления пакета** (копия проекта на машине должна быть свежей, см. §12a):

```bash
sudo bash scripts/12-setup-rag-svc.sh          # ставит v2 + env + гоняет самопроверку
# если корпус уже был в схеме v1 (векторы BLOB'ом внутри SQLite):
sudo bash scripts/17-rag-v2-migrate.sh --dry-run
sudo bash scripts/17-rag-v2-migrate.sh          # бэкап .v1-<штамп>.bak; откат: --rollback
```

Миграция **не пересчитывает эмбеддинги** — векторы переносятся как есть, поэтому
занимает минуты. Сервис v2 отказывается открыть базу v1 с внятной ошибкой:
тихая потеря векторов исключена.

**Qdrant — включить ДО массового импорта** (решение заказчика 28.10.2026: весь корпус
сразу пишется в целевой движок; обоснование и замеры — RAG-SCALE-PLAN.md §4.2):

```bash
sudo bash scripts/18-vector-store-qdrant.sh            # контейнер 127.0.0.1:6333 + переключение
curl -s localhost:8502/health | jq -c .                # backend = "qdrant"
sudo bash scripts/18-vector-store-qdrant.sh --status
sudo bash scripts/18-vector-store-qdrant.sh --rollback # откат: SQLite остаётся источником правды
```

Наполнение Qdrant идёт из уже посчитанных векторов (`POST /reindex
{"use_existing_vectors":true}` — скрипт делает это сам), поэтому переключение —
минуты, а не часы GPU. Если включить до импорта, корпус будет пуст/мал и весь
импорт напишет сразу в оба хранилища (Qdrant + SQLite-зеркало).

**Массовый импорт клинических рекомендаций:**

```bash
sudo mkdir -p /srv/kb/clinrec/{кардиология,эндокринология,хирургия}   # специальность = имя каталога
sudo bash scripts/19-import-clinrec.sh --dry-run      # ПРОГНОЗ: часы OCR, место, число чанков
sudo bash scripts/19-import-clinrec.sh --limit 20     # пилот: проверить метаданные и OCR глазами
sudo bash scripts/19-import-clinrec.sh --ocr never    # основной прогон (GPU не трогает)
sudo bash scripts/19-import-clinrec.sh --ocr auto --night-only --max-ocr-pages-total 3000
curl -s -X POST localhost:8502/compact | jq -c .
curl -s localhost:8502/stats | jq '{chunks,docs,recommendation,top1_score}'
```

Импорт возобновляем: журнал `/srv/agents/rag-svc/bulk-journal.jsonl` keyed по sha256,
поэтому Ctrl-C, обрыв и перезагрузка не требуют начинать заново (`--resume` включён
по умолчанию). VLM-OCR монопольно занимает llama.cpp — поэтому `--night-only`.

**Самопроверка после любых правок** (без Ollama и GPU, ~5 с):

```bash
/opt/rag-venv/bin/python research/test-rag-svc-v2.py --backend both   # 65 проверок
/opt/rag-venv/bin/python research/test-mcp-e2e.py                     # 27 проверок
python3 research/test-code-line-p4.py                                 # 81 проверка (P4, без Pi/БД)
```

## 12c. Кодовая линия P4: pi-worker оркестратора, шаблоны code-*, деплой

Кодовая линия = шаблоны задач, где роли `implementer`/`qa`/`reviewer`/`security`
исполняет **Pi headless** от unix-пользователя `agent-dev` (без sudo, доступ
только к своему home и `/srv/agents/repos`) через раннер
`/usr/local/sbin/agent-run-pi.sh`. Роль-карточки — `/etc/agents-roles/*.md`.

| Шаблон | Шаги | Группа |
|---|---|---|
| `code-fix` | implementer → qa → reviewer → **гейт человека** | development, administration |
| `code-review` | security → reviewer → **гейт человека** (только чтение diff) | development, administration |
| `code-feature` | planner → implementer → qa → reviewer → security → **гейт человека** | development, administration |

**Деплой после обновления пакета** (копия проекта должна быть свежей, см. §12a;
полная ежедневная последовательность — `OPS-CARD.md` §3 и §5):

```bash
PROJ="$(getent passwd "$SUDO_USER" | cut -d: -f6)/spark-llm-stack"
sudo systemctl is-active litellm agents-orchestrator    # оба должны быть active
sudo install -m 644 "$PROJ/agents/orchestrator/orchestrator.py" /opt/orchestrator/orchestrator.py
sudo systemctl restart agents-orchestrator
sudo bash "$PROJ/scripts/16-setup-code-line.sh"         # идемпотентен; смоук в конце
curl -s localhost:8500/api/templates | jq -r '.[].name'  # ожидаем code-fix/code-review/code-feature
```

**Проверка v3.12.5 (диф-артефакт гейта)** — именно так выглядела дыра: git от root
в репозиторий `agent-dev` молча пуст, а от владельца репо диф есть:

```bash
sudo git -C /srv/agents/repos/demo diff HEAD | head -5            # пусто + «dubious ownership» в stderr
sudo -u agent-dev git -C /srv/agents/repos/demo diff HEAD | head  # настоящий диф
```

После `restart` создайте контрольную задачу (§12c, `code-fix` на `demo`) и проверьте,
что в `/srv/agents/artifacts/task-<id>/` появился `task-<id>-<role>.diff` с шапкой
`# база задачи:`. На уже закрытых задачах диф задним числом не появится — артефакт
пишется в момент шага.

**Проверка без LLM** (фейковый раннер, ~20 с): `python3 research/test-code-line-p4.py`.

**Проверка самого Pi от `agent-dev`** (делаем до создания задачи, иначе шаги
падают с «No models available» / «Model not found»):

```bash
sudo -u agent-dev env -i HOME=/home/agent-dev PATH=/usr/bin:/bin TERM=dumb \
  /home/agent-dev/.pi/agent/bin/pi --list-models spark        # ждём строку qwen3.8-27b
cd /srv/agents/repos/demo && sudo -u agent-dev env -i HOME=/home/agent-dev \
  PATH=/home/agent-dev/.pi/agent/bin:/usr/bin:/bin TERM=dumb \
  /home/agent-dev/.pi/agent/bin/pi --model spark/qwen3.8-27b --no-session --print 'Ответь ровно: PONG'
```

`env -i` здесь принципиален: он проверяет именно тот путь, которым Pi зовёт
раннер (без переменных админской оболочки). `cd` — тоже: Pi кладёт сессию по cwd,
и если cwd не читаем для `agent-dev`, он печатает «Stored session working directory
does not exist» и до модели не доходит (ровно так валился эхо-смоук `scripts/16`
до v3.12.4). `--no-session` делает прогон одноразовым и убирает этот риск вовсе.
**Нельзя** проверять one-shot через `--mode rpc`: rpc читает JSONL из stdin и висит. То же самое одной командой делает
`sudo bash scripts/fix-pi-no-models.sh` (правка `models.json` + раннера + оба смоука,
бэкапы старых файлов создаёт сам). Конфиг воркера —
`agents/pi/models.headless.json`: ключ читается командой `!cat /home/agent-dev/.llm_key`,
поэтому `SPARK_LLM_KEY` в окружении не нужен ни в headless, ни в интерактивной сессии.

**Артефакты задачи** — `/srv/agents/artifacts/task-<id>/`: `TASK-*.md` (что ушло в Pi),
`OUT-*.md` (вывод роли, в конце строка `pi exit=<код>`), `task-<id>-<role>.diff` —
**пишется всегда**, это материал человеческого гейта (v3.12.5). Внутри: шапка
(`# база задачи:` — ревизия на старте задачи, `# изменено файлов:`, `# коммитов после базы:`,
`# неотслеживаемых файлов:`, при сбоях — `# ВНИМАНИЕ: git вернул ошибки:`), затем
`git diff <база>` (или прямой текст «диф пуст», если роль ничего не изменила) и раздел
«НЕОТСЛЕЖИВАЕМЫЕ ФАЙЛЫ» с содержимым новых файлов — `git diff` их не показывает.
git оркестратор зовёт **от владельца репозитория** (`sudo -n -u agent-dev git`): юнит
работает от root, а git ≥ 2.35 на чужой репозиторий отвечает `dubious ownership` с пустым
stdout. Если `sudo` не сработал — fallback `git -c safe.directory=<repo>` (разово,
в gitconfig ничего не пишется). База дифа — ревизия на старте задачи, поэтому в дифе видны
и незакоммиченные правки, и коммиты ролей; для репо без коммитов база — пустое дерево.
Решение на гейте: кнопка в `/tasks/` или
`POST /api/tasks/<id>/steps/<step_id>/decision {"decision":"approve|reject","comment":"..."}`.

**Переменные** (`/etc/orchestrator.env`): `AGENTS_PI_RUNNER`, `AGENTS_REPOS_DIR`,
`AGENTS_PI_TIMEOUT_S` (жёсткий бюджет шага; по таймауту группа процессов добивается,
шаг считается проваленным), `AGENTS_PI_MAX_PROMPT_CHARS`, `AGENTS_PI_USER`
(владелец репозиториев, от которого оркестратор зовёт git; по умолчанию `agent-dev`),
`AGENTS_PI_MAX_UNTRACKED` (сколько неотслеживаемых файлов тащить в `.diff`, по умолчанию 50).

**Подключение реального репозитория:**

```bash
echo 'myrepo git@git.local:clinic/myrepo.git' | sudo tee -a /etc/agents-repos.list
sudo -u agent-dev git clone git@git.local:clinic/myrepo.git /srv/agents/repos/myrepo
```

Pi правит рабочую копию в ветке `task-<id>` (создаётся автоматически); merge — за человеком.

## 13. Troubleshooting

| Симптом | Причина | Лечение |
|---|---|---|
| vLLM-контейнер умирает на старте / узел виснет | `gpu-memory-utilization` слишком высокий | только 0.60 (рецепт: 0.85 вешал узел!); `docker rm -f vllm-qwen38` → скрипт 03 |
| У задачи кодовой линии нет `.diff`-артефакта, reviewer/security видят «diff пуст», хотя правка в рабочей копии есть | оркестратор (root) звал git в репозиторий `agent-dev` → `fatal: detected dubious ownership`, stdout пуст | v3.12.5+: git зовётся через `sudo -n -u agent-dev` (fallback — разовый `-c safe.directory`); выложить свежий `orchestrator.py` (§12c) и `sudo systemctl restart agents-orchestrator`. Проверка: `sudo git -C /srv/agents/repos/demo diff HEAD` — пустой вывод при живых правках = оно. `safe.directory` в gitconfig НЕ добавлять |
| Гейт остался без дифа, хотя роль «ничего не меняла» | до v3.12.5 `.diff` писался только при непустом дифе — «нет изменений» и «git не отработал» выглядели одинаково | v3.12.5+: артефакт пишется всегда, пустой диф сопровождается текстом «диф пуст» и списком коммитов после базы |
| `docker pull vllm/vllm-openai` не идёт (таймаут/блок) | Docker Hub недоступен/медленный | registry-зеркало: `/etc/docker/daemon.json` → `{"registry-mirrors":["https://<зеркало>"]}` → `systemctl restart docker`; либо прокси для dockerd (`systemctl edit docker`) |
| NGC-образ нужен, но `nvcr.io` 403/unauthorized (из РФ — недоступен) | гео-блок/stale-логин | по умолчанию используется Docker Hub-образ (менять ничего не надо); точный NGC-образ — только через VPN: `docker login nvcr.io -u '$oauthtoken' -p <NGC_API_KEY>` + `VLLM_IMAGE=nvcr.io/nvidia/vllm:26.10.pre1-py3` |
| Загрузка весов с HF висит/ошибается | huggingface.co заблокирован/медленный из РФ | уже используется `HF_ENDPOINT=https://hf-mirror.com` (по умолчанию в скрипте 03); при проблемах зеркала — `HF_ENDPOINT=https://huggingface.co` через VPN |
| vLLM Docker Hub-образ: `literal_error: nvfp4_dynamic` / `unexpected_keyword_argument` в `--kernel-config` | патчи NVIDIA не влиты в upstream (open PRs #54614 и др.) — рецепт совместим ТОЛЬКО с NGC-образом | не использовать upstream с флагами рецепта; путь A: `sudo bash scripts/03b-deploy-qwen38-llamacpp.sh` (сам удалит crashed-контейнер), либо офлайн-импорт NGC (Приложение А.1) |
| `https://<host>/gateway/ui` перебрасывает на `/ui/` и 404 | LiteLLM UI (Next.js) под подпутём теряет префикс на абсолютных редиректах | UI живёт на отдельном origin: `https://<host>:8443/ui` (или напрямую `http://<host>:4000/ui`); API при этом штатно ходит через `/gateway/v1` |
| `curl -k https://127.0.0.1/` → 000 при живом caddy | доступ по IP: TLS-клиенты не шлют SNI для IP-литералов — internal/on-demand TLS не может выпустить сертификат | используется статический сертификат с SAN (IP+localhost+hostname), генерируется скриптом 04 в `stack/caddy/certs/`; при его отсутствии: `sudo rm -rf stack/caddy/certs && sudo bash scripts/04-deploy-web.sh`. После смены IP машины — перегенерировать тем же способом |
| Запросы через шлюз :4000 висят, хотя `curl 127.0.0.1:8000` работает | ufw режет путь docker-мост → хост:8000 (INPUT; DOCKER-USER его не покрывает) | `sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp` (в свежих версиях скрипта 01 правило добавляется само) |
| Контейнер vllm-qwen38 в цикле рестартов, лог повторяет ошибку аргументов | `--restart unless-stopped` перезапускает упавший процесс | `sudo docker update --restart=no vllm-qwen38 && sudo docker rm -f vllm-qwen38`, далее выбранный путь (§6) |
| Загрузка весов висит/обрывается | HF rate-limit | `HF_TOKEN=... sudo -E bash scripts/03-...`; кэш резюмится |
| Ответы очень длинные/медленные | thinking xhigh по умолчанию | `chat_template_kwargs: {"enable_thinking": false}` или `reasoning_effort: low` (§8) |
| Tool calling не работает | парсер не включён | в контейнере должны быть `--enable-auto-tool-choice --tool-call-parser qwen3_xml` (скрипт 03 содержит) |
| Литellм 401 / Open WebUI без моделей | рассинхрон ключей | скрипт 04 синхронизирует `/etc/spark-llm.env` → `stack/.env`; `docker compose up -d` |
| RAG не ищет по русским документам | не тот эмбеддер / скан без OCR | *Admin → RAG*: embedding = bge-m3; после смены — переиндексация; см. RAG-GUIDE §4 |
| `/ops` — «needs key» | VLLM_API_KEY не введён в браузере | вставить из `/etc/spark-llm.env` |
| Медленно при 5+ пользователях | KV-кэш/concurrency | поднять `--max-num-seqs` нельзя без памяти; снизить `--max-model-len` до 131072; следить `vllm:gpu_cache_usage_perc` в /ops |
| Vision-запрос → HTTP 500, в логах `ValueError: image size too small` | изображение мельче минимального патча Qwen-VL (28×28 / 56×56) | использовать изображения ≥64×64 (в 05-verify встроен валидный 64×64 PNG); в интеграциях — не отправлять крошечные/degenerate картинки |
| Длинные документы «обрезаются» на ~2048 токенах | `model_max_length: 2048` в tokenizer_config.json некоторых сборок NVFP4 | сервер это игнорирует (`--max-model-len 262144`); проверка — проба [4/6] в 05-verify; при клиентском усечении — править модель в кэше или настройки SDK; диагностика печатается в конце скрипта 03 |
| Первый запрос после простоя медленный | прогрев/pre-fill | норма; `--async-scheduling` уже включён |
| Pi: «Warning: No models available» (или `--print` падает с тем же текстом) | провайдер не авторизован: `apiKey: "$SPARK_LLM_KEY"` в `models.json`, а переменной в этой сессии нет — Pi скрывает все модели провайдера | для `agent-dev`: `sudo bash scripts/16-setup-code-line.sh` (ставит `models.headless.json` с `apiKey: "!cat /home/agent-dev/.llm_key"`); на рабочей станции: `export SPARK_LLM_KEY=…` в профиль. Проверка: `pi --list-models spark`, `pi auth check --provider spark` |
| Скрипт 16 на шаге [2/7] каждый раз лезет в сеть ставить Pi (или падает на закрытом периметре) | проверка была `command -v pi` в неlogin-шелле: install.sh прописывает PATH в `~/.bashrc`/`~/.profile`, поэтому уже установленный бинарь не находился | обновить копию `~/spark-llm-stack` из актуального пакета: шаг 2 ищет бинарь по путям (`/usr/local/bin/pi`, `~/.pi/agent/bin/pi`, `~/.pi/agent/install/releases/*/bin/pi`) и ставит Pi только если его реально нет; без сети — `OFFLINE_PI=1 sudo -E bash scripts/16-setup-code-line.sh` (шаг 2 пропускается), офлайн-установка: `npm pack @earendil-works/pi-coding-agent` на машине с сетью → `sudo -u agent-dev npm install -g --ignore-scripts ./earendil-works-pi-coding-agent-*.tgz`)
| Pi: `Model "spark:qwen3.8-27b" not found` | синтаксис Pi 0.10.x; в Pi 1.x двоеточие = уровень thinking | `--model spark/qwen3.8-27b` (опционально `:high`); в раннере `/usr/local/sbin/agent-run-pi.sh` то же самое — обновляется скриптом 16 |

## 14. Приложение А — офлайн-импорт NGC-образа и смена кванта

### А.1. Офлайн-импорт `nvcr.io/nvidia/vllm:26.10.pre1-py3` (путь B без NGC-доступа на Spark)

На любой машине с доступом к NGC (VPN/зарубежный VPS; **обязательно arm64** —
Apple Silicon подойдёт, на x86 — с `--platform linux/arm64`):

```bash
docker login nvcr.io -u '$oauthtoken' -p <NGC_API_KEY>     # бесплатный аккаунт ngc.nvidia.com
docker pull --platform linux/arm64 nvcr.io/nvidia/vllm:26.10.pre1-py3
docker save nvcr.io/nvidia/vllm:26.10.pre1-py3 | gzip > vllm-26.10.pre1-arm64.tar.gz  # ~10–15 ГБ
# перенести на Spark (scp/флешка), затем на Spark:
gunzip -c vllm-26.10.pre1-arm64.tar.gz | sudo docker load
sudo bash scripts/03-deploy-qwen38-vllm.sh   # docker pull увидит локальный образ и пропустит скачивание
```

### А.2. Смена кванта GGUF (путь A)

Выбор обоснован в §1: **UD-Q4_K_XL** (Dynamic v3.0: точность ≈Q5 при размере
Q4, +12% скорости к Q5_K_M). Если на ваших реальных документах Q4 даст
деградацию — переход на Q5_K_M одной командой:

```bash
sudo QWEN_GGUF=Qwen3.8-27B-UD-Q5_K_M.gguf bash scripts/03b-deploy-qwen38-llamacpp.sh
```
(скрипт докачает файл и пересоберёт юнит; +2,2 ГБ памяти, −10–12% скорости).
Линейка unsloth Dynamic v3.0: UD-Q3_K_XL 13,1 · UD-Q4_K_S 15,4 · UD-Q4_K_M 16,5 ·
**UD-Q4_K_XL 17,6** · UD-Q5_K_M 19,8 · UD-Q6_K_XL 25,3 · UD-Q8_K_XL 31,5 ГБ.

## 15. Приложение Б — Шпаргалка

```text
Модель шлюза (поле "model")     qwen3.8-27b  ·  эмбеддинги: bge-m3
Единый API                      http://<IP>:4000/v1  (https://<IP>/gateway/v1)
Ключи                           sudo cat /etc/spark-llm.env  (VLLM_API_KEY, LITELLM_MASTER_KEY)
Движки                          docker: vllm-qwen38 · systemd: ollama
Веб                             cd stack && docker compose ps|logs|restart|pull|up -d
URL                             https://<IP>/  чат+админка · /ops дашборд · /gateway/ui LiteLLM
Pi                              pi --model spark/qwen3.8-27b   (agents/pi/README.md)
Thinking                        "chat_template_kwargs":{"enable_thinking":false} | {"reasoning_effort":"low|medium|xhigh"}
Логи                            docker logs vllm-qwen38 · journalctl -u ollama -f
RAG                             RAG-GUIDE.md (документы, обновление КР, качество)
Корпус (rag-svc v2)             curl -s localhost:8502/stats | jq · KNOWLEDGE-WIKI.md
Масштабирование корпуса         RAG-SCALE-PLAN.md (замеры, пороги Qdrant, стоимость импорта)
Массовый импорт КР              scripts/19-import-clinrec.sh --dry-run → --limit 20 → прогон
Скиллы → DeepSeek Harness       scripts/21-export-dsh-skills.sh · SKILLS-INVENTORY.md · agents/dsh/…§7
Массовый reject висячих гейтов  OPS-CARD.md §3.7 (сначала список, потом reject — необратимо)
Мигрировать v1→v2 / Qdrant      scripts/17-rag-v2-migrate.sh · scripts/18-vector-store-qdrant.sh
MCP-доступ агентов к корпусу    agents/mcp/rag-mcp-server.py (kb_search/kb_list_docs/kb_stats)
Самопроверка корпуса            /opt/rag-venv/bin/python research/test-rag-svc-v2.py --backend both
```

---

## Источники (проверено 2026-10-22)

1. **nvidia/local-ai-inference-recipes** — рецепт `qwen3.8-27b/dgx-spark/linux/vllm/128gb/nvfp4` (37,29 tok/s, валидирован 19.10.2026, util 0.60, предупреждение про 0.85) и `qwen3.8-27b/geforce-rtx/.../vision-dflash2` (159,6 tok/s) — копии в `research/nvidia-recipes/`
2. Qwen/Qwen3.8-27B (карточка: TB2.1 73,0; SWE-bench Pro 61,7; OmniDocBench 91,1; OSWorld 84,3; CoWorkBench 70,7; 262K→1M; thinking/`reasoning_effort`; Apache-2.0)
3. unsloth/Qwen3.8-27B-GGUF (UD-Q4_K_XL 17,6 ГБ, UD-Q5_K_M 19,8 ГБ, mmproj-F16 0,9 ГБ); z-lab/Qwen3.8-27B-DFlash2-GGUF (Q4_K_M 1,14 ГБ)
4. Pi docs: pi.dev/docs (models-configuration; nvidia-rtx-llama-cpp); `@earendil-works/pi-coding-agent`
5. LiteLLM docs (gateway, virtual keys); Open WebUI docs (Knowledge/RAG, RBAC); BAAI/bge-m3 (100+ языков); vLLM blog «vLLM on the DGX Spark»
6. MODEL-ANALYSIS.md — сравнение моделей (включая исключённый Nemotron) с бенчмарками
