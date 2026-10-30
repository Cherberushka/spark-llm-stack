# PU-трек: персональная агентная сеть разработчика в WSL Ubuntu (Windows)

Цель: полный локальный пайплайн на машине разработчика (свой оркестратор,
worker'ы, Pi, MCP) с моделями **с центрального шлюза Spark** по персональному
ключу. Орг-данные и орг-оркестратор остаются центральными; удачные конфигурации
«повышаются» в глобальную сеть через PU8.

## Топология

```
 Windows ─ WSL Ubuntu
 │          │  Pi + skills + MCP(wsl-repos, win-docs-ro)
 │          │  персональный оркестратор :8501 (своя БД pu-postgres, свой inbox)
 │          │        │ модели/токены
 │          ▼        ▼
 │        LiteLLM :4000 ── Qwen3.8-27B :8000   ← Spark (центр)
 │          │
 │          └─ орг-оркестратор :8500, Open WebUI, Knowledge, гейты, аудит
 └─ браузер: Open WebUI / :8443 UI / dsh
```

**Что остаётся центральным навсегда:** движок, шлюз+ключи+аудит, орг-Knowledge,
орг-оркестратор (реестр ролей, гейты), артефакты орг-задач.
**Что персонально:** оркестратор-копия, worker'ы, Pi-сессии, MCP над личными
репо/док-папками, задачи-эксперименты.

## Шаги

| Шаг | Скрипт | Что делает | Критерий |
|---|---|---|---|
| PU1 | pu1-prereqs.sh | WSL-проверка, пакеты (pandoc/ocr/tesseract/venv) | `✓ Windows-диски видны` |
| PU2 | pu2-connect.sh | доступность шлюза, персональный ключ → `~/.spark-env` (600) | `curl /v1/models` = 200 |
| PU3 | pu3-pi.sh | Pi + models.json (шлюз) + курированные скиллы | `pi --list-models spark` видит spark/qwen3.8-27b |
| PU4 | pu4-mcp-local.sh | MCP: `$HOME/projects` (rw) + папка документов Windows (ro-смысл) | `pi` → `/mcp` connected |
| PU5 | pu5-orchestrator-local.sh | pu-postgres (docker), venv, копия orchestrator.py, старт :8501 | `GET :8501/api/templates` = JSON |
| PU6 | pu6-workers-local.sh | смоук: echo + doc-convert на личном inbox/artifacts | task status=done, md в artifacts |
| PU7 | pu7-bridge.sh | проверка моста: расход по ключу, правила эскалации орг-задач | вывод моделей шлюза |
| PU8 | pu8-return-global.sh | экспорт задач/артефактов на Spark, promotion ролей, декомиссия | экспорт в /srv/agents/import/ |

## Правила трека

1. Персональный ключ — с бюджетом/limite'ами (админ выдаёт в :8443 UI);
   ротация/отзыв при уходе с машины.
2. В `~/agents-inbox` и `~/projects` **не копировать** орг-персданные;
   документы с /mnt/c — читать, не переносить.
3. Персональный оркестратор не имеет доступа к центральной БД задач и к
   ORCH_API_KEY сервера — эскалация орг-задач только через центральный intake
   (Open WebUI Tool create_task с P2 / через админа).
4. Пути артефактов/inbox персональной копии — в `~/` (orchestrator.py читает
   `AGENTS_ART_DIR/AGENTS_INBOX_DIR` из env — один код на центр и WSL).
5. Версии: пинуйте образ pu-postgres (postgres:16-alpine) и тег dsh/Pi, как на
   центре (TUNING-GUIDE шаг 8).

## Возврат к глобальной развертке

PU8 → экспорт лежит на Spark в `/srv/agents/import/personal-<дата>/`;
админ переносит проверенные карточки ролей/worker-конфиги в центральный
`agents_registry` (UPDATE/INSERT по образцу `GET /api/registry`) и включает
линии по roadmap: P2 (doc) → P4 (code) → P6 → P3 (MSSQL) → P5 → P7.
Персональная инстанса после этого гасится (pu8 делает это сам).
