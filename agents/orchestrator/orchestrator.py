#!/usr/bin/env python3
"""
agents-orchestrator — P1 skeleton of the agent network (AGENT-NETWORK-PLAN §2, §7)

Responsibilities:
  * task intake (REST; later: Open WebUI Tool, forms, cron)
  * planning: template-driven steps OR LLM planner (qwen3.8 via LiteLLM gateway)
  * registry of agent role cards (agents_registry) — router picks roles from it
  * dispatch: in-process workers (echo, doc_convert, rag_index, rag_query)
    + Pi headless (P4): шаги ролей implementer/qa/reviewer/security через
    /usr/local/sbin/agent-run-pi.sh от пользователя agent-dev
  * human gates: steps with kind='gate' pause the task until approve/reject
  * events/artifacts journal (task_events), token budget accounting

Listens on 127.0.0.1:8500 only. Снаружи — SSH-туннель (`ssh -N -L 8500:127.0.0.1:8500 …`,
дальше http://localhost:8500/): роута /tasks/* в Caddy намеренно нет — борд с кнопками
гейта не публикуется в LAN. Все пути дашборда относительные, поэтому при желании его
можно повесить на `handle_path /tasks/*` + basic_auth без правок HTML.
"""
import json, os, re, signal, subprocess, threading, time, traceback, uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Request, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

DB_URL = os.environ["DATABASE_URL"]
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:4000/v1")
ORCH_LLM_KEY = os.environ.get("ORCH_LLM_KEY", "")
ORCH_API_KEY = os.environ["ORCH_API_KEY"]
ART = Path(os.environ.get("AGENTS_ART_DIR", "/srv/agents/artifacts"))
INBOX = Path(os.environ.get("AGENTS_INBOX_DIR", "/srv/agents/kb-inbox"))
WEB = Path(__file__).parent / "web"

FREEFORM_GROUPS = {"development", "administration", "admin", "dev"}
RAG_SVC = os.environ.get("RAG_SVC_URL", "http://127.0.0.1:8502")

# ── P4: кодовая линия (Pi headless) ─────────────────────────────────────────
# Раннер ролей ставит scripts/16-setup-code-line.sh; если его нет, шаги ролей
# выполняются заглушкой каркаса (SKEL), как до P4 — поведение не ломается.
PI_RUNNER = Path(os.environ.get("AGENTS_PI_RUNNER", "/usr/local/sbin/agent-run-pi.sh"))
AGENT_REPOS = Path(os.environ.get("AGENTS_REPOS_DIR", "/srv/agents/repos"))
PI_TIMEOUT_S = int(os.environ.get("AGENTS_PI_TIMEOUT_S", "3600"))
PI_WORKDIR_USER = os.environ.get("AGENTS_PI_USER", "agent-dev")
# Роли, которые исполняет Pi. Имена = файлы /etc/agents-roles/<role>.md:
# карточка security-reviewer лежит в security.md (роль в реестре — "security").
PI_ROLES = ("implementer", "qa", "reviewer", "security")
MAX_PROMPT_CHARS = int(os.environ.get("AGENTS_PI_MAX_PROMPT_CHARS", "24000"))
MAX_OUTPUT_CHARS = 20000

SCHEMA = """
create table if not exists agents_registry (
  role text primary key,
  description text not null,
  thinking text not null default 'off',
  tools jsonb not null default '[]',
  mcp jsonb not null default '[]',
  gates text[] not null default '{}',
  budget_default int not null default 200000,
  active bool not null default true
);
create table if not exists templates (
  name text primary key,
  description text not null,
  groups text[] not null default '{everyone}',
  steps jsonb not null
);
create table if not exists tasks (
  id bigserial primary key,
  template text, payload jsonb not null default '{}',
  requester text not null, grp text not null default 'everyone',
  status text not null default 'new',
  current_step int not null default 0,
  token_budget int not null default 200000, tokens_used int not null default 0,
  result text, artifacts text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table if not exists task_steps (
  id bigserial primary key, task_id bigint references tasks(id) on delete cascade,
  idx int not null, role text not null, kind text not null default 'worker',
  spec jsonb not null default '{}', status text not null default 'pending',
  output text, started_at timestamptz, finished_at timestamptz
);
create table if not exists task_events (
  id bigserial primary key, task_id bigint references tasks(id) on delete cascade,
  ts timestamptz not null default now(), level text not null default 'info',
  message text not null, artifact text
);
create index if not exists ix_tasks_status on tasks(status);
create index if not exists ix_events_task on task_events(task_id, ts);
"""

SEED_REGISTRY = [
    ("planner",        "Декомпозиция задачи в граф шагов; выбирает роли из реестра",            "xhigh", ["read-only fs", "repo-graph"], [], [], 120000),
    ("implementer",    "Реализация правок в workspace задачи; тесты обязательны",               "off",   ["fs", "shell"], ["fs-workspace"], [], 300000),
    ("qa",             "Тесты/приёмка: tdd-скилл, прогон автотестов",                            "low",   ["fs", "shell"], ["fs-workspace"], [], 200000),
    ("reviewer",       "Код-ревью diff'а, вердикт блокирует merge-артефакт",                    "medium",["read-only diff"], [], [], 150000),
    ("security",       "Security-чеклист: инъекции, секреты, зависимости",                      "medium",["read-only"], [], [], 150000),
    ("docs",           "Документация задачи/репо (writing-for-agents)",                          "off",   ["fs"], [], [], 100000),
    ("db-auditor",     "MSSQL: каталог объектов, статистики, pg-аналог = sys.dm_*; только чтение","low",  ["mssql-ro"], ["mssql-ro"], [], 200000),
    ("db-perf",        "MSSQL: планы запросов, sys.dm_exec_query_stats, гипотезы оптимизаций",   "medium",["mssql-ro(staging)"], ["mssql-staging"], [], 300000),
    ("db-migration",   "MSSQL: скрипты изменений + тесты на staging; gate DBA обязателен",       "medium",["mssql-staging"], ["mssql-staging"], ["dba"], 300000),
    ("report",         "Отчёты: числа ТОЛЬКО из SQL-артефактов db-роли; оформление по шаблону",   "off",   ["mssql-ro", "fs"], ["mssql-ro"], [], 150000),
    ("doc-worker",     "Документы: OCR/конвертация/выжимки, цитаты обязательны",                  "off",   ["fs", "ocr", "rag"], ["fs-kb"], [], 150000),
]
SEED_TEMPLATES = [
    ("echo", "Проверочная задача каркаса", ["everyone"],
     [{"role": "implementer", "kind": "worker", "spec": {"worker": "echo"}}]),
    ("doc-convert", "Конвертация из kb-inbox в Markdown (OCR при нужде) + индексация в корпус", ["everyone"],
     [{"role": "doc-worker", "kind": "worker", "spec": {"worker": "doc_convert"}},
      {"role": "doc-worker", "kind": "worker", "spec": {"worker": "rag_index"}}]),
    ("doc-qa", "Вопрос по корпусу документов: поиск с цитатами + ответ", ["everyone"],
     [{"role": "doc-worker", "kind": "worker", "spec": {"worker": "rag_query"}}]),
    ("feature", "Полный цикл фичи (plan→impl→qa→review∥security→docs)", ["development", "administration"],
     [{"role": "planner", "kind": "worker", "spec": {"plan": True}},
      {"role": "implementer", "kind": "worker", "spec": {}},
      {"role": "qa", "kind": "worker", "spec": {}},
      {"role": "reviewer", "kind": "gate", "spec": {"note": "ревью кода человеком"}},
      {"role": "docs", "kind": "worker", "spec": {}}]),
    ("db-report", "Отчёт по данным МИС (MSSQL, read-only) — заглушка до P3", ["administration"],
     [{"role": "report", "kind": "worker", "spec": {"worker": "not_implemented"}}]),
    ("code-fix", "P4: правка в репозитории силами Pi (implementer→qa→reviewer) + гейт человека",
     ["development", "administration"],
     [{"role": "implementer", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 3600}},
      {"role": "qa", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "reviewer", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "reviewer", "kind": "gate", "spec": {"note": "принять правку: смотреть .diff и OUT-*-reviewer.md"}}]),
    ("code-review", "P4: только ревью текущего diff (security + reviewer) + гейт человека",
     ["development", "administration"],
     [{"role": "security", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "reviewer", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "reviewer", "kind": "gate", "spec": {"note": "принять/отклонить замечания ревью"}}]),
    ("code-feature", "P4: фича целиком — planner → Pi-роли → гейт человека → docs",
     ["development", "administration"],
     [{"role": "planner", "kind": "worker", "spec": {"plan": True}},
      {"role": "implementer", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 3600}},
      {"role": "qa", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "reviewer", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "security", "kind": "worker", "spec": {"worker": "pi", "timeout_s": 1800}},
      {"role": "reviewer", "kind": "gate", "spec": {"note": "принять фичу: diff + выводы qa/reviewer/security"}}]),
]

# Roles whose lines are not deployed yet (P3). Seeded inactive: planner never
# sees them (registry_roles filters active), dispatcher rejects steps on them.
PENDING_ROLES = ("db-auditor", "db-perf", "db-migration", "report")

app = FastAPI(title="agents-orchestrator", docs_url=None, redoc_url=None)


def db():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def ev(conn, task_id, level, message, artifact=None):
    conn.execute("insert into task_events(task_id, level, message, artifact) values (%s,%s,%s,%s)",
                 (task_id, level, message, artifact))


def touch(conn, task_id):
    conn.execute("update tasks set updated_at=now() where id=%s", (task_id,))


# ── workers (P1: in-process) ─────────────────────────────────────────────
def w_echo(task, step, spec):
    return "echo: " + json.dumps(task["payload"], ensure_ascii=False)[:500], []


def _vlm_ocr_pdf(pdf_path: Path, outdir: Path, max_pages: int = 60):
    """VLM-OCR сканов нашей же Qwen3.8-27B (vision): страницы → Markdown.
    Значительно точнее tesseract на русских сканах (OmniDocBench 91.1)."""
    import base64, urllib.request
    pre = outdir / (pdf_path.stem + "_pages"); pre.mkdir(exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-r", "150", str(pdf_path), str(pre / "pg")],
                   check=True, capture_output=True, timeout=1800)
    pages = sorted(pre.glob("pg-*.png"))[:max_pages]
    if not pages:
        return None
    out_blocks = []
    for i, pg in enumerate(pages, 1):
        b64 = base64.b64encode(pg.read_bytes()).decode()
        body = {"model": "qwen3.8-27b", "max_tokens": 4096,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Перепиши страницу документа дословно в Markdown (русский язык). "
                                             "Сохрани заголовки, списки, таблицы (markdown-таблицами), "
                                             "подписи/печати помечай [подпись]/[печать]. Без комментариев."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(GATEWAY + "/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + ORCH_LLM_KEY})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                out_blocks.append(f"--- страница {i} ---\n\n" + json.load(r)["choices"][0]["message"]["content"])
        except Exception:
            return None   # fallback: ocrmypdf-ветка
    return "\n\n".join(out_blocks)


GLOSSARY = Path(os.environ.get("RAG_GLOSSARY", "/srv/agents/glossary.md"))

_TR = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
       "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
       "х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"}
_TRMAP = {**{ord(k): v for k, v in _TR.items()}, **{ord(k.upper()): v.capitalize() for k, v in _TR.items()}}

def _safe_name(name: str) -> str:
    """Единая санитизация имён индекса: транслит, пробелы->_, только [A-Za-z0-9._-]."""
    s = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._-]+", "_", name.translate(_TRMAP))).strip("._-")
    return s or "doc"


def w_doc_convert(task, step, spec):
    fname = (task["payload"] or {}).get("file")
    if not fname:
        raise ValueError("payload.file обязателен (имя файла в /srv/agents/kb-inbox)")
    src = INBOX / Path(fname).name          # basename only: no path escape
    if not src.is_file():
        raise ValueError(f"файл не найден в inbox: {src}")
    outdir = ART / f"task-{task['id']}"; outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / (src.stem + ".md")
    if src.suffix.lower() in (".md", ".html", ".htm", ".docx", ".odt", ".rtf"):
        subprocess.run(["pandoc", str(src), "-t", "gfm", "-o", str(out)], check=True, timeout=600)
    elif src.suffix.lower() == ".pdf":
        txt = subprocess.run(["pdftotext", "-layout", str(src), "-"], capture_output=True, timeout=600)
        has_text = len(txt.stdout.strip()) > 200
        work = src
        if not has_text:
            vlm_md = _vlm_ocr_pdf(src, outdir) if ORCH_LLM_KEY else None
            if vlm_md:
                out.write_text(f"# {src.name}\n\n_конвертировано: VLM-OCR (qwen3.8-27b vision), "
                               f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z_\n\n{vlm_md}\n", encoding="utf-8")
                return f"конвертация готова (VLM-OCR): {out}", [str(out)]
            ocr = outdir / (src.stem + ".ocr.pdf")
            subprocess.run(["ocrmypdf", "--language", "rus+eng", "-q", str(src), str(ocr)], check=True, timeout=3600)
            work = ocr
        subprocess.run(["pdftotext", "-layout", str(work), str(outdir / (src.stem + ".txt"))], check=True, timeout=600)
        t = (outdir / (src.stem + ".txt")).read_text(encoding="utf-8", errors="replace")
        out.write_text(f"# {src.name}\n\n_конвертировано: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z_\n\n```\n{t}\n```\n", encoding="utf-8")
    else:
        raise ValueError(f"неподдерживаемый тип: {src.suffix}")
    return f"конвертация готова: {out}", [str(out)]


def w_not_implemented(task, step, spec):
    raise ValueError("worker появится в P3 (MSSQL-линия)")


def _rag_post(path, body):
    import urllib.request
    req = urllib.request.Request(RAG_SVC + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)


def w_rag_index(task, step, spec):
    arts = task["artifacts"] if isinstance(task.get("artifacts"), list) else []
    md = None
    for a in arts:
        if a.endswith(".md"):
            md = Path(a); break
    if md is None or not md.is_file():
        raise ValueError("rag_index: нет .md-артефакта от шага doc_convert")
    idx_name = _safe_name(md.stem)
    # метаданные нужны фильтрам rag-svc v2 (doctype/source_path); специальность и версию
    # для КР определяет массовый импорт (scripts/19), здесь — только происхождение файла
    r = _rag_post("/index", {"name": idx_name,
                             "text": md.read_text(encoding="utf-8", errors="replace"),
                             "meta": {"doctype": "inbox", "source_path": str(md),
                                      "title": md.stem, "current": 1}})
    return f"проиндексировано: {r['doc']} ({r['chunks']} чанков)", []


def w_rag_query(task, step, spec):
    q = (task["payload"] or {}).get("question")
    if not q:
        raise ValueError("payload.question обязателен для doc-qa")
    resp = _rag_post("/query", {"q": q, "k": 6})
    res = resp["results"]
    if not res:
        # rag-svc v2 различает «индекс пуст» и «нашлось, но ниже порога релевантности».
        # Сообщать «корпус пуст» во втором случае — значит отправлять админа искать
        # несуществующую проблему с загрузкой документов.
        note = resp.get("note") or "ничего не найдено"
        dropped = resp.get("dropped") or 0
        if dropped:
            return (f"в корпусе найдено {dropped} фрагмент(ов), но все ниже порога "
                    f"релевантности {resp.get('min_score')}: {note}. Проверьте "
                    f"RAG_MIN_SCORE и метаданные (specialty/doctype)."), []
        return f"{note}: сначала выполните doc-convert", []

    def _cite(r):
        """Цитата с редакцией: для КР «документ» без версии клинически бесполезен."""
        bits = [f"источник: {r['doc']}#{r['idx']}"]
        if r.get("version"):
            bits.append(f"редакция {r['version']}")
        if r.get("specialty"):
            bits.append(r["specialty"])
        if r.get("current") is False:
            bits.append("УСТАРЕВШАЯ РЕДАКЦИЯ — не использовать для решений")
        if r.get("score") is not None:
            bits.append(f"score={r['score']}")
        else:
            bits.append("точное совпадение термина")
        return "[" + ", ".join(bits) + "]"

    ctx = "\n\n".join(f"{_cite(r)}\n{r['text']}" for r in res)
    import urllib.request
    body = {"model": "qwen3.8-27b",
            "messages": [
                {"role": "system", "content": "Отвечай ТОЛЬКО по приведённому корпусу, указывай источники "
                                              "[документ#чанк, редакция]. Если в корпусе есть только устаревшая "
                                              "редакция — прямо скажи об этом, не выдавай её за действующую. "
                                              "Если данных нет — пиши «в документах не найдено». "
                                              "Ответ — черновик для проверки специалистом. "
                                              + (f"Глоссарий организации (раскрывай аббревиатуры ТОЛЬКО по нему):\n{GLOSSARY.read_text(encoding='utf-8')}"
                                                 if GLOSSARY.is_file() else "")},
                {"role": "user", "content": f"Вопрос: {q}\n\nКорпус:\n{ctx}"}],
            "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 1200}
    req = urllib.request.Request(GATEWAY + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + ORCH_LLM_KEY})
    with urllib.request.urlopen(req, timeout=600) as rr:
        ans = json.load(rr)["choices"][0]["message"]["content"]
    cites = ", ".join(f"{r['doc']}#{r['idx']}" for r in res[:3])
    return f"{ans}\n\n_источники: {cites}_", []


# ── git от имени владельца репозитория (P4) ────────────────────────────────
# Оркестратор работает от root (systemd-юнит без User=), репозитории в
# AGENTS_REPOS принадлежат PI_WORKDIR_USER. git ≥ 2.35 на чужой репозиторий от
# root отвечает «detected dubious ownership»: stdout пуст, и шаг молча теряет
# is_git/ветку/диф. Поэтому git зовём от владельца репо через `sudo -n -u`.
# Если sudo недоступен (не root, нет бинаря) — fallback: `-c safe.directory`
# ТОЛЬКО на время вызова, без записи в какой-либо gitconfig.
GIT_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_GIT_CACHE = {}


def _git_cmd(workdir):
    """Префикс команды git для workdir: [sudo, -n, -u, <user>, git] или [git].

    Проверяется один раз на репозиторий и кэшируется. Ничего не raise'ит:
    не-git каталог и недоступный git оба дают пустой вывод, который вызывающий
    код трактует как «не репозиторий».
    """
    key = str(workdir)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    probe_args = ["rev-parse", "--is-inside-work-tree"]
    plain = ["git", "-C", key]
    cmd = plain
    try:
        probe = subprocess.run(plain + probe_args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        probe = None
    if probe is not None and "dubious ownership" in (probe.stderr or "") and os.geteuid() == 0:
        as_user = ["sudo", "-n", "-u", PI_WORKDIR_USER, "git", "-C", key]
        try:
            p2 = subprocess.run(as_user + probe_args, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            p2 = None                           # sudo не установлен/недоступен
        if p2 is not None and p2.returncode == 0:
            cmd = as_user
        else:
            # sudo не помог: зовём git от себя, но с разовой меткой доверия к
            # этому пути. В gitconfig ничего не пишется — safe.directory живёт
            # только в аргументах конкретного вызова.
            cmd = plain + ["-c", f"safe.directory={key}"]
    _GIT_CACHE[key] = cmd
    return cmd


def _git_out(workdir, *args, timeout=120):
    """(stdout, stderr) git-команды от владельца репо. Исключений не бросает.

    stderr возвращается ТОЛЬКО при ненулевом exit-коде. Git пишет в stderr и
    информационные сообщения («Switched to a new branch», «Сброс ветки
    «task-7»»), а их текст зависит от локали — отфильтровать их по словам
    нельзя (v3.12.6 так и попал в артефакт ложный «ВНИМАНИЕ: git вернул
    ошибки»). Признак ошибки — код возврата, не лексика.
    """
    try:
        p = subprocess.run(_git_cmd(workdir) + list(args),
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"git {' '.join(args)}: {e}"
    if p.returncode == 0:
        return p.stdout or "", ""
    err = (p.stderr or "").strip() or f"git {' '.join(args)}: stderr пуст"
    return p.stdout or "", f"{err} [exit {p.returncode}]"


def _git_err(msg):
    """Нормализация текста ошибки git (пустые строки убрать).

    Решение «ошибка или нет» принимает _git_out по коду возврата; здесь только
    причесывание текста для шапки диф-артефакта.
    """
    keep = [ln for ln in (msg or "").splitlines() if ln.strip()]
    return "\n".join(keep).strip()


def _git_is_repo(workdir):
    out, _ = _git_out(workdir, "rev-parse", "--is-inside-work-tree", timeout=60)
    return out.strip() == "true"


MAX_UNTRACKED = int(os.environ.get("AGENTS_PI_MAX_UNTRACKED", "50"))


def _git_untracked(workdir):
    """Неотслеживаемые файлы (git diff их не показывает, а роль могла их создать)."""
    out, _ = _git_out(workdir, "ls-files", "--others", "--exclude-standard", timeout=60)
    return [ln for ln in out.splitlines() if ln.strip()][:MAX_UNTRACKED]


def _read_small_text(workdir, rel, limit):
    """Текст файла из репо (utf-8, без NUL) или None — бинарь/огромные не тащим."""
    f = Path(workdir) / rel
    try:
        if not f.is_file() or f.stat().st_size > limit:
            return None
        raw = f.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _untracked_section(workdir, files, per_file=4000, total=12000):
    """Раздел артефакта: список неотслеживаемых файлов + их текст (с лимитами)."""
    if not files:
        return ""
    parts = ["\n# ── НЕОТСЛЕЖИВАЕМЫЕ ФАЙЛЫ (git diff их не показывает) ──\n"]
    parts.append("# " + "\n# ".join(files) + "\n")
    budget = total
    for rel in files:
        if budget <= 0:
            parts.append(f"\n# ... остальное ({len(files)}) обрезано лимитом артефакта\n")
            break
        txt = _read_small_text(workdir, rel, per_file)
        if txt is None:
            parts.append(f"\n# ── {rel}: бинарный/слишком большой/нечитаемый, текст не приведён ──\n")
            continue
        chunk = txt[:min(per_file, budget)]
        parts.append(f"\n# ── новый файл: {rel} ──\n" + chunk
                     + ("\n# ... (обрезано)\n" if len(txt) > len(chunk) else ""))
        budget -= len(chunk)
    return "".join(parts)


def _git_rev(workdir, ref="HEAD"):
    out, _ = _git_out(workdir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                      timeout=60)
    return out.strip()


def _task_base_file(task_id):
    """Маркер базы задачи рядом с её артефактами."""
    return ART / f"task-{task_id}" / "base-rev.txt"


def _task_diff_base(task, workdir):
    """(база, HEAD сейчас, примечание) — база дифа на уровне ЗАДАЧИ, не шага.

    Первый Pi-шаг задачи фиксирует ревизию в маркере base-rev.txt; следующие
    шаги берут базу из него. Иначе коммит, сделанный ролью внутри задачи,
    сдвигает базу, и гейт-артефакт reviewer'а становится пустым
    («изменено файлов: 0», «коммитов после базы: 0») ровно тогда, когда
    смотреть нужнее всего. Маркер хранит и путь репо: задача с другим repo
    (freeform/смена payload) не унаследует чужую базу.
    """
    head_now = _git_rev(workdir)
    f = _task_base_file(task["id"])
    note = ""
    saved_wd = saved_rev = ""
    try:
        parts = f.read_text(encoding="utf-8").rstrip("\n").split("\t")
        saved_wd, saved_rev = (parts + ["", ""])[:2]
    except OSError:
        pass
    if saved_rev and saved_wd == str(workdir) and _git_rev(workdir, saved_rev):
        base = saved_rev
        if head_now and base != head_now:
            note = (f"HEAD на старте шага {head_now[:12]} != база задачи: "
                    "после базы были коммиты (база не сдвинута)")
    else:
        base = head_now
        if saved_rev and saved_wd == str(workdir):
            note = f"маркер базы {saved_rev[:12]} в репо не найден — база пересчитана"
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"{workdir}\t{base}\n", encoding="utf-8")
        except OSError as e:
            note = f"маркер базы не записан ({e}) — база только этого шага"
    return base, head_now, note


# ── Pi headless worker (P4) ────────────────────────────────────────────────
def _pi_available():
    return PI_RUNNER.is_file() and os.access(PI_RUNNER, os.X_OK)


def _pi_run(task, role, workdir, prompt_text, timeout_s):
    """Прогон роли Pi в workdir. Возвращает (текст вывода, путь OUT-*.md, rc).

    Оркестратор работает от root, раннер сам делает `sudo -u agent-dev`, поэтому
    каталог артефактов задачи делается group-writable: иначе Pi (agent-dev) не
    сможет записать туда свой OUT-файл.
    """
    if role not in PI_ROLES:
        raise ValueError(f"роль {role} не исполняется Pi (допустимы: {', '.join(PI_ROLES)})")
    if not _pi_available():
        raise ValueError(f"раннер Pi не найден: {PI_RUNNER} — выполните scripts/16-setup-code-line.sh")
    outdir = ART / f"task-{task['id']}"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(outdir, 0o2775)          # setgid + rwx для группы: agent-dev пишет OUT-*
    except OSError:
        pass
    # Карточку роли (/etc/agents-roles/<role>.md) подмешивает сам раннер.
    prompt = outdir / f"TASK-{task['id']}-{step_no(task)}-{role}.md"
    prompt.write_text(prompt_text[:MAX_PROMPT_CHARS], encoding="utf-8")
    outfile = outdir / f"OUT-{task['id']}-{step_no(task)}-{role}.md"
    # start_new_session: по таймауту убиваем ВСЮ группу процессов раннера.
    # Иначе осиротевший потомок pi держит наш пайп и communicate() висит вечно.
    proc = subprocess.Popen([str(PI_RUNNER), role, str(workdir), str(prompt), str(outfile), str(timeout_s)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    t0 = time.monotonic()
    timed_out = False
    rc = None
    # Жёсткий бюджет шага: раннер сам убивает pi через timeout(1) (код 124),
    # но если он завис или дал шагу докрутиться — оркестратор добивает группу
    # процессов и считает шаг таймаутом. Молча принимать такой шаг нельзя:
    # диспетчер однопоточный, один зависший Pi остановил бы всю очередь задач.
    while True:
        try:
            proc.communicate(timeout=5)
            rc = proc.returncode
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() - t0 > timeout_s + 15:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.communicate(timeout=15)
            except Exception:
                pass
            rc = proc.returncode if proc.returncode is not None else -1
            break
    elapsed = time.monotonic() - t0
    if rc == 124:                      # штатный таймаут внутри раннера
        timed_out = True
    try:
        os.chmod(outfile, 0o664)
    except OSError:
        pass
    txt = outfile.read_text(encoding="utf-8", errors="replace") if outfile.is_file() else ""
    if timed_out or rc == 124:
        raise ValueError(f"Pi ({role}): таймаут — бюджет шага {timeout_s} с, факт {elapsed:.0f} с; "
                         f"частичный вывод сохранён в {outfile}")
    if rc != 0:
        raise ValueError(f"Pi ({role}) завершился с кодом {rc}: {txt[-400:].strip() or 'вывод пуст'}")
    return txt, outfile, rc


def step_no(task):
    """Порядковый номер шага для имён файлов (0, если ещё не проставлен)."""
    return task.get("current_step") or 0


def prev_step_outputs(task):
    """Выводы предыдущих Pi-шагов этой задачи — контекст для следующей роли."""
    d = ART / f"task-{task['id']}"
    if not d.is_dir():
        return []
    return sorted(d.glob("OUT-*.md"), key=lambda p: p.stat().st_mtime)


def pi_verdict(role, txt):
    """Вердикт роли по её выводу. Никаких «всё хорошо по умолчанию»:
    нет явного PASS/APPROVE — считаем, что роль работу не приняла."""
    low = txt.lower()
    if role == "qa":
        if "pass" in low and "fail" not in low:
            return "PASS"
        return "FAIL" if "fail" in low else "FAIL(нет явного вердикта PASS)"
    if role == "reviewer":
        if "request_changes" in low or "request changes" in low:
            return "REQUEST_CHANGES"
        return "APPROVE" if "approve" in low else "без явного вердикта APPROVE/REQUEST_CHANGES"
    if role == "security":
        if re.search(r"\b(crit|critical|high)\b", low):
            return "есть находки crit/high"
        return "замечаний нет"
    return "ok"


def w_pi(task, step, spec):
    """Шаг кодовой линии: Pi headless от agent-dev в рабочем репозитории.

    payload: {"repo": "<имя из /srv/agents/repos>", "text": "<что сделать>"}
    spec:    {"timeout_s": 3600}
    Роль берётся из шага (implementer/qa/reviewer/security); цепочка передаёт
    друг другу предыдущие выводы, reviewer/security дополнительно получают diff
    от базы задачи. База — ревизия на старте ЗАДАЧИ: её фиксирует маркер
    base-rev.txt в каталоге артефактов (см. _task_diff_base), поэтому коммит
    роли внутри задачи не обнуляет диф гейта. Диф-артефакт <id>-<role>.diff
    пишется всегда — он материал человеческого гейта.

    git зовётся от владельца репозитория (см. _git_cmd): оркестратор работает
    от root, а репозитории принадлежат agent-dev, и git на такой расклад
    отвечает «dubious ownership» с пустым stdout.
    """
    payload = task["payload"] or {}
    repo = (payload.get("repo") or "").strip()
    text = (payload.get("text") or "").strip()
    if not repo:
        raise ValueError("payload.repo обязателен (имя каталога в /srv/agents/repos)")
    if not text:
        raise ValueError("payload.text обязателен (что нужно сделать)")
    workdir = (AGENT_REPOS / Path(repo).name).resolve()
    try:
        workdir.relative_to(AGENT_REPOS.resolve())
    except ValueError:
        raise ValueError(f"repo вне {AGENT_REPOS}: {workdir}")
    if not workdir.is_dir():
        raise ValueError(f"репозиторий не найден: {workdir} "
                         f"(клон: sudo -u {PI_WORKDIR_USER} git clone URL {workdir})")
    role = step["role"]
    if role not in PI_ROLES:
        raise ValueError(f"роль {role} не исполняется Pi (допустимы: {', '.join(PI_ROLES)})")

    is_git = _git_is_repo(workdir)
    head_before = ""
    base_note = ""
    git_errs = []
    diff_base = ""
    if is_git:
        diff_base, head_before, base_note = _task_diff_base(task, workdir)
        branch = f"task-{task['id']}"
        _, cerr = _git_out(workdir, "checkout", "-B", branch, timeout=120)
        cerr = _git_err(cerr)
        if cerr:
            git_errs.append(f"checkout -B {branch}: {cerr}")
    # База дифа ЗАДАЧИ (маркер base-rev.txt, см. _task_diff_base); для репо без
    # единого коммита — пустое дерево, чтобы первый коммит тоже попал в диф.
    diff_base = diff_base or GIT_EMPTY_TREE
    log_ref = "" if diff_base == GIT_EMPTY_TREE else diff_base

    bits = [f"РЕПОЗИТОРИЙ: {workdir}", f"ЗАДАЧА (id {task['id']}, шаг {step['idx']}): {text}"]
    if role in ("reviewer", "security") and is_git:
        d, derr = _git_out(workdir, "diff", diff_base)
        derr = _git_err(derr)
        if derr:
            git_errs.append(f"diff {diff_base[:12]}: {derr}")
        bits.append(f"GIT DIFF от базы задачи {diff_base[:12]} "
                    "(изменения предыдущих ролей, включая их коммиты; файлы НЕ менять):\n"
                    + (d[:MAX_PROMPT_CHARS] if d.strip() else "(diff пуст — изменений нет)"))
        unt = _git_untracked(workdir)
        if unt:
            bits.append("НЕОТСЛЕЖИВАЕМЫЕ ФАЙЛЫ (в git diff их нет; решить, нужны ли в коммите):\n"
                        + "\n".join(unt))
    prev = prev_step_outputs(task)
    if prev:
        ctx = []
        for p in prev[-3:]:
            t = p.read_text(encoding="utf-8", errors="replace")
            ctx.append(f"--- вывод предыдущей роли: {p.name} ---\n{t[:4000]}")
        bits.append("КОНТЕКСТ ЦЕПОЧКИ:\n" + "\n\n".join(ctx))
    prompt = "\n\n".join(bits)

    to = int(spec.get("timeout_s") or PI_TIMEOUT_S)
    out_txt, outfile, rc = _pi_run(task, role, workdir, prompt, to)

    diff_path = None
    log = ""
    if is_git:
        diff, derr = _git_out(workdir, "diff", diff_base)
        derr = _git_err(derr)
        if derr:
            git_errs.append(f"diff {diff_base[:12]}: {derr}")
        if log_ref:
            log, lerr = _git_out(workdir, "log", "--oneline", f"{log_ref}..HEAD", timeout=60)
            log = log.strip()
            lerr = _git_err(lerr)
            if lerr:
                git_errs.append(f"log {log_ref[:12]}..HEAD: {lerr}")
        # Артефакт гейта пишется всегда: даже пустой диф — это факт для человека
        # («роль ничего не изменила»), а не отсутствие данных. Ошибки git видны
        # в шапке, а не пропадают молча.
        diff_path = ART / f"task-{task['id']}" / f"task-{task['id']}-{role}.diff"
        nfiles = len([ln for ln in diff.splitlines() if ln.startswith("diff --git ")])
        untracked = _git_untracked(workdir)
        head_txt = (f"# база задачи: {diff_base}"
                    + (" (пустое дерево — в репо не было коммитов)"
                       if diff_base == GIT_EMPTY_TREE else "")
                    + f"\n# роль: {role}, шаг {step['idx']}, задача {task['id']}"
                    + (f"\n# примечание: {base_note}" if base_note else "")
                    + f"\n# изменено файлов: {nfiles}"
                    + (f"\n# коммитов после базы: {len(log.splitlines())}" if log_ref else "")
                    + ("\n# ВНИМАНИЕ: git вернул ошибки:\n"
                       + "".join(f"#   {e}\n" for e in git_errs) if git_errs else "")
                    + "\n")
        body = diff if diff.strip() else (
            "# диф пуст: рабочая копия не отличается от базы задачи\n"
            + (f"# коммиты после базы:\n{log}\n" if log else ""))
        head_txt += f"# неотслеживаемых файлов: {len(untracked)}\n"
        try:
            diff_path.write_text(head_txt + body + _untracked_section(workdir, untracked),
                                 encoding="utf-8")
            os.chmod(diff_path, 0o664)
        except OSError as e:
            git_errs.append(f"запись {diff_path}: {e}")
            diff_path = None
    if git_errs:
        log = (log + "\n" if log else "") + "; ".join(git_errs)

    verdict = pi_verdict(role, out_txt)
    arts = [str(outfile)] + ([str(diff_path)] if diff_path else [])
    summary = (f"{role}: {verdict}; вывод {outfile.name}"
               + (f"; diff {diff_path.name} ({nfiles} файл(ов) изменено"
                   f"{f', {len(untracked)} неотслеж.' if untracked else ''})" if diff_path
                  else ("; diff НЕ записан" if is_git else ""))
               + (f"; коммиты: {log.splitlines()[0]}" if log else ""))
    return summary, arts


WORKERS = {"echo": w_echo, "doc_convert": w_doc_convert, "rag_index": w_rag_index,
           "rag_query": w_rag_query, "not_implemented": w_not_implemented, "pi": w_pi}


# ── LLM planner (freeform, только для разрешённых групп) ─────────────────
def llm_plan(task):
    if not ORCH_LLM_KEY:
        raise ValueError("ORCH_LLM_KEY не задан — freeform недоступен")
    import urllib.request
    roles = registry_roles()
    body = {
        "model": "qwen3.8-27b",
        "messages": [
            {"role": "system", "content": "Ты — планировщик агентной сети. Верни СТРОГО JSON: "
             '{"steps":[{"role":<role из реестра>,"kind":"worker"|"gate","spec":{}}]}. '
             "Роли из реестра: " + ", ".join(f"{r}: {d}" for r, d in roles) + ". "
             "Не придумывай ролей вне реестра. gate ставь на действия с записью/публикацией/прод-БД."},
            {"role": "user", "content": json.dumps({"task": task["payload"].get("text", ""),
                                                    "template_hint": task["template"]}, ensure_ascii=False)},
        ],
        "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "xhigh"},
        "max_tokens": 1200,
    }
    req = urllib.request.Request(GATEWAY + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + ORCH_LLM_KEY})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.load(r)
    content = resp["choices"][0]["message"]["content"]
    usage = resp.get("usage", {}).get("total_tokens", 0)
    start = content.find("{"); end = content.rfind("}") + 1
    plan = json.loads(content[start:end])
    steps = []
    for s in plan.get("steps", []):
        if s.get("role") not in {r for r, _ in roles}:
            raise ValueError(f"planer предложил роль вне реестра: {s.get('role')}")
        steps.append({"role": s["role"], "kind": s.get("kind", "worker"), "spec": s.get("spec", {})})
    if not steps:
        raise ValueError("пустой план")
    return steps, usage


def registry_roles():
    with db() as c:
        return [(r["role"], r["description"]) for r in c.execute(
            "select role, description from agents_registry where active order by role").fetchall()]


# ── dispatcher ────────────────────────────────────────────────────────────
def run_step(conn, task, step):
    conn.execute("update task_steps set status='running', started_at=now() where id=%s", (step["id"],))
    conn.commit()
    try:
        if step["kind"] == "gate":
            conn.execute("update task_steps set status='awaiting_approval' where id=%s", (step["id"],))
            conn.execute("update tasks set status='awaiting_approval' where id=%s", (task["id"],))
            ev(conn, task["id"], "gate", f"шаг {step['idx']} ({step['role']}): ожидает решения человека — {step['spec'].get('note','')}")
            conn.commit()
            return "pause"
        spec = step["spec"] or {}
        wname = spec.get("worker")
        if spec.get("plan"):
            steps, usage = llm_plan(task)
            conn.execute("update tasks set tokens_used = tokens_used + %s where id=%s", (usage, task["id"]))
            with db() as c2:   # materialize planned steps after current one
                for i, s in enumerate(steps):
                    c2.execute("insert into task_steps(task_id, idx, role, kind, spec) values (%s,%s,%s,%s,%s)",
                               (task["id"], 100 + i, s["role"], s["kind"], json.dumps(s["spec"])))
                ev(c2, task["id"], "info", f"planner: материализовано шагов: {len(steps)}")
            out, arts = f"план: {len(steps)} шагов", []
        elif wname in WORKERS:
            out, arts = WORKERS[wname](task, step, spec)
        elif step["role"] in PI_ROLES and _pi_available():
            # LLM-план не знает про worker'ы: роль кодовой линии исполняет Pi
            out, arts = w_pi(task, step, spec)
        else:
            out, arts = f"(SKEL: роль {step['role']} активна, но внешний runner приедет в P2/P4; шаг-заглушка)", []
            ev(conn, task["id"], "warn", f"шаг {step['idx']} ({step['role']}): выполнен как заглушка каркаса P1")
        conn.execute("update task_steps set status='done', finished_at=now(), output=%s where id=%s", (out, step["id"]))
        if arts:
            conn.execute("update tasks set artifacts = array_cat(artifacts, %s) where id=%s", (arts, task["id"]))
        ev(conn, task["id"], "info", f"шаг {step['idx']} ({step['role']}) выполнен: {out[:200]}", arts[0] if arts else None)
        conn.commit()
        return "next"
    except Exception as e:
        conn.execute("update task_steps set status='failed', finished_at=now(), output=%s where id=%s", (str(e)[:2000], step["id"]))
        conn.execute("update tasks set status='failed', result=%s where id=%s", (str(e)[:2000], task["id"]))
        ev(conn, task["id"], "error", f"шаг {step['idx']} ({step['role']}): {e}")
        conn.commit()
        return "stop"


def dispatcher_loop():
    while True:
        try:
            with db() as conn:
                row = conn.execute(
                    "select id, template, payload, requester, grp, status, current_step, token_budget, tokens_used, artifacts "
                    "from tasks where status in ('new','running') order by id limit 1").fetchone()
                if not row:
                    time.sleep(2); continue
                task = dict(row)
                conn.execute("update tasks set status='running' where id=%s", (task["id"],))
                if task["status"] == "new":
                    # materialize steps: template or freeform plan
                    active_roles = {r for r, _ in registry_roles()}
                    if task["template"]:
                        t = conn.execute("select steps from templates where name=%s", (task["template"],)).fetchone()
                        steps = t["steps"] if t else []
                        bad = [x["role"] for x in steps if x["role"] not in active_roles]
                        if bad:
                            conn.execute("update tasks set status='failed', result=%s where id=%s",
                                         (f"роли не развёрнуты (см. roadmap): {bad}", task["id"]))
                            ev(conn, task["id"], "error", f"шаблон требует неразвёрнутых ролей: {bad}")
                            conn.commit(); time.sleep(1); continue
                        for i, s in enumerate(steps):
                            conn.execute("insert into task_steps(task_id, idx, role, kind, spec) values (%s,%s,%s,%s,%s)",
                                         (task["id"], i, s["role"], s.get("kind", "worker"), json.dumps(s.get("spec", {}))))
                        ev(conn, task["id"], "info", f"задача по шаблону {task['template']}: шагов {len(steps)}")
                    else:
                        if task["grp"] not in FREEFORM_GROUPS:
                            conn.execute("update tasks set status='failed', result='freeform запрещён для группы' where id=%s", (task["id"],))
                            ev(conn, task["id"], "error", "freeform-задачи доступны только группам development/administration")
                            conn.commit(); time.sleep(1); continue
                        steps, usage = llm_plan(task)
                        conn.execute("update tasks set tokens_used=tokens_used+%s where id=%s", (usage, task["id"]))
                        bad = [x["role"] for x in steps if x["role"] not in active_roles]
                        if bad:
                            conn.execute("update tasks set status='failed', result=%s where id=%s",
                                         (f"planer предложил неразвёрнутые роли: {bad}", task["id"]))
                            ev(conn, task["id"], "error", f"LLM-план отклонён: роли не развёрнуты: {bad}")
                            conn.commit(); time.sleep(1); continue
                        for i, s in enumerate(steps):
                            conn.execute("insert into task_steps(task_id, idx, role, kind, spec) values (%s,%s,%s,%s,%s)",
                                         (task["id"], i, s["role"], s["kind"], json.dumps(s["spec"])))
                        ev(conn, task["id"], "info", f"LLM-план: шагов {len(steps)}")
                step = conn.execute(
                    "select * from task_steps where task_id=%s and status in ('pending','running') order by idx, id limit 1",
                    (task["id"],)).fetchone()
                if not step:
                    conn.execute("update tasks set status='done', result='все шаги выполнены' where id=%s", (task["id"],))
                    ev(conn, task["id"], "info", "задача завершена")
                    conn.commit(); continue
                conn.commit()
                res = run_step(conn, task, dict(step))
                if res == "next":
                    conn.execute("update tasks set current_step=current_step+1 where id=%s", (task["id"],))
                    conn.commit()
                time.sleep(0.5)
        except Exception:
            traceback.print_exc()
            time.sleep(5)


# ── API ──────────────────────────────────────────────────────────────────
def auth(req: Request):
    if req.headers.get("X-Orch-Key") != ORCH_API_KEY:
        raise HTTPException(401, "X-Orch-Key required")


@app.on_event("startup")
def startup():
    with db() as c:
        c.execute(SCHEMA)
        for r in SEED_REGISTRY:
            c.execute("insert into agents_registry(role, description, thinking, tools, mcp, gates, budget_default) "
                      "values (%s,%s,%s,%s,%s,%s,%s) on conflict (role) do nothing",
                      (r[0], r[1], r[2], json.dumps(r[3]), json.dumps(r[4]), r[5], r[6]))
        for t in SEED_TEMPLATES:
            c.execute("insert into templates(name, description, groups, steps) values (%s,%s,%s,%s) "
                      "on conflict (name) do update set steps=excluded.steps, groups=excluded.groups, "
                      "description=excluded.description",
                      (t[0], t[1], t[2], json.dumps(t[3])))
        c.execute("update agents_registry set active=false where role = any(%s)", (list(PENDING_ROLES),))
    ART.mkdir(parents=True, exist_ok=True); INBOX.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=dispatcher_loop, daemon=True).start()


@app.get("/api/tasks")
def list_tasks(status: str = ""):
    q = "select id, template, requester, grp, status, current_step, tokens_used, token_budget, result, created_at, updated_at from tasks"
    args = ()
    if status:
        q += " where status=%s"; args = (status,)
    q += " order by id desc limit 200"
    with db() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


@app.get("/api/tasks/{tid}")
def get_task(tid: int):
    with db() as c:
        t = c.execute("select * from tasks where id=%s", (tid,)).fetchone()
        if not t:
            raise HTTPException(404)
        return {"task": dict(t),
                "steps": [dict(r) for r in c.execute("select * from task_steps where task_id=%s order by idx, id", (tid,))],
                "events": [dict(r) for r in c.execute("select * from task_events where task_id=%s order by ts", (tid,))]}


@app.post("/api/tasks")
async def create_task(req: Request):
    auth(req)
    body = await req.json()
    template = body.get("template"); grp = body.get("group", "everyone")
    if template:
        with db() as c:
            t = c.execute("select groups from templates where name=%s", (template,)).fetchone()
            if not t:
                raise HTTPException(404, "нет такого шаблона")
            if "everyone" not in t["groups"] and grp not in t["groups"]:
                raise HTTPException(403, "шаблон недоступен группе")
    with db() as c:
        cur = c.execute("insert into tasks(template, payload, requester, grp, token_budget) values (%s,%s,%s,%s,%s) returning id",
                        (template, json.dumps(body.get("payload", {})), body.get("requester", "api"), grp,
                         body.get("token_budget", 200000)))
        tid = cur.fetchone()["id"]
        ev(c, tid, "info", f"задача создана (template={template or 'freeform'}, group={grp})")
    return {"id": tid}


@app.post("/api/tasks/{tid}/steps/{sid}/decision")
async def decide(tid: int, sid: int, req: Request):
    auth(req)
    body = await req.json()
    ok = body.get("decision") in ("approve", "reject")
    if not ok:
        raise HTTPException(400, "decision: approve|reject")
    with db() as c:
        s = c.execute("select * from task_steps where id=%s and task_id=%s", (sid, tid)).fetchone()
        if not s or s["status"] != "awaiting_approval":
            raise HTTPException(409, "шаг не ждёт решения")
        if body["decision"] == "approve":
            c.execute("update task_steps set status='done', finished_at=now(), output=%s where id=%s",
                      ("approved: " + body.get("comment", ""), sid))
            c.execute("update tasks set status='running' where id=%s", (tid,))
        else:
            c.execute("update task_steps set status='failed', finished_at=now(), output=%s where id=%s",
                      ("rejected: " + body.get("comment", ""), sid))
            c.execute("update tasks set status='failed', result='отклонено на gate' where id=%s", (tid,))
        ev(c, tid, "gate", f"решение человека: {body['decision']} ({body.get('comment','')})")
    return {"ok": True}


@app.post("/api/inbox")
async def inbox_upload(request: Request, file: UploadFile = File(...), make_task: str = "doc-convert"):
    """Web-form/script upload into kb-inbox (+ optional immediate task)."""
    auth(request)
    name = Path(file.filename or "upload.bin").name
    if not re.match(r"^[A-Za-z0-9._-]+$", name):
        raise HTTPException(400, "имя файла: только латиница/цифры/._- (без пробелов и кириллицы)")
    dest = INBOX / name
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "файл > 200 МБ")
    dest.write_bytes(data)
    tid = None
    if make_task:
        with db() as c:
            cur = c.execute("insert into tasks(template, payload, requester, grp) values (%s,%s,%s,%s) returning id",
                            (make_task, json.dumps({"file": name}), "inbox-upload", "everyone"))
            tid = cur.fetchone()["id"]
            ev(c, tid, "info", f"файл загружен веб-формой: {name} ({len(data)} байт)")
    return {"saved": str(dest), "bytes": len(data), "task_id": tid}


@app.get("/api/registry")
def registry():
    with db() as c:
        return [dict(r) for r in c.execute("select * from agents_registry order by role")]


@app.get("/api/templates")
def templates():
    with db() as c:
        return [dict(r) for r in c.execute("select * from templates order by name")]


@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse((WEB / "tasks-dashboard.html").read_text(encoding="utf-8"))
