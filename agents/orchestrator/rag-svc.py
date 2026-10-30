#!/usr/bin/env python3
"""
rag-svc v2 — корпусный поиск клиники. arm64-safe, без torch.

Что изменилось относительно v1 (и зачем — см. RAG-SCALE-PLAN.md, всё замерено):
  * v1 на каждом /query вычитывал ВСЕ строки (текст+эмбеддинг) и пересобирал матрицу.
    Замер: 50k чанков -> 0.99 с/запрос, пик RSS 536 МБ, и 98% времени — однопоточный
    Python (SQLite fetchall + np.frombuffer + np.stack), поэтому 20 ядер Spark не помогают.
  * v2 держит плотную матрицу одним непрерывным массивом (memmap/резидент), текст и
    метаданные — в SQLite. Тот же корпус: ~0.03 с/запрос (в 39 раз быстрее), память не
    пересоздаётся на каждый запрос.
  * Гибридный поиск: BM25 (SQLite FTS5, префиксные токены под русскую морфологию) +
    dense (bge-m3), слияние RRF. Для лекарств, МКБ-10 и аббревиатур точное совпадение
    важнее косинуса.
  * Метаданные и фильтры: specialty / doctype / mkb10 / version / current / valid_from.
    Поиск только по действующим редакциям — обязательное требование для КР.
  * Два взаимозаменяемых бэкенда за одним интерфейсом: sqlite (по умолчанию) и qdrant
    (RAG_BACKEND=qdrant). SQLite остаётся источником правды, Qdrant — пересобираемый
    индекс. Переключение — env + scripts/18-vector-store-qdrant.sh, код приложения не меняется.

Слушает 127.0.0.1:8502. Контракт v1 (/index /query /delete /docs /docs/{name}/chunks
/health) сохранён полностью — оркестратор и Open WebUI Tool править не нужно.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from collections import Counter

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# --------------------------------------------------------------------------- конфиг
DATA_DIR = os.environ.get("RAG_SVC_DIR", "/srv/agents/rag-svc")
DB = os.environ.get("RAG_SVC_DB", os.path.join(DATA_DIR, "index.db"))
VEC_FILE = os.path.join(os.path.dirname(DB) or ".", "vectors.f32")
GW = os.environ.get("GATEWAY_URL", "http://127.0.0.1:4000/v1")
KEY = os.environ.get("ORCH_LLM_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
CHUNK = int(os.environ.get("RAG_CHUNK", 900))
OVERLAP = int(os.environ.get("RAG_OVERLAP", 150))
EMBED_BATCH = int(os.environ.get("RAG_EMBED_BATCH", 32))
BACKEND = os.environ.get("RAG_BACKEND", "sqlite").lower()       # sqlite | qdrant
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "rag_chunks")
QDRANT_KEY = os.environ.get("QDRANT_API_KEY", "")
DEFAULT_MODE = os.environ.get("RAG_QUERY_MODE", "hybrid")        # hybrid | dense | bm25
# Порог релевантности. Для медицинской базы «нашлось что-то далёкое» хуже, чем
# «не нашлось»: агент начинает договаривать. Косинус bge-m3 на нерелевантной паре
# обычно 0.0-0.2, на релевантной 0.35+. Фильтруем только dense-оценку: хиты,
# пришедшие только из BM25 (точное совпадение термина), оставляем.
MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.15"))
RESIDENT_LIMIT_MB = int(os.environ.get("RAG_DENSE_RESIDENT_MB", 4096))
DIM = int(os.environ.get("RAG_DIM", 1024))                       # bge-m3 = 1024

# пороги для рекомендации в /stats (обоснованы замерами в RAG-SCALE-PLAN.md)
T_CHUNKS_QDRANT = int(os.environ.get("RAG_T_CHUNKS_QDRANT", 200000))
T_LATENCY_MS = float(os.environ.get("RAG_T_LATENCY_MS", 500.0))

FILTERABLE = ("specialty", "doctype", "current", "mkb10")
META_FIELDS = ("title", "specialty", "nosology", "mkb10", "doctype", "version",
               "valid_from", "superseded_by", "source_path", "sha256", "ocr", "current")

app = FastAPI(docs_url=None, redoc_url=None)
_lock = threading.RLock()


# --------------------------------------------------------------------------- эмбеддинги
def embed(texts):
    """bge-m3 через центральный шлюз. Батчами по EMBED_BATCH, с ретраем: массовый
    импорт КР — это десятки тысяч вызовов, один обрыв не должен валить документ."""
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        body = json.dumps({"model": EMBED_MODEL, "input": batch}).encode()
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    GW + "/embeddings", data=body,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + KEY})
                with urllib.request.urlopen(req, timeout=600) as r:
                    d = json.load(r)
                m = np.array([x["embedding"] for x in d["data"]], dtype=np.float32)
                out.append(m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9))
                last = None
                break
            except Exception as e:                                     # noqa: BLE001
                last = e
                time.sleep(2 ** attempt)
        if last:
            raise RuntimeError(f"embeddings failed for batch {i}..{i + len(batch)}: {last}")
    if not out:
        return np.zeros((0, DIM), dtype=np.float32)
    M = np.vstack(out)
    if M.shape[1] != DIM:
        raise RuntimeError(f"модель вернула dim={M.shape[1]}, ожидается RAG_DIM={DIM}")
    return M


def chunkify(t):
    """Разбивка по абзацам с перекрытием. Шаг = CHUNK-OVERLAP."""
    paras = re.split(r"\n\s*\n", t)
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > CHUNK and cur:
            out.append(cur)
            cur = cur[-OVERLAP:] + p
        else:
            cur += "\n\n" + p if cur else p
    if cur:
        out.append(cur)
    return out or [t[:CHUNK]]


_stop = set("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы "
            "по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг "
            "ли если уже или ни быть был него до вас нибудь опять уж вам сказал ведь там потом "
            "себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб "
            "без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой "
            "совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех "
            "никогда можно при наконец два об другой хоть после над больше тот через эти нас "
            "про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда "
            "лучше чуть том нельзя такой им более всегда конечно всю между это".split())


def terms(q, limit=8):
    """Значимые токены запроса для BM25. Русской стемминга в FTS5 нет, поэтому
    токены идут в запрос префиксом ('препарат*' -> препараты/препарата/препарату)."""
    ws = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9][a-zA-Zа-яА-ЯёЁ0-9\-\.]{2,}", q.lower())
    out = []
    for w in ws:
        if w in _stop or len(w) < 4:
            continue
        if w not in out:
            out.append(w)
    return out[:limit]


def fts_match(ws):
    """FTS5-запрос: OR по префиксам, кавычки экранируем."""
    parts = []
    for w in ws:
        w = w.replace('"', "")
        if w:
            parts.append(f'"{w}"*')
    return " OR ".join(parts)


# --------------------------------------------------------------------------- бэкенд: sqlite
class SqliteIndex:
    """Плотные векторы — одним непрерывным файлом vectors.f32, текст и метаданные — в
    SQLite, BM25 — FTS5. Слоты векторов переиспользуются после удаления, поэтому матрица
    не пересобирается на запись (это и есть главное отличие от v1, см. RAG-SCALE-PLAN.md).

    Два представления матрицы:
      _Mw — memmap на запись (mode=r+), единственный источник правды по векторам;
      _Mr — то, по чему считается matmul: либо тот же memmap, либо резидентная копия
            в RAM (если матрица влезает в RAG_DENSE_RESIDENT_MB). Копия обновляется
            инкрементально при каждой записи, поэтому не разъезжается с файлом.
    """
    name = "sqlite"

    def __init__(self, db_path=DB, vec_path=VEC_FILE, dim=DIM, resident_mb=None):
        self.db_path, self.vec_path, self.dim = db_path, vec_path, dim
        self.resident_mb = RESIDENT_LIMIT_MB if resident_mb is None else resident_mb
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._c = sqlite3.connect(db_path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._ensure_schema()
        self._dirty_cols = True
        self.lat = []
        self._Mw = None
        self._Mr = None
        self._live = None
        self._slot2row = None
        self._cols = {}
        self.scores = []          # top-1 косинус последних запросов: для калибровки RAG_MIN_SCORE
        self._reload_vectors()
        self._reload_cols()

    # --- схема -------------------------------------------------------------
    def _ensure_schema(self):
        c = self._c
        # проверку v1 делаем ПЕРЕД любым DDL: иначе create index на несуществующей
        # колонке slot упадёт с непонятной ошибкой вместо внятного диагноза
        t = c.execute("select name from sqlite_master where type='table' and name='chunks'").fetchone()
        if t:
            cols = [r["name"] for r in c.execute("pragma table_info(chunks)")]
            if "emb" in cols:
                raise RuntimeError(
                    "найдена схема v1 (колонка emb). Выполните миграцию: "
                    "sudo bash scripts/17-rag-v2-migrate.sh (исходники в kb-inbox целы, "
                    "пере-эмбеддинг не нужен — векторы переносятся как есть)")
        c.execute("""create table if not exists chunks(
                        rowid_ integer primary key autoincrement,
                        doc text, idx int, slot int, text text, hash text,
                        title text, specialty text, nosology text, mkb10 text,
                        doctype text, version text, valid_from text, superseded_by text,
                        source_path text, sha256 text, ocr int default 0,
                        current int default 1, chars int default 0,
                        ingested_at text default (datetime('now')))""")
        c.execute("create index if not exists ix_chunks_doc on chunks(doc)")
        c.execute("create index if not exists ix_chunks_slot on chunks(slot)")
        for col in ("specialty", "doctype", "current", "mkb10"):
            c.execute(f"create index if not exists ix_chunks_{col} on chunks({col})")
        c.execute("""create virtual table if not exists chunks_fts
                     using fts5(text, content='', tokenize='unicode61 remove_diacritics 2')""")
        c.execute("create table if not exists meta(k text primary key, v text)")
        c.execute("create table if not exists free_slots(slot integer primary key)")
        c.commit()

    def _meta_get(self, k, default=None):
        r = self._c.execute("select v from meta where k=?", (k,)).fetchone()
        return r["v"] if r else default

    def _meta_set(self, k, v):
        self._c.execute("insert or replace into meta(k, v) values(?,?)", (k, str(v)))
        self._c.commit()

    def _capacity(self):
        return int(self._meta_get("capacity", "0") or 0)

    # --- векторный файл ----------------------------------------------------
    def _open_write(self):
        self._Mw = np.memmap(self.vec_path, dtype=np.float32, mode="r+",
                             shape=(self._capacity(), self.dim))
        mb = self._capacity() * self.dim * 4 / 1e6
        self.resident = mb <= self.resident_mb
        self._Mr = np.array(self._Mw) if self.resident else self._Mw

    def _reload_vectors(self):
        cap = self._capacity()
        if cap < 1024 or not os.path.exists(self.vec_path):
            self._ensure_file(1024)
        self._open_write()
        n = self._capacity()
        self._live = np.zeros(n, dtype=bool)
        self._slot2row = np.full(n, -1, dtype=np.int64)
        for r in self._c.execute("select rowid_, slot from chunks"):
            if 0 <= r["slot"] < n:
                self._live[r["slot"]] = True
                self._slot2row[r["slot"]] = r["rowid_"]

    def _ensure_file(self, need):
        """Растим файл с запасом 25% (округление до 1024 слотов), чтобы не дёргать
        truncate на каждом документе."""
        have = self._capacity()
        if have >= need and os.path.exists(self.vec_path):
            return False
        cap = max(need, int(have * 1.25), 1024)
        cap = ((cap + 255) // 256) * 256        # запас 25%, но без грубого округления до 1024
        existed = os.path.exists(self.vec_path)
        with open(self.vec_path, "ab" if existed else "wb") as f:
            f.truncate(cap * self.dim * 4)
        self._meta_set("capacity", cap)
        self._meta_set("dim", self.dim)
        return True

    def _grow(self, need):
        """Обеспечить место ещё под `need` слотов (не абсолютная позиция!) и
        пересоздать оба представления матрицы, сохранив live/slot2row."""
        cap = self._capacity()
        used = self._max_slot() + 1
        if cap - used >= need and self._Mw is not None and self._Mw.shape[0] == cap:
            return False
        self._ensure_file(used + need)
        newcap = self._capacity()
        if self._Mw is None or self._Mw.shape[0] != newcap:
            del self._Mw
            self._Mw = np.memmap(self.vec_path, dtype=np.float32, mode="r+",
                                 shape=(newcap, self.dim))
        mb = newcap * self.dim * 4 / 1e6
        self.resident = mb <= self.resident_mb
        old_r = self._Mr
        if self.resident:
            m = np.zeros((newcap, self.dim), dtype=np.float32)
            if old_r is not None:
                k = min(old_r.shape[0], newcap)
                m[:k] = old_r[:k]
            else:
                m[:] = self._Mw[:]
            self._Mr = m
        else:
            self._Mr = self._Mw
        live = np.zeros(newcap, dtype=bool)
        s2r = np.full(newcap, -1, dtype=np.int64)
        if self._live is not None:
            k = min(len(self._live), newcap)
            live[:k] = self._live[:k]
            s2r[:k] = self._slot2row[:k]
        self._live, self._slot2row = live, s2r
        return True

    def _max_slot(self):
        r = self._c.execute("select coalesce(max(slot), -1) m from chunks").fetchone()
        return int(r["m"])

    def _alloc_slots(self, n):
        """Слоты под n векторов: сначала из free_slots, затем новые подряд.
        Важно: выделяем СРАЗУ все n, потому что строк в chunks ещё нет и
        max(slot) на время цикла не меняется."""
        free = [int(r["slot"]) for r in self._c.execute(
            "select slot from free_slots order by slot limit ?", (n,))]
        if free:
            self._c.executemany("delete from free_slots where slot=?", [(s,) for s in free])
        need_new = n - len(free)
        if need_new > 0:
            # важно: _grow ждёт ДОПОЛНИТЕЛЬНОЕ число слотов. Раньше сюда передавали
            # base+need_new (абсолютную позицию) — файл раздувался в 2.4 раза.
            self._grow(need_new)
            base = self._max_slot() + 1
            if self._capacity() < base + need_new:      # страховка после роста
                self._ensure_file(base + need_new)
                del self._Mw
                self._Mw = np.memmap(self.vec_path, dtype=np.float32, mode="r+",
                                     shape=(self._capacity(), self.dim))
                if not self.resident:
                    self._Mr = self._Mw
            free.extend(range(base, base + need_new))
        return free

    # --- кэш колонок для фильтров -----------------------------------------
    def _reload_cols(self):
        cap = max(len(self._live) if self._live is not None else 0, 1)
        self._cols = {}
        for col in FILTERABLE:
            codes, lookup = [""], {"": 0}
            arr = np.zeros(cap, dtype=np.int32)
            for r in self._c.execute(
                    f"select slot, {col} v from chunks where {col} is not null and {col}<>''"):
                v = str(r["v"])
                if v not in lookup:
                    lookup[v] = len(codes)
                    codes.append(v)
                if r["slot"] < cap:
                    arr[r["slot"]] = lookup[v]
            self._cols[col] = (lookup, arr)
        self._dirty_cols = False

    def _mask(self, filters):
        cap = len(self._live)
        m = self._live.copy()
        for k, v in (filters or {}).items():
            if k not in FILTERABLE or v in (None, ""):
                continue
            lookup, arr = self._cols[k]
            code = lookup.get(str(v))
            if code is None:
                return np.zeros(cap, dtype=bool)
            m &= (arr == code)
        return m

    # --- запись ------------------------------------------------------------
    def upsert_doc(self, name, texts, embs, meta, doc_hash):
        with _lock:
            texts = list(texts)
            n = len(texts)
            if n == 0:
                self.delete_doc(name)
                return 0, None
            self._grow(n)
            self.delete_doc(name, _commit=False)
            dup = self._c.execute("select doc from chunks where hash=? and doc<>? limit 1",
                                  (doc_hash, name)).fetchone()
            if dup:
                self.delete_doc(dup["doc"], _commit=False)
            slots = self._alloc_slots(n)
            mv = {k: meta.get(k) for k in META_FIELDS}
            rows = []
            for i, (t, e) in enumerate(zip(texts, embs)):
                slot = slots[i]
                self._Mw[slot] = e
                if self.resident and self._Mr is not self._Mw:
                    self._Mr[slot] = e
                rows.append((name, i, slot, t, doc_hash,
                             mv["title"] or name, mv["specialty"], mv["nosology"],
                             mv["mkb10"], mv["doctype"], mv["version"], mv["valid_from"],
                             mv["superseded_by"], mv["source_path"], mv["sha256"],
                             int(mv["ocr"] or 0),
                             1 if mv["current"] in (None, "", 1, True, "1") else 0,
                             len(t)))
            self._c.executemany(
                """insert into chunks(doc, idx, slot, text, hash, title, specialty, nosology,
                                      mkb10, doctype, version, valid_from, superseded_by,
                                      source_path, sha256, ocr, current, chars)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            got = self._c.execute("select rowid_, slot from chunks where doc=? order by idx",
                                  (name,)).fetchall()
            self._c.executemany("insert into chunks_fts(rowid, text) values(?,?)",
                                [(r["rowid_"], t) for r, t in zip(got, texts)])
            self._Mw.flush()
            for r in got:
                self._live[r["slot"]] = True
                self._slot2row[r["slot"]] = r["rowid_"]
            self._c.commit()
            self._dirty_cols = True
            return len(got), (dup["doc"] if dup else None)

    def _fts_delete(self, rows):
        """FTS5 создана с content='' (без копии текста), поэтому обычный DELETE не
        работает: нужен служебный 'delete'-комманд с исходным текстом строки."""
        self._c.executemany(
            "insert into chunks_fts(chunks_fts, rowid, text) values('delete', ?, ?)",
            [(r["rowid_"], r["text"]) for r in rows])

    def delete_doc(self, name, _commit=True):
        with _lock:
            rows = self._c.execute("select rowid_, slot, text from chunks where doc=?",
                                   (name,)).fetchall()
            if not rows:
                if _commit:
                    self._c.commit()
                return 0
            self._c.executemany("insert or ignore into free_slots(slot) values(?)",
                                [(r["slot"],) for r in rows])
            for r in rows:
                s = r["slot"]
                if 0 <= s < len(self._live):
                    self._live[s] = False
                    self._slot2row[s] = -1
                    self._Mw[s] = 0.0
                    if self.resident and self._Mr is not self._Mw:
                        self._Mr[s] = 0.0
            self._fts_delete(rows)
            self._c.execute("delete from chunks where doc=?", (name,))
            if _commit:
                self._Mw.flush()
                self._c.commit()
                self._dirty_cols = True
            return len(rows)

    def set_current(self, name, current, superseded_by=None):
        """Пометить редакцию действующей/устаревшей. Устаревшие не удаляются: они
        нужны для аудита, но в поиск по умолчанию (current_only=1) не попадают."""
        with _lock:
            self._c.execute("update chunks set current=?, superseded_by=coalesce(?, superseded_by) "
                            "where doc=?", (int(bool(current)), superseded_by, name))
            self._c.commit()
            self._dirty_cols = True
            return self._c.execute("select count(*) from chunks where doc=?", (name,)).fetchone()[0]

    # --- поиск -------------------------------------------------------------
    def dense_scores(self, q):
        if self._Mr.shape[0] != len(self._live):
            self._reload_vectors()
        sim = self._Mr @ q
        if not self._live.all():
            sim = np.where(self._live, sim, -2.0)
        return sim

    def search(self, q, k=6, filters=None, mode=None, bm25_pool=500, raw=""):
        """q — вектор запроса (float32, dim), raw — исходный текст запроса для BM25."""
        t0 = time.perf_counter()
        mode = (mode or DEFAULT_MODE).lower()
        if self._dirty_cols:
            self._reload_cols()
        mask = self._mask(filters)
        n_live = int(mask.sum())
        if n_live == 0:
            self._lat(time.perf_counter() - t0)
            return [], {"mode": mode, "candidates": 0}
        dense_rank, bm25_rank = {}, {}
        if mode in ("hybrid", "dense"):
            sim = np.where(mask, self.dense_scores(q), -2.0)
            kk = min(bm25_pool if mode == "hybrid" else k, n_live)
            top = np.argpartition(sim, -kk)[-kk:]
            top = top[np.argsort(sim[top])[::-1]]
            for pos, slot in enumerate(top):
                rid = self._slot2row[slot]
                if rid >= 0:
                    dense_rank[int(rid)] = (pos, float(sim[slot]))
        if mode in ("hybrid", "bm25"):
            ws = terms(raw)
            if ws:
                sql = ("select f.rowid rid from chunks_fts f join chunks c on c.rowid_=f.rowid "
                       "where chunks_fts match ? ")
                args = [fts_match(ws)]
                for col, val in (filters or {}).items():
                    if col in FILTERABLE and val not in (None, ""):
                        sql += f" and c.{col}=?"
                        args.append(int(val) if col == "current" else str(val))
                sql += " order by rank limit ?"
                args.append(bm25_pool)
                try:
                    for pos, r in enumerate(self._c.execute(sql, args)):
                        bm25_rank[int(r["rid"])] = pos
                except sqlite3.OperationalError:
                    bm25_rank = {}
        if mode == "dense":
            fused = sorted(dense_rank.items(), key=lambda kv: kv[1][0])
        elif mode == "bm25":
            fused = sorted(bm25_rank.items(), key=lambda kv: kv[1])
        else:
            score = Counter()
            for rid, (pos, _) in dense_rank.items():
                score[rid] += 1.0 / (60 + pos)
            for rid, pos in bm25_rank.items():
                score[rid] += 1.0 / (60 + pos)
            fused = score.most_common(k * 3)
        ids = [rid for rid, _ in fused[:k]]
        rrf = dict(fused)
        out = []
        if ids:
            ph = ",".join("?" * len(ids))
            rows = {r["rowid_"]: r for r in self._c.execute(
                f"select * from chunks where rowid_ in ({ph})", ids)}
            for rid in ids:
                r = rows.get(rid)
                if not r:
                    continue
                sc = dense_rank.get(rid, (None, None))[1]
                out.append({
                    "doc": r["doc"], "idx": r["idx"],
                    "score": round(sc, 4) if sc is not None else None,
                    "rrf": round(rrf.get(rid, 0.0), 5) if mode == "hybrid" else None,
                    "bm25_rank": bm25_rank.get(rid),
                    "text": r["text"][:1600],
                    "title": r["title"], "specialty": r["specialty"], "mkb10": r["mkb10"],
                    "doctype": r["doctype"], "version": r["version"],
                    "current": bool(r["current"]), "chars": r["chars"],
                    "source_path": r["source_path"],
                })
        self._lat(time.perf_counter() - t0)
        sc = [x["score"] for x in out if x.get("score") is not None]
        if sc:
            self.scores.append(max(sc))
            if len(self.scores) > 500:
                del self.scores[:len(self.scores) - 500]
        return out, {"mode": mode, "candidates": n_live,
                     "dense_pool": len(dense_rank), "bm25_pool": len(bm25_rank)}

    def _lat(self, dt):
        self.lat.append(dt * 1000)
        if len(self.lat) > 500:
            del self.lat[:len(self.lat) - 500]

    # --- справочное --------------------------------------------------------
    def list_docs(self):
        rows = self._c.execute(
            """select doc, count(*) n, sum(chars) chars, max(ingested_at) at,
                      max(title) title, max(specialty) specialty, max(doctype) doctype,
                      max(version) version, min(current) cur
               from chunks group by doc order by doc""").fetchall()
        return [{"name": r["doc"], "chunks": r["n"], "chars": r["chars"] or 0,
                 "title": r["title"], "specialty": r["specialty"], "doctype": r["doctype"],
                 "version": r["version"], "current": bool(r["cur"]),
                 "ingested_at": r["at"]} for r in rows]

    def doc_chunks(self, name, k=3):
        rows = self._c.execute("select idx, text from chunks where doc=? order by idx limit ?",
                               (name, k)).fetchall()
        return [{"idx": r["idx"], "text": r["text"][:400]} for r in rows]

    def doc_texts(self, name):
        rows = self._c.execute(
            "select idx, text, title, specialty, nosology, mkb10, doctype, version, valid_from, "
            "source_path, sha256, ocr, current from chunks where doc=? order by idx",
            (name,)).fetchall()
        if not rows:
            return None, None
        meta = {k: rows[0][k] for k in ("title", "specialty", "nosology", "mkb10", "doctype",
                                        "version", "valid_from", "source_path", "sha256",
                                        "ocr", "current")}
        return [r["text"] for r in rows], meta

    def doc_vectors(self, name):
        """Текст + метаданные + готовые векторы документа. Нужно, чтобы наполнить
        Qdrant из уже посчитанных эмбеддингов и не жечь часы GPU на пере-эмбеддинг."""
        rows = self._c.execute(
            "select rowid_, idx, slot, text, title, specialty, nosology, mkb10, doctype, "
            "version, valid_from, superseded_by, source_path, sha256, ocr, current "
            "from chunks where doc=? order by idx", (name,)).fetchall()
        if not rows:
            return None, None, None
        meta = {k: rows[0][k] for k in ("title", "specialty", "nosology", "mkb10", "doctype",
                                        "version", "valid_from", "superseded_by", "source_path",
                                        "sha256", "ocr", "current")}
        texts = [r["text"] for r in rows]
        embs = np.vstack([np.asarray(self._Mw[r["slot"]], dtype=np.float32) for r in rows])
        return texts, meta, embs

    def count(self):
        return int(self._c.execute("select count(*) from chunks").fetchone()[0])

    def stats(self):
        cap = self._capacity()
        n = self.count()
        by = {}
        for col in ("specialty", "doctype"):
            by[col] = {str(r[0] or "—"): r[1] for r in self._c.execute(
                f"select {col}, count(*) from chunks group by {col} order by count(*) desc limit 12")}
        lat = sorted(self.lat) if self.lat else []
        return {
            "backend": self.name, "chunks": n,
            "docs": int(self._c.execute("select count(distinct doc) from chunks").fetchone()[0]),
            "dim": self.dim, "capacity_slots": cap,
            "fragmentation": round((cap - n) / cap, 3) if cap else 0.0,
            "dense_resident": self.resident,
            "db_bytes": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
            "vectors_bytes": os.path.getsize(self.vec_path) if os.path.exists(self.vec_path) else 0,
            # файл векторов разреженный (truncate): apparent != занято на диске
            "vectors_allocated_bytes": _allocated(self.vec_path),
            "dense_matrix_mb": round(cap * self.dim * 4 / 1e6, 1),
            "superseded_docs": int(self._c.execute(
                "select count(distinct doc) from chunks where current=0").fetchone()[0]),
            "min_score": MIN_SCORE, "query_mode": DEFAULT_MODE,
            "query_latency_ms": {"n": len(lat),
                                 "p50": round(lat[len(lat) // 2], 1) if lat else None,
                                 "p95": round(lat[min(int(len(lat) * .95), len(lat) - 1)], 1) if lat else None,
                                 "max": round(lat[-1], 1) if lat else None},
            "top1_score": _pct(self.scores),
            "by": by,
            "oldest": self._c.execute("select min(ingested_at) from chunks").fetchone()[0],
            "newest": self._c.execute("select max(ingested_at) from chunks").fetchone()[0],
        }

    def compact(self):
        """Убирает дыры от удалений: пересобирает векторный файл плотнее."""
        with _lock:
            rows = self._c.execute("select rowid_, slot from chunks order by slot").fetchall()
            n = len(rows)
            cap = max(((n + 255) // 256) * 256, 1024)
            tmp = self.vec_path + ".new"
            with open(tmp, "wb") as f:
                f.truncate(cap * self.dim * 4)
            new = np.memmap(tmp, dtype=np.float32, mode="r+", shape=(cap, self.dim))
            old = self._Mw
            for newslot, r in enumerate(rows):
                if 0 <= r["slot"] < old.shape[0]:
                    new[newslot] = old[r["slot"]]
            new.flush()
            del new
            os.replace(tmp, self.vec_path)
            self._c.execute("delete from free_slots")
            for newslot, r in enumerate(rows):
                self._c.execute("update chunks set slot=? where rowid_=?", (newslot, r["rowid_"]))
            self._meta_set("capacity", cap)
            self._c.commit()
            self._reload_vectors()
            self._dirty_cols = True
            return {"chunks": n, "capacity": cap}


# --------------------------------------------------------------------------- бэкенд: qdrant
class QdrantIndex:
    """Тот же интерфейс поверх Qdrant. Векторы и payload живут в Qdrant, SQLite
    остаётся источником правды по тексту (чтобы индекс можно было пересобрать с нуля).
    Полнотекстовый фильтр — text-index с tokenizer=multilingual (русский стемминг),
    но это ФИЛЬТР, а не BM25-ранжирование: настоящий гибрид в Qdrant требует sparse-векторов."""
    name = "qdrant"

    def __init__(self, url=QDRANT_URL, collection=QDRANT_COLLECTION, dim=DIM,
                 sqlite_mirror=None):
        from qdrant_client import QdrantClient
        from qdrant_client import models as qm
        self.qm = qm
        self.dim, self.collection = dim, collection
        if url == ":memory:" or url.startswith("/"):
            # локальный режим qdrant-client: отладка/тесты/миграция без контейнера
            self.client = QdrantClient(path=url, timeout=60)
        else:
            self.client = QdrantClient(url=url, api_key=(QDRANT_KEY or None), timeout=60)
        # зеркало — источник правды по тексту/метаданным и поставщик BM25.
        # Резидентная копия матрицы ему не нужна (векторы всё равно в Qdrant).
        self.mirror = sqlite_mirror
        self.lat = []
        self.scores = []
        self._ensure_collection()

    def _ensure_collection(self):
        qm = self.qm
        names = [c.name for c in self.client.get_collections().collections]
        if self.collection not in names:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE),
                hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=200))
        import warnings
        with warnings.catch_warnings():
            # в локальном режиме qdrant-client payload-индексы не поддерживает (это ок:
            # локальный режим нужен для тестов/миграции, в бою — контейнер)
            warnings.simplefilter("ignore")
            for f in ("specialty", "doctype", "mkb10", "doc"):
                try:
                    self.client.create_payload_index(self.collection, f,
                                                     qm.PayloadSchemaType.KEYWORD)
                except Exception:                                        # noqa: BLE001
                    pass
        try:
            self.client.create_payload_index(self.collection, "current", qm.PayloadSchemaType.INTEGER)
        except Exception:                                                # noqa: BLE001
            pass
        try:
            self.client.create_payload_index(
                self.collection, "text", qm.TextIndexParams(
                    type="text", tokenizer=qm.TokenizerType.MULTILINGUAL,
                    min_token_len=3, lowercase=True))
        except Exception:                                                # noqa: BLE001
            pass

    def _qfilter(self, filters, match_texts=None):
        qm = self.qm
        must = []
        for k, v in (filters or {}).items():
            if v in (None, ""):
                continue
            if k == "current":
                must.append(qm.FieldCondition(key="current", match=qm.MatchValue(value=int(v))))
            else:
                must.append(qm.FieldCondition(key=k, match=qm.MatchValue(value=str(v))))
        if match_texts:
            must.append(qm.FieldCondition(key="text", match=qm.MatchText(text=match_texts)))
        return qm.Filter(must=must) if must else None

    def upsert_doc(self, name, texts, embs, meta, doc_hash):
        qm = self.qm
        # Порядок важен: delete_doc чистит ОБА хранилища (Qdrant + зеркало), поэтому
        # сначала удаляем старое, и только потом пишем новое.
        self.delete_doc(name)
        if self.mirror is not None:
            # SQLite остаётся источником правды: без этой записи Qdrant нельзя
            # пересобрать (/reindex) и нельзя откатиться на sqlite-бэкенд
            self.mirror.upsert_doc(name, texts, embs, meta, doc_hash)
        pts = []
        for i, (t, e) in enumerate(zip(texts, embs)):
            pid = int(hashlib.sha1(f"{name}\x00{i}".encode()).hexdigest()[:15], 16)
            payload = {"doc": name, "idx": i, "text": t, "hash": doc_hash,
                       "title": meta.get("title") or name, "chars": len(t),
                       "current": int(meta.get("current", 1))}
            for f in ("specialty", "nosology", "mkb10", "doctype", "version",
                      "valid_from", "source_path", "sha256"):
                if meta.get(f) is not None:
                    payload[f] = str(meta[f])
            pts.append(qm.PointStruct(id=pid, vector=e.tolist(), payload=payload))
        for i in range(0, len(pts), 256):
            self.client.upsert(self.collection, points=pts[i:i + 256], wait=True)
        return len(pts), None

    def delete_doc(self, name, _commit=True):
        qm = self.qm
        if self.mirror is not None:
            self.mirror.delete_doc(name, _commit=_commit)
        n = self.count_doc(name)
        self.client.delete(self.collection, points_selector=qm.FilterSelector(
            filter=qm.Filter(must=[qm.FieldCondition(
                key="doc", match=qm.MatchValue(value=name))])), wait=True)
        return n

    def count_doc(self, name):
        try:
            return self.client.count(self.collection, count_filter=self._qfilter({"doc": name}),
                                     exact=True).count
        except Exception:                                                # noqa: BLE001
            return 0

    def search(self, q, k=6, filters=None, mode=None, bm25_pool=500, raw=""):
        """Dense ANN в Qdrant (+ payload-фильтр), BM25 — в SQLite-зеркале, слияние RRF.
        Настоящего BM25-ранжирования внутри Qdrant нет: text-index даёт булев фильтр,
        а sparse-векторы bge-m3 через Ollama недоступны (см. RAG-SCALE-PLAN.md §6)."""
        t0 = time.perf_counter()
        mode = (mode or DEFAULT_MODE).lower()
        mfilters = {kk: (int(vv) if kk == "current" else str(vv))
                    for kk, vv in (filters or {}).items() if vv not in (None, "")}
        bm25_rank = {}
        if mode in ("hybrid", "bm25") and self.mirror is not None:
            mres, _ = self.mirror.search(q, k=bm25_pool, filters=mfilters, mode="bm25",
                                         bm25_pool=bm25_pool, raw=raw)
            for pos, r in enumerate(mres):
                bm25_rank[(r["doc"], r["idx"])] = pos
        if mode == "bm25":
            out = []
            keys = sorted(bm25_rank.items(), key=lambda kv: kv[1])[:k]
            for (doc, i), pos in keys:
                texts, _ = self.mirror.doc_texts(doc)
                out.append({"doc": doc, "idx": i, "score": None, "rrf": None,
                            "bm25_rank": pos, "text": (texts[i][:1600] if texts and i < len(texts) else ""),
                            "title": None, "specialty": None, "mkb10": None, "doctype": None,
                            "version": None, "current": True, "chars": None, "source_path": None})
            self.lat.append((time.perf_counter() - t0) * 1000)
            return out, {"mode": mode, "candidates": len(bm25_rank), "bm25_pool": len(bm25_rank)}
        limit = bm25_pool if mode == "hybrid" else k
        hits = self.client.query_points(
            self.collection, query=q.tolist(), limit=min(limit, max(k, 1)),
            query_filter=self._qfilter(mfilters), with_payload=True).points
        dense_rank = {}
        for pos, h in enumerate(hits):
            p = h.payload or {}
            dense_rank[(p.get("doc"), p.get("idx"))] = (pos, float(h.score), p)
        if mode == "dense":
            fused = sorted(dense_rank.items(), key=lambda kv: kv[1][0])
        else:
            score = Counter()
            for key, (pos, _, _) in dense_rank.items():
                score[key] += 1.0 / (60 + pos)
            for key, pos in bm25_rank.items():
                score[key] += 1.0 / (60 + pos)
            fused = score.most_common(k * 3)
        rrf = dict(fused)
        out = []
        for key, _ in fused[:k]:
            ent = dense_rank.get(key)
            if ent:
                pos, sc, p = ent
            else:
                pos, sc, p = None, None, None
            if p is None and self.mirror is not None:
                # чанк пришёл только из BM25 — текст берём из зеркала
                texts, meta = self.mirror.doc_texts(key[0])
                p = {"doc": key[0], "idx": key[1],
                     "text": texts[key[1]] if texts and key[1] < len(texts) else "",
                     **(meta or {})}
            if p is None:
                continue
            out.append({"doc": p.get("doc"), "idx": p.get("idx"),
                        "score": round(sc, 4) if sc is not None else None,
                        "rrf": round(rrf.get(key, 0.0), 5) if mode == "hybrid" else None,
                        "bm25_rank": bm25_rank.get(key),
                        "text": (p.get("text") or "")[:1600],
                        "title": p.get("title"), "specialty": p.get("specialty"),
                        "mkb10": p.get("mkb10"), "doctype": p.get("doctype"),
                        "version": p.get("version"), "current": bool(p.get("current", 1)),
                        "chars": p.get("chars"), "source_path": p.get("source_path")})
        self.lat.append((time.perf_counter() - t0) * 1000)
        if len(self.lat) > 500:
            del self.lat[:len(self.lat) - 500]
        return out, {"mode": mode, "candidates": len(hits),
                     "dense_pool": len(dense_rank), "bm25_pool": len(bm25_rank)}

    def sync_from_mirror(self, docs=None, log_every=20):
        """Наполнить Qdrant векторами, которые уже посчитаны и лежат в SQLite-зеркале.
        Пере-эмбеддинг НЕ выполняется — это минуты, а не часы."""
        if self.mirror is None:
            raise RuntimeError("нет SQLite-зеркала: sync_from_mirror невозможен")
        names = docs or [d["name"] for d in self.mirror.list_docs()]
        qm = self.qm
        moved = failed = 0
        for n, nm in enumerate(names, 1):
            try:
                texts, meta, embs = self.mirror.doc_vectors(nm)
                if not texts:
                    continue
                self.delete_doc(nm)
                pts = []
                for i, (t, e) in enumerate(zip(texts, embs)):
                    pid = int(hashlib.sha1(f"{nm}\x00{i}".encode()).hexdigest()[:15], 16)
                    payload = {"doc": nm, "idx": i, "text": t, "hash": meta.get("sha256"),
                               "title": meta.get("title") or nm, "chars": len(t),
                               "current": int(meta.get("current") or 0)}
                    for f in ("specialty", "nosology", "mkb10", "doctype", "version",
                              "valid_from", "source_path", "sha256"):
                        if meta.get(f) is not None:
                            payload[f] = str(meta[f])
                    pts.append(qm.PointStruct(id=pid, vector=e.tolist(), payload=payload))
                for i in range(0, len(pts), 256):
                    self.client.upsert(self.collection, points=pts[i:i + 256], wait=True)
                moved += len(pts)
            except Exception:                                            # noqa: BLE001
                failed += 1
        return {"docs": len(names), "points": moved, "failed_docs": failed}

    def list_docs(self):
        if self.mirror is not None:
            return self.mirror.list_docs()
        agg = {}
        off = None
        while True:
            pts, off = self.client.scroll(self.collection, limit=1000, offset=off,
                                          with_payload=["doc", "idx", "title", "specialty",
                                                        "doctype", "version", "current", "chars"])
            for p in pts:
                pl = p.payload or {}
                d = agg.setdefault(pl.get("doc"), {"name": pl.get("doc"), "chunks": 0, "chars": 0,
                                                   "title": pl.get("title"),
                                                   "specialty": pl.get("specialty"),
                                                   "doctype": pl.get("doctype"),
                                                   "version": pl.get("version"),
                                                   "current": bool(pl.get("current", 1))})
                d["chunks"] += 1
                d["chars"] += int(pl.get("chars") or 0)
            if off is None:
                break
        return sorted(agg.values(), key=lambda x: str(x["name"]))

    def doc_chunks(self, name, k=3):
        if self.mirror is not None:
            return self.mirror.doc_chunks(name, k)
        pts, _ = self.client.scroll(self.collection, limit=k, with_payload=["idx", "text"],
                                    scroll_filter=self._qfilter({"doc": name}))
        return [{"idx": (p.payload or {}).get("idx"),
                 "text": ((p.payload or {}).get("text") or "")[:400]} for p in pts]

    def count(self):
        try:
            return self.client.count(self.collection, exact=True).count
        except Exception:                                                # noqa: BLE001
            return 0

    def stats(self):
        """Контракт /stats одинаков для обоих бэкендов — иначе дашборд и дайджест
        пришлось бы ветвить."""
        lat = sorted(self.lat) if self.lat else []
        info = {}
        try:
            info = self.client.get_collection(self.collection).__dict__
        except Exception:                                                # noqa: BLE001
            pass
        mirror = self.mirror
        docs = None
        superseded = None
        if mirror is not None:
            docs = len(mirror.list_docs())
            try:
                superseded = int(mirror._c.execute(                                # noqa: SLF001
                    "select count(distinct doc) from chunks where current=0").fetchone()[0])
            except Exception:                                            # noqa: BLE001
                superseded = None
        return {"backend": self.name, "chunks": self.count(), "docs": docs,
                "dim": self.dim, "collection": self.collection, "url": QDRANT_URL,
                "dense_resident": None,
                "points_indexed": getattr(info, "points_count", None) or info.get("points_count"),
                "indexed_vectors": getattr(info, "indexed_vectors_count", None)
                or info.get("indexed_vectors_count"),
                "status": getattr(info, "status", None) or info.get("status"),
                "superseded_docs": superseded,
                "db_bytes": mirror.stats()["db_bytes"] if mirror is not None else None,
                "vectors_bytes": mirror.stats()["vectors_bytes"] if mirror is not None else None,
                "dense_matrix_mb": None, "capacity_slots": None, "fragmentation": None,
                "query_latency_ms": {"n": len(lat),
                                     "p50": round(lat[len(lat) // 2], 1) if lat else None,
                                     "p95": round(lat[min(int(len(lat) * .95), len(lat) - 1)], 1) if lat else None,
                                     "max": round(lat[-1], 1) if lat else None},
                "top1_score": _pct(self.scores),
                "by": mirror.stats()["by"] if mirror is not None else {},
                "note": "SQLite — источник правды по тексту; Qdrant пересобирается: "
                        "scripts/18-vector-store-qdrant.sh --sync"}

    def compact(self):
        return {"backend": self.name, "noop": "Qdrant уплотняет сегменты сам (optimizer)"}


# --------------------------------------------------------------------------- экземпляр
def build_backend():
    if BACKEND == "qdrant":
        mirror = SqliteIndex(resident_mb=0)     # RAM экономим: векторы читаются из Qdrant
        return QdrantIndex(sqlite_mirror=mirror)
    return SqliteIndex()


try:
    IDX = build_backend()
except Exception as _e:                                                  # noqa: BLE001
    # сервис должен стартовать и отдать диагноз в /health, а не падать в restart-loop
    IDX = None
    STARTUP_ERROR = f"{type(_e).__name__}: {_e}"
else:
    STARTUP_ERROR = None


def idx():
    if IDX is None:
        raise HTTPException(503, f"хранилище не инициализировано: {STARTUP_ERROR}")
    return IDX


# --------------------------------------------------------------------------- API
class IndexReq(BaseModel):
    name: str
    text: str
    meta: dict | None = None


class BulkReq(BaseModel):
    docs: list[dict]                                 # [{name, text, meta}]


class QueryReq(BaseModel):
    q: str
    k: int = 6
    mode: str | None = None                          # hybrid | dense | bm25
    filters: dict | None = None
    current_only: bool | None = None
    min_score: float | None = None                   # None = RAG_MIN_SCORE из env


class DelReq(BaseModel):
    name: str


class ReindexReq(BaseModel):
    docs: list[str] | None = None
    use_existing_vectors: bool = False   # true = без пере-эмбеддинга (только qdrant-бэкенд)


def _norm_filters(f, current_only):
    out = dict(f or {})
    if current_only is None:
        current_only = os.environ.get("RAG_CURRENT_ONLY", "1") == "1"
    if current_only:
        out.setdefault("current", 1)
    return {k: (int(v) if k == "current" else v) for k, v in out.items()}


@app.post("/index")
def index(r: IndexReq):
    texts = chunkify(r.text)
    embs = embed(texts)
    h = hashlib.sha256(re.sub(r"\s+", " ", r.text).strip().encode()).hexdigest()
    meta = dict(r.meta or {})
    meta.setdefault("sha256", h)
    meta.setdefault("chars", len(r.text))
    n, dup = idx().upsert_doc(r.name, texts, embs, meta, h)
    return {"doc": r.name, "chunks": n, "replaced_duplicate_of": dup}


@app.post("/index_bulk")
def index_bulk(r: BulkReq):
    """Массовый импорт: один вызов = много документов, каждый со своими метаданными."""
    done, failed = [], []
    for d in r.docs:
        try:
            texts = chunkify(d["text"])
            embs = embed(texts)
            h = hashlib.sha256(re.sub(r"\s+", " ", d["text"]).strip().encode()).hexdigest()
            meta = dict(d.get("meta") or {})
            meta.setdefault("sha256", h)
            n, dup = idx().upsert_doc(d["name"], texts, embs, meta, h)
            done.append({"doc": d["name"], "chunks": n, "replaced_duplicate_of": dup})
        except Exception as e:                                           # noqa: BLE001
            failed.append({"doc": d.get("name"), "error": f"{type(e).__name__}: {e}"})
    return {"indexed": len(done), "failed": len(failed), "results": done, "errors": failed}


@app.post("/query")
def query(r: QueryReq):
    q = embed([r.q])[0]
    filters = _norm_filters(r.filters, r.current_only)
    res, info = idx().search(q, k=r.k, filters=filters, mode=r.mode, raw=r.q)
    floor = MIN_SCORE if r.min_score is None else r.min_score
    dropped = 0
    if floor and floor > 0:
        keep = []
        for x in res:
            # score=None означает «пришёл только из BM25» — это точное лексическое
            # совпадение, его порогом косинуса отсекать нельзя
            if x.get("score") is not None and x["score"] < floor:
                dropped += 1
            else:
                keep.append(x)
        res = keep
    if not res:
        note = ("ничего не найдено" if not dropped else
                f"найдено {dropped} фрагмент(ов), но все ниже порога релевантности {floor}")
        return {"results": [], "note": note + " (индекс пуст, фильтр слишком узкий или "
                                             "документа в корпусе нет)",
                "search": info, "filters": filters, "min_score": floor, "dropped": dropped}
    return {"results": res, "search": info, "filters": filters,
            "min_score": floor, "dropped": dropped}


@app.post("/delete")
def delete(r: DelReq):
    n = idx().delete_doc(r.name)
    return {"deleted": r.name, "chunks_removed": n}


class SupersedeReq(BaseModel):
    name: str
    by: str | None = None
    current: bool = False


@app.post("/supersede")
def supersede(r: SupersedeReq):
    """Пометить редакцию устаревшей (current=0) — в поиск по умолчанию она больше не
    попадает, но остаётся в индексе для аудита. Обратное: current=true."""
    ix = idx()
    if isinstance(ix, QdrantIndex) and ix.mirror is not None:
        n = ix.mirror.set_current(r.name, r.current, r.by)
        try:
            ix.client.set_payload(ix.collection, payload={"current": int(r.current),
                                                          "superseded_by": r.by},
                                  points=ix._qfilter({"doc": r.name}))
        except Exception as e:                                           # noqa: BLE001
            return {"doc": r.name, "chunks": n, "warning": f"payload в Qdrant не обновлён: {e}"}
        return {"doc": r.name, "chunks": n, "current": r.current}
    n = ix.set_current(r.name, r.current, r.by)
    return {"doc": r.name, "chunks": n, "current": r.current}


@app.get("/docs")
def docs():
    d = idx().list_docs()
    return {"docs": d, "total_chunks": sum(x["chunks"] for x in d)}


@app.get("/docs/{name}/chunks")
def doc_chunks(name: str, k: int = 3):
    return {"doc": name, "preview": idx().doc_chunks(name, k)}


@app.get("/stats")
def stats():
    s = idx().stats()
    s["startup_error"] = STARTUP_ERROR
    s["thresholds"] = {"qdrant_chunks": T_CHUNKS_QDRANT, "latency_ms": T_LATENCY_MS,
                       "min_score": MIN_SCORE}
    s["recommendation"] = _recommend(s)
    return s


def _pct(vals):
    """Перцентили top-1 оценок. Нужны, чтобы калибровать RAG_MIN_SCORE по фактическому
    корпусу: базовая косинусная близость bge-m3 на нерелевантных парах заранее
    неизвестна и измеряется только на месте."""
    if not vals:
        return {"n": 0, "p10": None, "p50": None, "p90": None, "min": None, "max": None}
    v = sorted(vals)
    pick = lambda q: round(v[min(int(len(v) * q), len(v) - 1)], 4)   # noqa: E731
    return {"n": len(v), "p10": pick(.10), "p50": pick(.50), "p90": pick(.90),
            "min": round(v[0], 4), "max": round(v[-1], 4)}


def _allocated(path):
    """Реально занятые блоки: файл векторов создаётся через truncate и остаётся
    разреженным, поэтому st_size сильно больше фактического расхода диска."""
    try:
        st = os.stat(path)
        return st.st_blocks * 512
    except OSError:
        return 0


def _recommend(s):
    """Машина решает по замерам, а не по вкусу: что уже пора, а что ещё терпит."""
    n = s.get("chunks") or 0
    p95 = (s.get("query_latency_ms") or {}).get("p95")
    msgs = []
    if n >= T_CHUNKS_QDRANT:
        msgs.append(f"чанков {n} >= порога {T_CHUNKS_QDRANT}: включать Qdrant "
                    "(scripts/18-vector-store-qdrant.sh)")
    if p95 and p95 > T_LATENCY_MS:
        msgs.append(f"p95 латентность {p95} мс > {T_LATENCY_MS}: включать Qdrant")
    frag = s.get("fragmentation") or 0
    if frag > 0.3 and (s.get("chunks") or 0) > 1000:
        msgs.append(f"фрагментация {frag:.0%} (запас роста + удалённые документы): "
                    "выполнить POST /compact — вернёт место на диске")
    if s.get("backend") == "sqlite" and not s.get("dense_resident") and n:
        msgs.append("матрица не резидентна (memmap): поднять RAG_DENSE_RESIDENT_MB или перейти на Qdrant")
    t1 = s.get("top1_score") or {}
    if t1.get("n", 0) >= 20:
        # порог ниже p10 реальных запросов = отсекает почти всё; выше p50 = режет полезное
        msgs.append(f"калибровка RAG_MIN_SCORE: p10={t1['p10']} p50={t1['p50']} p90={t1['p90']} "
                    f"по {t1['n']} запросам — разумный порог между p10 и p50 "
                    f"(сейчас {MIN_SCORE})")
    elif n:
        msgs.append("RAG_MIN_SCORE не откалиброван: задайте 10-15 реальных вопросов и 5 "
                    "заведомо посторонних, затем смотрите /stats.top1_score")
    if not any("Qdrant" in m or "qdrant" in m for m in msgs):
        if s.get("backend") == "qdrant":
            msgs.append(f"qdrant-бэкенд в норме ({n} чанков); SQLite — источник правды, "
                        "откат: scripts/18-vector-store-qdrant.sh --rollback")
        else:
            msgs.append(f"sqlite-бэкенд в норме ({n} чанков); машинный порог перехода на Qdrant — "
                        f"{T_CHUNKS_QDRANT} чанков или p95 > {T_LATENCY_MS:.0f} мс; проектным "
                        "решением переход может быть назначен раньше (RAG-SCALE-PLAN.md §4.2, "
                        "scripts/18-vector-store-qdrant.sh)")
    return "; ".join(msgs)


@app.post("/compact")
def compact():
    return idx().compact()


@app.post("/reindex")
def reindex(r: ReindexReq):
    """Пересобрать векторы из сохранённых текстов (смена эмбеддинг-модели или RAG_DIM,
    либо наполнение Qdrant из SQLite-зеркала). Требует доступного шлюза bge-m3."""
    base = IDX.mirror if isinstance(IDX, QdrantIndex) else IDX
    names = r.docs or [d["name"] for d in base.list_docs()]
    if r.use_existing_vectors:
        if not isinstance(IDX, QdrantIndex):
            raise HTTPException(400, "use_existing_vectors применим только при RAG_BACKEND=qdrant "
                                     "(в sqlite-бэкенде векторы и так на месте)")
        return IDX.sync_from_mirror(docs=names)
    ok, fail, chunks = 0, [], 0
    for nm in names:
        try:
            texts, meta = base.doc_texts(nm)
            if not texts:
                continue
            embs = embed(texts)
            h = meta.get("sha256") or hashlib.sha256("".join(texts).encode()).hexdigest()
            IDX.upsert_doc(nm, texts, embs, meta, h)
            ok += 1
            chunks += len(texts)
        except Exception as e:                                           # noqa: BLE001
            fail.append({"doc": nm, "error": f"{type(e).__name__}: {e}"})
    return {"reindexed_docs": ok, "chunks": chunks, "failed": len(fail), "errors": fail[:20]}


@app.get("/health")
def health():
    if IDX is None:
        return {"status": "error", "error": STARTUP_ERROR, "chunks": 0}
    try:
        return {"status": "ok", "backend": IDX.name, "chunks": IDX.count(),
                "dim": DIM, "mode": DEFAULT_MODE}
    except Exception as e:                                               # noqa: BLE001
        return {"status": "degraded", "error": f"{type(e).__name__}: {e}", "chunks": 0}
