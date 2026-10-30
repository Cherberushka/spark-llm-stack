#!/usr/bin/env python3
"""
test-mcp-e2e.py — сквозная проверка линии «агент -> MCP -> rag-svc -> шлюз эмбеддингов».

Поднимает всё по-настоящему, но без Ollama и без GPU:
  * фейковый шлюз /v1/embeddings (детерминированные векторы из мешка слов);
  * rag-svc под uvicorn как отдельный процесс (HTTP, не TestClient);
  * rag-mcp-server.py как отдельный процесс, общение по stdio JSON-RPC.

Проверяет: handshake MCP, tools/list, все 4 инструмента, фильтры по метаданным,
что устаревшая редакция не попадает в выдачу, и внятную ошибку при недоступном сервисе.

Запуск:  python3 research/test-mcp-e2e.py            (нужны fastapi, uvicorn, numpy)
На Spark: /opt/rag-venv/bin/python research/test-mcp-e2e.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
SVC = REPO / "agents" / "orchestrator" / "rag-svc.py"
MCP = REPO / "agents" / "mcp" / "rag-mcp-server.py"
DIM = 1024
FAIL = []


def check(cond, label, extra=""):
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  {extra}" if not cond and extra else ""))
    if not cond:
        FAIL.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------- фейковый шлюз
_wcache = {}


def _word_vec(w):
    if w not in _wcache:
        import hashlib
        h = hashlib.sha256(w.encode()).digest()
        g = np.random.default_rng(int.from_bytes(h[:8], "big"))
        v = g.standard_normal(DIM).astype(np.float32)
        _wcache[w] = v / np.linalg.norm(v)
    return _wcache[w]


def _embed_one(text):
    toks = [w for w in "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
            if len(w) > 2]
    v = np.zeros(DIM, dtype=np.float32)
    for t in toks:
        v += _word_vec(t)
    n = np.linalg.norm(v)
    return (v / n) if n > 1e-9 else _word_vec("пусто")


class FakeGateway(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):                             # noqa: ARG002
        pass

    def do_POST(self):                                     # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0))
        if not self.path.endswith("/embeddings"):
            self.send_error(404); return
        FakeGateway.calls += 1
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [{"object": "embedding", "index": i, "embedding": _embed_one(t).tolist()}
                for i, t in enumerate(texts)]
        out = json.dumps({"object": "list", "model": body.get("model", "bge-m3"),
                          "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


# --------------------------------------------------------------------- MCP-клиент
class McpClient:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, str(MCP)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=env, bufsize=1)
        self.id = 0

    def call(self, method, params=None, notify=False):
        self.id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self.id
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        if not line:
            err = self.p.stderr.read()
            raise RuntimeError(f"MCP-сервер закрыл stdout. stderr: {err[-800:]}")
        return json.loads(line)

    def tool(self, tool_name, **args):
        # tool_name, а не name: иначе коллизия с аргументом name у kb_doc_chunks
        r = self.call("tools/call", {"name": tool_name, "arguments": args})
        if "error" in r:
            return None, r["error"], True
        c = r["result"]["content"][0]["text"]
        return c, r["result"], r["result"].get("isError", False)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:                                  # noqa: BLE001
            self.p.kill()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="rage2e-"))
    print(f"# рабочий каталог: {tmp}")
    check(SVC.exists() and MCP.exists(), "найдены rag-svc.py и rag-mcp-server.py",
          extra=f"{SVC} / {MCP}")

    gw_port, svc_port = free_port(), free_port()
    gw = ThreadingHTTPServer(("127.0.0.1", gw_port), FakeGateway)
    threading.Thread(target=gw.serve_forever, daemon=True).start()

    # uvicorn не может импортировать модуль с дефисом в имени — копируем
    app_copy = tmp / "rag_svc_app.py"
    shutil.copyfile(SVC, app_copy)
    env = dict(os.environ,
               RAG_SVC_DB=str(tmp / "index.db"), RAG_SVC_DIR=str(tmp),
               RAG_BACKEND="sqlite", RAG_DIM=str(DIM), RAG_QUERY_MODE="hybrid",
               RAG_CURRENT_ONLY="1", RAG_DENSE_RESIDENT_MB="4096",
               GATEWAY_URL=f"http://127.0.0.1:{gw_port}/v1", ORCH_LLM_KEY="test-key")
    log = open(tmp / "rag-svc.log", "wb")
    svc = subprocess.Popen([sys.executable, "-m", "uvicorn", "rag_svc_app:app",
                            "--host", "127.0.0.1", "--port", str(svc_port), "--log-level", "warning"],
                           cwd=str(tmp), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{svc_port}"
    up = False
    for _ in range(60):
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    up = True
                break
        except Exception:                                  # noqa: BLE001
            time.sleep(0.5)
            if svc.poll() is not None:
                break
    check(up, "rag-svc поднялся по HTTP", extra=(tmp / "rag-svc.log").read_text()[-500:] if not up else "")
    if not up:
        svc.kill(); gw.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
        return 1

    try:
        # --- наполняем корпус через настоящий HTTP-путь (embed -> фейковый шлюз)
        docs = [
            ("kr_arterialnaya_gipertenziya.pdf", "кардиология", "clinrec", "2024",
             "Артериальная гипертензия. Клинические рекомендации.\n\nПервая линия терапии — "
             "ингибиторы АПФ и сартаны. Целевое артериальное давление ниже 140/90 мм рт. ст. "
             "Код МКБ-10 I10. При недостаточном контроле добавляют диуретик индапамид."),
            ("kr_saharnyy_diabet_2.pdf", "эндокринология", "clinrec", "2025",
             "Сахарный диабет 2 типа. Клинические рекомендации.\n\nПервая линия — метформин. "
             "Целевой HbA1c ниже 7%. Код МКБ-10 E11. Обязателен контроль артериального давления."),
            ("prikaz_citologiya_gistologiya.pdf", "патологическая анатомия", "order", None,
             "Приказ о порядке оказания помощи по профилю патологическая анатомия.\n\n"
             "Цитологическое исследование и гистологическое исследование: сроки хранения "
             "препаратов, маркировка направительного документа, журнал учёта."),
        ]
        for name, sp, dt, ver, text in docs:
            body = json.dumps({"name": name, "text": text,
                               "meta": {"title": name.replace(".pdf", "").replace("_", " "),
                                        "specialty": sp, "doctype": dt, "version": ver,
                                        "mkb10": "I10" if "gipert" in name else ("E11" if "diabet" in name else None),
                                        "current": 1}}).encode()
            req = urllib.request.Request(base + "/index", data=body,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            check(d.get("chunks", 0) > 0, f"индексация {name}: {d.get('chunks')} чанков")
        check(FakeGateway.calls >= len(docs), f"шлюз эмбеддингов вызван {FakeGateway.calls} раз")

        # --- MCP-клиент
        print("\n=== MCP-протокол ===")
        mcp = McpClient(dict(os.environ, RAG_SVC_URL=base))
        init = mcp.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}})
        check(init.get("result", {}).get("serverInfo", {}).get("name") == "spark-rag",
              f"initialize: serverInfo={init.get('result', {}).get('serverInfo')}")
        mcp.call("notifications/initialized", notify=True)
        tl = mcp.call("tools/list")
        names = [t["name"] for t in tl.get("result", {}).get("tools", [])]
        check(names == ["kb_search", "kb_list_docs", "kb_doc_chunks", "kb_stats"],
              f"tools/list: {names}")
        check(all("inputSchema" in t for t in tl["result"]["tools"]),
              "у всех инструментов есть inputSchema")

        txt, res, is_err = mcp.tool("kb_search", query="первая линия терапии при артериальной гипертензии", k=3)
        check(not is_err and "doc: kr_arterialnaya_gipertenziya.pdf" in txt,
              "kb_search: найден нужный документ И отдан его ключ (нужен для kb_doc_chunks)",
              extra=(txt or "")[:400])
        check("ред. 2024" in txt, "kb_search: в выдаче видна версия редакции")
        check("цитируйте" in txt.lower() or "Цитируйте" in txt or "ВАЖНО" in txt,
              "kb_search: агенту передаётся требование цитировать")

        txt2, _, _ = mcp.tool("kb_search", query="первая линия терапии", specialty="эндокринология", k=5)
        check("saharnyy_diabet" in txt2 and "gipertenziya" not in txt2,
              "kb_search: фильтр по специальности работает", extra=(txt2 or "")[:300])

        txt3, _, _ = mcp.tool("kb_search", query="цитологическое исследование хранение препаратов",
                             doctype="order", k=3)
        check("prikaz_citologiya" in txt3, "kb_search: фильтр по типу документа работает")

        # порог релевантности: нерелевантный запрос не должен выдавать мусор за результат
        txt4, _, err4 = mcp.tool("kb_search", query="квантовая телепортация протоны плазма",
                                k=3, min_score=0.3)
        check(not err4 and "ничего не найдено" in txt4.lower(),
              "kb_search: min_score отсекает нерелевантное -> честное «не найдено»",
              extra=(txt4 or "")[:300])
        check("Не выдумывайте" in txt4 or "не выдумывайте" in txt4.lower(),
              "kb_search: агенту явно запрещено додумывать ответ")

        txt5, _, _ = mcp.tool("kb_list_docs")
        check(txt5.count("фрагм.") >= 3, "kb_list_docs: все 3 документа в списке",
              extra=(txt5 or "")[:200])
        txt6, _, _ = mcp.tool("kb_list_docs", contains="diabet")
        check("kr_saharnyy_diabet_2.pdf" in txt6, "kb_list_docs: фильтр contains по имени")
        txt6b, _, _ = mcp.tool("kb_list_docs", contains="кардиология")
        check("gipertenziya" in txt6b and "diabet" not in txt6b,
              "kb_list_docs: фильтр contains по специальности", extra=(txt6b or "")[:300])

        txt7, _, err7 = mcp.tool("kb_doc_chunks", name="prikaz_citologiya_gistologiya.pdf", k=2)
        check(not err7 and "цитологическое" in txt7.lower(), "kb_doc_chunks: превью документа")
        _, _, err8 = mcp.tool("kb_doc_chunks", name="нет-такого-документа")
        check(err8, "kb_doc_chunks: неизвестный документ -> isError=true")

        txt9, _, _ = mcp.tool("kb_stats")
        check("движок: sqlite" in txt9 and "фрагментов: " in txt9,
              "kb_stats: состояние корпуса", extra=(txt9 or "")[:200])
        check("рекомендация:" in txt9, "kb_stats: есть рекомендация по порогам")

        # --- устаревшая редакция
        body = json.dumps({"name": "kr_ag_2020.pdf", "text":
                           "Артериальная гипертензия, редакция 2020 года. Устаревшие целевые значения "
                           "артериального давления 150 на 90.",
                           "meta": {"title": "Артериальная гипертензия 2020",
                                    "specialty": "кардиология", "doctype": "clinrec",
                                    "version": "2020", "current": 1}}).encode()
        urllib.request.urlopen(urllib.request.Request(base + "/index", data=body, headers={
            "Content-Type": "application/json"}), timeout=60).read()
        urllib.request.urlopen(urllib.request.Request(
            base + "/supersede", data=json.dumps({"name": "kr_ag_2020.pdf",
                                                  "by": "kr_arterialnaya_gipertenziya.pdf",
                                                  "current": False}).encode(),
            headers={"Content-Type": "application/json"}), timeout=60).read()
        txt10, _, _ = mcp.tool("kb_search", query="устаревшие целевые значения артериального давления 150", k=5)
        check("kr_ag_2020" not in txt10, "устаревшая редакция не в выдаче MCP-поиска")
        txt11, _, _ = mcp.tool("kb_search", query="устаревшие целевые значения артериального давления 150",
                              k=5, include_superseded=True)
        check("kr_ag_2020" in txt11, "include_superseded=true показывает устаревшую редакцию")
        txt12, _, _ = mcp.tool("kb_stats")
        check("устаревших редакций: 1" in txt12, "kb_stats: учитывает устаревшие редакции")
        mcp.close()

        # --- поведение при недоступном сервисе: внятная ошибка, а не стектрейс
        print("\n=== отказоустойчивость ===")
        dead = McpClient(dict(os.environ, RAG_SVC_URL="http://127.0.0.1:1"))
        dead.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}})
        t, r, is_err = dead.tool("kb_search", query="что-нибудь")
        check(is_err and "недоступен" in t, "MCP отдаёт внятную ошибку, если rag-svc недоступен",
              extra=(t or "")[:200])
        dead.close()
    finally:
        svc.terminate()
        try:
            svc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            svc.kill()
        gw.shutdown()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL:
        print(f"✗ ПРОВАЛЕНО: {len(FAIL)}")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print("✓ сквозной тест MCP -> rag-svc пройден")
    return 0


if __name__ == "__main__":
    sys.exit(main())
