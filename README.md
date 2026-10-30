# Spark LLM Stack — DGX Spark (GB10, 128 ГБ)

Локальный LLM-сервер для организации: **агентный кодинг** (Pi-агент) и
**информационный оптимизатор частной клиники** (отчёты, преобразование
документов, RAG-поиск по документации, Text-to-SQL). Один API-шлюз,
одна чат-модель, минимум движков.

> Состав (решение заказчика 22.10.2026): **Qwen3.8-27B — единственная
> чат-модель**; Nemotron исключён полностью (история выбора — MODEL-ANALYSIS.md).

| Роль | Модель | Движок | Скорость (измерено NVIDIA на DGX Spark) |
|---|---|---|---|
| Чат-модель: код, документы, отчёты, зрение, tools | **Qwen3.8-27B** | :8000 — путь A: llama.cpp GGUF+DFlash2 (без NGC, из РФ); путь B: vLLM NVFP4 (точный рецепт) | A: ~25–30 tok/s, ctx 160K · B: **37,3 tok/s**, ctx 262K, 8 польз. |
| Эмбеддинги RAG | **bge-m3** (RU+EN) | Ollama :11434 | — |
| Единый API | `qwen3.8-27b` · `bge-m3` | **LiteLLM** :4000 | ключи отделов, лимиты, логи |
| Веб | чат + админка + Knowledge/RAG | **Open WebUI** за **Caddy** :443 | `/ops`-дашборд |

## Быстрый старт

```bash
cd dgx-spark-project
sudo bash scripts/setup-wizard.sh     # 6 стадий с подтверждениями (~1–1,5 ч с закачками)
```

Затем:
- `https://<IP-Spark>/` — чат + админка (**первый зарегистрированный = админ**);
- `https://<IP-Spark>/ops` — дашборд состояния;
- `http://<IP-Spark>:4000/v1` — единый API (модель `qwen3.8-27b`, эмбеддинги `bge-m3`);
- на рабочей станции разработчика: `bash scripts/06-setup-pi.sh` → `pi --model spark/qwen3.8-27b`.

## Документация

| Файл | О чём |
|---|---|
| **[OPS-CARD.md](OPS-CARD.md)** | карта ежедневной эксплуатации на одну страницу: кто где живёт (сервисы/порты/пути), утренний чек, борд задач через SSH-туннель, полный цикл задачи P4 и решение гейта, добавление документа в RAG, порядок обновления пакета, аварийные команды, инварианты безопасности |
| **[STATE.md](STATE.md)** | кэш состояния стека на 1 экран: что работает, какие линии (P1–P7) открыты, ключевые замеры, действующие решения, быстрые команды. **Читать первым**; обновлять после каждого этапа |
| **[INSTALL-RUNBOOK.md](INSTALL-RUNBOOK.md)** | полная инструкция: архитектура, бюджет памяти, стадии 1–6, API и thinking-режимы, админка, эксплуатация, безопасность, troubleshooting, fallback llama.cpp (Приложение А) |
| **[RAG-GUIDE.md](RAG-GUIDE.md)** | RAG с нуля для клиники: какие документы загружать, подготовка сканов, **регламент обновления при изменении КР**, золотой набор вопросов, качество/рост, граница RAG↔БД, мед-оговорки |
| [MODEL-ANALYSIS.md](MODEL-ANALYSIS.md) | история выбора модели (бенчмарки, языки, измерения NVIDIA на Spark) |
| [agents/pi/](agents/pi/) | настройка Pi-агента (models.json + README) |
| [agents/dsh/DEEPSEEK-HARNESS-GUIDE.md](agents/dsh/DEEPSEEK-HARNESS-GUIDE.md) | DeepSeek Harness (dsh) → шлюз Spark: установка, custom provider, метаданные-грабли |
| [agents/mcp/WINDOWS-MCP-GUIDE.md](agents/mcp/WINDOWS-MCP-GUIDE.md) | MCP на Windows: Node, JSON-конфиги (filesystem / desktop-commander / БД), проверка, безопасность, troubleshooting |
| [SKILLS-APPLIED.md](SKILLS-APPLIED.md) | какие agent-скиллы из GitHub-баз собраны и как применены |
| **[RAG-SCALE-PLAN.md](RAG-SCALE-PLAN.md)** | масштабирование корпуса до всех клинических рекомендаций: **замеры** латентности/памяти, переход на Qdrant (решение 28.10.2026 — до массового импорта), модель стоимости импорта (OCR/эмбеддинги), калибровка порога релевантности |
| [KNOWLEDGE-WIKI.md](KNOWLEDGE-WIKI.md) | база знаний: три индекса и почему общий — только rag-svc, как посмотреть что в базе, операции (supersede/compact/reindex/миграция/Qdrant) |
| [AGENT-NETWORK-PLAN.md](AGENT-NETWORK-PLAN.md) | агентная сеть: роли, шаблоны, гейты, фазы P0–P7 |

## Структура

```
dgx-spark-project/
├── scripts/
│   ├── setup-wizard.sh          ← интерактивная установка всего стека (6 стадий)
│   ├── 00-check-prereqs.sh      ← preflight (GB10, память, диск, Docker+NVIDIA runtime, сеть)
│   ├── 01-prepare-system.sh     ← пакеты, /opt, ufw + DOCKER-USER (LAN_NETS=192.168.8.0/22)
│   ├── 02-install-ollama-embeddings.sh ← Ollama + bge-m3 (эмбеддинги RAG)
│   ├── 03-deploy-qwen38-vllm.sh ← путь B: vLLM NVFP4 (NGC-образ, рецепт дословно)
│   ├── 03b-deploy-qwen38-llamacpp.sh ← путь A (default, без NGC): llama.cpp GGUF+DFlash2
│   ├── 04-deploy-web.sh         ← LiteLLM + Open WebUI + Caddy (compose)
│   ├── 05-verify.sh             ← сквозные тесты (текст/зрение/шлюз/эмбеддинги)
│   ├── 06-setup-pi.sh           ← конфиг Pi-агента (models.json, ключ)
│   ├── 10-install-pi-skills.sh  ← курированный набор скиллов (OFFLINE=1 → vendor-skills/)
│   ├── 11-setup-orchestrator.sh ← оркестратор задач :8500 + дашборд /tasks
│   ├── 12-setup-rag-svc.sh      ← корпусный поиск :8502 (v2) + самопроверка
│   ├── 13-add-doc.sh            ← добавить один документ и сразу проверить вопросом
│   ├── 14-ops-cron.sh           ← ночной бэкап 03:30 + дайджест 06:30 + logrotate
│   ├── 15-share-doctor.sh       ← samba-шара doctor + cron-приём файлов (5 мин)
│   ├── 16-setup-code-line.sh    ← линия кода: user agent-dev, runner Pi, карточки ролей
│   ├── 17-rag-v2-migrate.sh     ← миграция корпуса v1→v2 без пере-эмбеддинга (+ --rollback)
│   ├── 18-vector-store-qdrant.sh ← Qdrant: включить до массового импорта (решение 28.10.2026)
│   │                               --install/--sync/--status/--rollback/--remove
│   ├── 19-import-clinrec.sh     ← массовый импорт КР: прогноз, resume, ночное окно
│   ├── 20-log-hygiene.sh        ← чистка/архивация логов (dry-run по умолчанию, --apply)
│   └── 21-export-dsh-skills.sh  ← экспорт скиллов (16 vendor + роли P4) в формат DeepSeek Harness
├── stack/
│   ├── docker-compose.yml       ← litellm + postgres + redis + open-webui + caddy
│   ├── litellm/config.yaml      ← маршруты моделей шлюза
│   ├── caddy/Caddyfile + certs/ ← / · /ops · /gateway · /vllm · /ollama; self-signed с SAN
│   ├── web/ops-dashboard.html   ← дашборд здоровья (taste-skill app-screens)
│   └── .env.example
├── agents/
│   ├── orchestrator/            ← orchestrator.py, rag-svc.py, rag_migrate_v1.py,
│   │                              rag_bulk_ingest.py, openwebui-tool-tasks.py, glossary
│   ├── mcp/                     ← rag-mcp-server.py (доступ агентов к корпусу) + Windows-гайд
│   ├── pi/  ·  dsh/  ·  wsl/    ← харнесс-агенты и WSL-трек PU1–PU8
├── vendor-skills/               ← офлайн-копии скиллов для установки без интернета
└── research/
    ├── nvidia-recipes/          ← копии рецептов/измерений NVIDIA (первоисточники)
    ├── bench-rag-scale.py       ← бенчмарк корпусного поиска (--decomp, --v2)
    ├── test-rag-svc-v2.py       ← 65 проверок хранилища (без Ollama/GPU)
    ├── test-mcp-e2e.py          ← 27 проверок сквозного пути MCP → rag-svc
    └── test-code-line-p4.py     ← 81 проверка кодовой линии P4 (без Pi/БД)
```

## Ключевые команды

```bash
docker ps && docker logs -f vllm-qwen38     # основной движок
systemctl status ollama                     # эмбеддинги
cd stack && docker compose ps               # шлюз + веб
sudo cat /etc/spark-llm.env                 # все API-ключи
sudo bash scripts/05-verify.sh              # здоровье стека (6 групп тестов)
curl -s localhost:8502/stats | jq '{chunks,docs,recommendation}'   # корпус и пороги
nvtop                                       # память/GPU
```

**Доступность из РФ:** путь A (llama.cpp) не требует NGC: исходники с GitHub,
веса через `hf-mirror.com`. Путь B (vLLM NVFP4, 37,3 tok/s) требует NGC-образ
`nvcr.io/nvidia/vllm:26.10.pre1-py3` — офлайн-импорт `docker save/load`
(runbook Приложение А.1). Docker Hub `vllm/vllm-openai` с флагами рецепта
НЕ совместим (патчи NVIDIA — открытые PR).

**Сеть:** по умолчанию доступ к API (8000/4000/11434) разрешён только подсетям
организации **192.168.8.0 – 192.168.11.255** (`LAN_NETS=192.168.8.0/22`);
проброшенные порты контейнеров фильтрует цепочка DOCKER-USER (Docker NAT минует
ufw) — юнит `spark-docker-user.service`, конфиг `/etc/default/spark-firewall`.
Веб (80/443) открыт — защищён аутентификацией Open WebUI и ключами шлюза.

Все скрипты идемпотентны; загрузки весов возобновляемы.
