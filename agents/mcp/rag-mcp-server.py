#!/usr/bin/env python3
"""
rag-mcp-server.py — MCP-сервер (stdio) поверх rag-svc: единая точка доступа к
организационной базе знаний для любого агента (Pi, DeepSeek Harness, свой код).

Зачем: до этого корпус был доступен только оркестратору и Open WebUI Tool, а у
агентов на рабочих станциях не было способа спросить «что у нас в КР по X».
MCP-обёртка решает ровно это и НЕ заводит второе хранилище: источник правды один —
rag-svc (см. KNOWLEDGE-WIKI §«три индекса»).

Без внешних зависимостей (только stdlib): протокол MCP/JSON-RPC 2.0 по stdio
реализован вручную, поэтому сервер ставится на рабочую станцию одним файлом и
работает офлайн.

Инструменты:
  kb_search      — гибридный поиск (BM25 + dense, RRF) с фильтрами по метаданным
  kb_list_docs   — список документов корпуса со специальностью/версией/признаком «действующий»
  kb_doc_chunks  — превью чанков конкретного документа
  kb_stats       — состояние корпуса и рекомендация порога (когда включать Qdrant)

Подключение в Pi (~/.pi/…/mcp или AGENTS.md — см. agents/mcp/WINDOWS-MCP-GUIDE.md):
  command: python3
  args: ["/путь/к/rag-mcp-server.py"]
  env:   RAG_SVC_URL=http://192.168.11.251:8502   # если сервис открыт в LAN,
                                                  # по умолчанию он на 127.0.0.1
Локально на сервере env не нужен. С рабочей станции — нужен туннель (ssh -L 8502:…)
или публикация порта через Caddy с авторизацией; наружу 8502 не открываем.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

RAG = os.environ.get("RAG_SVC_URL", "http://127.0.0.1:8502")
TOKEN = os.environ.get("RAG_SVC_TOKEN", "")          # если появится авторизация
TIMEOUT = int(os.environ.get("RAG_MCP_TIMEOUT", "120"))
SERVER_INFO = {"name": "spark-rag", "version": "2.0.0"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "kb_search",
        "description": (
            "Поиск по организационной базе знаний клиники: клинические рекомендации, "
            "приказы, СОП, внутренние регламенты. Гибридный поиск (лексический BM25 + "
            "семантический), возвращает фрагменты с указанием документа и его версии. "
            "По умолчанию ищет только по ДЕЙСТВУЮЩИМ редакциям. Всегда цитируйте "
            "документ и версию в ответе; если результатов нет — так и скажите, не "
            "додумывайте."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Вопрос или ключевые термины (русский)"},
                "k": {"type": "integer", "description": "Сколько фрагментов вернуть (1-20, по умолчанию 6)",
                      "minimum": 1, "maximum": 20},
                "specialty": {"type": "string", "description": "Фильтр по специальности, напр. «кардиология»"},
                "doctype": {"type": "string",
                            "description": "Фильтр по типу: clinrec | standard | order | sop | other"},
                "mkb10": {"type": "string", "description": "Фильтр по коду МКБ-10, напр. «I10»"},
                "mode": {"type": "string", "enum": ["hybrid", "dense", "bm25"],
                         "description": "Режим ранжирования (по умолчанию hybrid)"},
                "include_superseded": {"type": "boolean",
                                       "description": "true = искать и в устаревших редакциях (аудит)"},
                "min_score": {"type": "number",
                              "description": "Порог косинусной близости (0-1). Фрагменты ниже "
                                             "порога отбрасываются. По умолчанию берётся из "
                                             "настроек сервера; 0 = вернуть всё."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_list_docs",
        "description": "Список документов базы знаний: имя, число фрагментов, специальность, "
                       "тип, версия, признак действующей редакции. Нужен, чтобы понять, что "
                       "вообще есть в корпусе, прежде чем искать.",
        "inputSchema": {"type": "object", "properties": {
            "contains": {"type": "string",
                         "description": "Фильтр по подстроке в имени, заголовке, специальности или типе"}},
            "required": []},
    },
    {
        "name": "kb_doc_chunks",
        "description": "Первые фрагменты конкретного документа — чтобы понять его структуру "
                       "и охват, не вычитывая всё.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Имя документа (из kb_list_docs)"},
            "k": {"type": "integer", "description": "Сколько фрагментов показать (по умолчанию 3)"}},
            "required": ["name"]},
    },
    {
        "name": "kb_stats",
        "description": "Состояние базы знаний: сколько документов и фрагментов, какой поисковый "
                       "движок, латентность, заполненность по специальностям и рекомендация "
                       "по порогам масштабирования.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


# --------------------------------------------------------------------------- HTTP к rag-svc
def _req(method, path, body=None):
    url = RAG.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"rag-svc HTTP {e.code} на {path}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"rag-svc недоступен по адресу {RAG} ({e.reason}). Проверьте: "
            "systemctl status rag-svc; при запуске с рабочей станции — RAG_SVC_URL и туннель") from None


def _text(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)


def tool_kb_search(args):
    q = (args.get("query") or "").strip()
    if not q:
        return "пустой query", True
    filters = {}
    for key in ("specialty", "doctype", "mkb10"):
        if args.get(key):
            filters[key] = str(args[key]).strip()
    body = {"q": q, "k": int(args.get("k") or 6), "filters": filters}
    if args.get("mode") in ("hybrid", "dense", "bm25"):
        body["mode"] = args["mode"]
    if args.get("include_superseded"):
        body["current_only"] = False
    if args.get("min_score") is not None:
        body["min_score"] = float(args["min_score"])
    r = _req("POST", "/query", body)
    res = r.get("results") or []
    if not res:
        why = r.get("note") or "ничего не найдено"
        return ("В базе знаний ничего не найдено по запросу «%s»%s. Причина: %s. "
                "Не выдумывайте ответ: скажите, что источника в корпусе клиники нет, "
                "и предложите уточнить специальность или добавить документ "
                "(scripts/19-import-clinrec.sh)."
                % (q, f" (фильтры: {filters})" if filters else "", why)), False
    mode = r.get("search", {}).get("mode")
    dropped = r.get("dropped") or 0
    out = [f"Найдено фрагментов: {len(res)} (режим {mode}"
           + (f", отброшено по порогу релевантности: {dropped}" if dropped else "") + ").",
           "ВАЖНО: цитируйте документ и версию; это первоисточник клиники, а не общие знания.", ""]
    for i, x in enumerate(res, 1):
        head = x.get("title") or x.get("doc")
        ver = f", ред. {x['version']}" if x.get("version") else ""
        cur = "" if x.get("current", True) else " [УСТАРЕВШАЯ РЕДАКЦИЯ]"
        sc = f" score={x['score']}" if x.get("score") is not None else " (лексическое совпадение)"
        # ключ документа обязателен: по нему работают kb_doc_chunks и /supersede
        out.append(f"[{i}] {head}{ver}{cur} — фрагмент {x.get('idx')}{sc}")
        out.append(f"    doc: {x.get('doc')}")
        if x.get("specialty"):
            out.append(f"    специальность: {x['specialty']}"
                       + (f", МКБ-10: {x['mkb10']}" if x.get("mkb10") else ""))
        if x.get("source_path"):
            out.append(f"    источник: {x['source_path']}")
        out.append("")
        out.append((x.get("text") or "").strip())
        out.append("")
    return "\n".join(out), False


def tool_kb_list_docs(args):
    r = _req("GET", "/docs")
    docs = r.get("docs") or []
    needle = (args.get("contains") or "").lower()
    if needle:
        docs = [d for d in docs if needle in " ".join(
        str(d.get(f) or "").lower()
        for f in ("name", "title", "specialty", "doctype"))]
    if not docs:
        return "Корпус пуст или по фильтру ничего нет. Импорт: scripts/19-import-clinrec.sh", False
    out = [f"Документов: {len(docs)} (всего фрагментов в корпусе: {r.get('total_chunks')})", ""]
    for d in docs:
        mark = "" if d.get("current", True) else " [УСТАРЕЛ]"
        sp = f" | {d['specialty']}" if d.get("specialty") else ""
        dt = f" | {d['doctype']}" if d.get("doctype") else ""
        v = f" | ред. {d['version']}" if d.get("version") else ""
        out.append(f"  {d['name']}{mark}{sp}{dt}{v} — {d['chunks']} фрагм.")
    return "\n".join(out), False


def tool_kb_doc_chunks(args):
    name = args.get("name") or ""
    if not name:
        return "нужен параметр name (имя документа из kb_list_docs)", True
    k = int(args.get("k") or 3)
    r = _req("GET", "/docs/" + urllib.parse.quote(name) + f"/chunks?k={k}")
    prev = r.get("preview") or []
    if not prev:
        return f"документ «{name}» не найден или пуст", True
    out = [f"Документ: {name} — первые {len(prev)} фрагментов:", ""]
    for x in prev:
        out.append(f"--- фрагмент {x.get('idx')} ---")
        out.append((x.get("text") or "").strip())
        out.append("")
    return "\n".join(out), False


def tool_kb_stats(args):
    s = _req("GET", "/stats")
    lat = s.get("query_latency_ms") or {}
    lines = [
        f"движок: {s.get('backend')}",
        f"документов: {s.get('docs')}, фрагментов: {s.get('chunks')}, dim: {s.get('dim')}",
        f"устаревших редакций: {s.get('superseded_docs')}",
        f"латентность p95: {lat.get('p95')} мс (замеров: {lat.get('n')})",
        f"место: index.db {(s.get('db_bytes') or 0) / 1e6:.0f} МБ, "
        f"векторы {(s.get('vectors_bytes') or 0) / 1e6:.0f} МБ",
        f"рекомендация: {s.get('recommendation')}",
    ]
    by = s.get("by") or {}
    if by.get("specialty"):
        lines.append("по специальностям: " + ", ".join(
            f"{k}={v}" for k, v in list(by["specialty"].items())[:8]))
    return "\n".join(lines), False


HANDLERS = {"kb_search": tool_kb_search, "kb_list_docs": tool_kb_list_docs,
            "kb_doc_chunks": tool_kb_doc_chunks, "kb_stats": tool_kb_stats}


# --------------------------------------------------------------------------- MCP/JSON-RPC
def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def error(rid, code, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(msg):
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        reply(rid, {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO})
    elif method in ("notifications/initialized", "initialized"):
        pass                                              # уведомление — ответ не нужен
    elif method == "ping":
        reply(rid, {})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            error(rid, -32602, f"неизвестный инструмент: {name}")
            return
        try:
            text, is_err = fn(args)
        except Exception as e:                            # noqa: BLE001
            text, is_err = f"{type(e).__name__}: {e}", True
        reply(rid, {"content": [{"type": "text", "text": text}], "isError": bool(is_err)})
    elif rid is not None:
        error(rid, -32601, f"метод не поддерживается: {method}")


def main():
    # stderr — единственный канал логов: stdout занят протоколом
    print(f"[rag-mcp] запущен, rag-svc = {RAG}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            error(None, -32700, f"некорректный JSON: {e}")
            continue
        if isinstance(msg, list):                          # батч
            for m in msg:
                handle(m)
        else:
            handle(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
