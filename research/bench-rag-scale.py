#!/usr/bin/env python3
"""
bench-rag-scale.py — замер: где ломается текущий rag-svc (SQLite + brute-force numpy)
при росте корпуса до объёма всех клинических рекомендаций Минздрава.

Каждый вариант прогоняется в ОТДЕЛЬНОМ ПРОЦЕССЕ, поэтому память изолирована
и виден реальный пик RSS на один запрос (а не сумма всех вариантов).

Варианты поискового пути (одни и те же данные, dim=1024, bge-m3):
  A) as-is    — текущий /query: SELECT всех строк (text+emb) -> np.frombuffer построчно -> np.stack -> matmul
  B) memmap   — эмбеддинги одним непрерывным .npy (memmap), только matmul; метаданные в SQLite
  C) hybrid   — SQLite FTS5 (BM25) -> top-500 кандидатов -> dense-реранк по memmap
  D) filtered — фильтр по метаданным (специальность) + dense по подмножеству (fancy index из memmap)

Размеры 105 000 и 500 000 чанков считаются ЭКСТРАПОЛЯЦИЕЙ по измеренному наклону,
если стенд не может их вместить (линейность A/B/D по N проверена на 1k/10k/50k).

Запуск:
  python3 bench-rag-scale.py                          # быстрые размеры, extrapolation до 500k
  python3 bench-rag-scale.py --measure 105000         # честный замер 105k, если RAM хватает
Целевая машина для финальных цифр — DGX Spark (20 ядер). На 2-ядерном стенде matmul
в 5-10 раз медленнее; относитльные различия A vs B/C/D при этом сохраняются.
"""
import argparse
import json
import multiprocessing as mp
import os
import random
import re
import resource
import shutil
import sqlite3
import sys
import time

import numpy as np

WORDS = ("терапия диагностика препарат доза мг противопоказания показания пациент "
         "клинический рекомендация обследование лечение анамнез синдром риск "
         "артериальный давление глюкоза инсулин антибиотик хирургический").split()
SPECS = ["кардиология", "эндокринология", "хирургия", "терапия", "педиатрия",
         "онкология", "неврология", "акушерство"]


# ---------------------------------------------------------------- синтез корпуса
_VOCAB_CACHE = {}


def vocab(size, seed=5):
    """Псевдословарь нужного объёма. Реалистичное лексическое разнообразие важно:
    на корпусе из 24 слов BM25-запрос матчит почти каждую строку, и FTS5 выглядит
    в разы хуже, чем на настоящем тексте КР (там десятки тысяч уникальных токенов)."""
    key = (size, seed)
    if key in _VOCAB_CACHE:
        return _VOCAB_CACHE[key]
    base = list(WORDS)
    g = random.Random(seed)
    syl = ["ар", "тер", "кар", "дио", "пре", "па", "рат", "доз", "а", "эн", "до", "кри",
           "но", "ги", "пер", "тен", "зи", "я", "ин", "су", "лин", "мет", "фор", "мин",
           "гли", "кем", "ц", "гист", "ол", "ог", "ик", "ан", "от", "ум", "ес", "сл"]
    while len(base) < size:
        w = "".join(g.choice(syl) for _ in range(g.randint(2, 5)))
        if w not in base:
            base.append(w)
    _VOCAB_CACHE[key] = base[:size]
    return _VOCAB_CACHE[key]


def synth_text(n_chars: int, rng: random.Random, vocab_size: int = 8000,
               zipf_a: float = 1.4) -> str:
    """Текст с Zipf-распределением частот: несколько частых терминов и длинный хвост
    редких, как в настоящей клинической документации."""
    vs = vocab(vocab_size)
    n = len(vs)
    out, total = [], 0
    while total < n_chars:
        k = rng.randint(8, 16)
        # zipf: большинство токенов редкие,немногие частые
        idxs = [min(int((1.0 - rng.random() ** (1.0 / zipf_a)) * n), n - 1) for _ in range(k)]
        s = " ".join(vs[i] for i in idxs).capitalize() + "."
        out.append(s); total += len(s) + 2
    return "\n\n".join(out)[:n_chars]


def build_db(path, npy, n_chunks, dim, chunk_chars, n_docs, seed=7):
    """SQLite ровно в схеме rag-svc (+ specialty/version для замеров фильтрации)."""
    rng, qrng = random.Random(seed), np.random.default_rng(seed)
    for p in (path, npy):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(path)
    c.execute("create table chunks(doc text, idx int, text text, emb blob, hash text, "
              "specialty text, version int, primary key(doc, idx))")
    per_doc = max(1, n_chunks // n_docs)
    mm = np.memmap(npy, dtype=np.float32, mode="w+", shape=(n_chunks, dim))
    made, rows = 0, []
    for d in range(n_docs):
        doc, spec = f"kr_{d:05d}_v{1 + d % 3}.pdf", SPECS[d % len(SPECS)]
        for i in range(per_doc):
            if made >= n_chunks:
                break
            t = synth_text(chunk_chars, rng)
            e = qrng.standard_normal(dim).astype(np.float32)
            e /= max(float(np.linalg.norm(e)), 1e-9)
            mm[made] = e
            rows.append((doc, i, t, e.tobytes(), f"h{made}", spec, 1 + d % 3))
            made += 1
        if made >= n_chunks:
            break
        if len(rows) >= 4096:
            c.executemany("insert into chunks values (?,?,?,?,?,?,?)", rows); rows = []
    if rows:
        c.executemany("insert into chunks values (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.execute("create virtual table chunks_fts using fts5(text, doc, idx, tokenize='unicode61')")
    step = 20000
    for off in range(0, made, step):
        rs = c.execute("select rowid, text, doc, idx from chunks where rowid > ? and rowid <= ? "
                       "order by rowid", (off, off + step)).fetchall()
        c.executemany("insert into chunks_fts(rowid, text, doc, idx) values (?,?,?,?)",
                      [(r[0], r[1], r[2], r[3]) for r in rs])
    c.commit(); c.close(); mm.flush(); del mm
    return made


# ---------------------------------------------------------------- варианты поиска
def _qvec(dim, seed=99):
    g = np.random.default_rng(seed)
    q = g.standard_normal(dim).astype(np.float32)
    return q / np.linalg.norm(q)


def run_A(db, dim, k, specialty=None):
    """as-is: точная копия текущего /query."""
    q = _qvec(dim)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    rows = c.execute("select doc, idx, text, emb from chunks").fetchall()
    M = np.stack([np.frombuffer(x["emb"], dtype=np.float32) for x in rows])
    sim = M @ q
    top = np.argsort(sim)[::-1][:k]
    return [(rows[i]["doc"], float(sim[i])) for i in top]


def run_B(npy, dim, k, specialty=None, db=None):
    """memmap: матрица не пересобирается."""
    q = _qvec(dim)
    n = os.path.getsize(npy) // (4 * dim)
    mm = np.memmap(npy, dtype=np.float32, mode="r", shape=(n, dim))
    sim = mm @ q
    top = np.argpartition(sim, -k)[-k:]
    return [(int(i), float(sim[i])) for i in top]


def run_D(npy, dim, k, specialty="кардиология", db=None):
    """memmap + фильтр по метаданным: id из SQLite, затем срез матрицы."""
    q = _qvec(dim)
    n = os.path.getsize(npy) // (4 * dim)
    mm = np.memmap(npy, dtype=np.float32, mode="r", shape=(n, dim))
    c = sqlite3.connect(db)
    sel = np.array([r[0] - 1 for r in c.execute(
        "select rowid from chunks where specialty=?", (specialty,))], dtype=np.int64)
    sub = mm[sel]
    sim = sub @ q
    top = np.argsort(sim)[::-1][:k]
    return [(int(sel[i]), float(sim[i])) for i in top]


def query_terms():
    vs = vocab(8000)
    # 4 редких термина из хвоста — реалистичный клинический запрос
    return [vs[4001], vs[5500], vs[6200], vs[7100]]


def run_C(db, npy, dim, k, n_cand=500, specialty=None):
    """hybrid: BM25 (FTS5) -> кандидаты -> dense-реранк."""
    q = _qvec(dim)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    match = " OR ".join(f'"{w}"*' for w in query_terms())   # как fts_match() в rag-svc
    if specialty:
        cand = [r["rowid"] - 1 for r in c.execute(
            "select f.rowid as rowid from chunks_fts f join chunks ch on ch.rowid=f.rowid "
            "where chunks_fts match ? and ch.specialty=? order by rank limit ?",
            (match, specialty, n_cand))]
    else:
        cand = [r["rowid"] - 1 for r in c.execute(
            "select rowid from chunks_fts where chunks_fts match ? order by rank limit ?",
            (match, n_cand))]
    if not cand:
        return []
    n = os.path.getsize(npy) // (4 * dim)
    mm = np.memmap(npy, dtype=np.float32, mode="r", shape=(n, dim))
    sub = mm[np.array(cand, dtype=np.int64)]
    sim = sub @ q
    top = np.argsort(sim)[::-1][:k]
    return [(cand[i], float(sim[i])) for i in top]


# ---------------------------------------------------------------- изолированный прогон
def _worker(fn_name, args, out_q, reps):
    fn = {"A": run_A, "B": run_B, "C": run_C, "D": run_D}[fn_name]
    ts, err = [], None
    try:
        for _ in range(reps):
            t = time.perf_counter(); fn(**args); ts.append(time.perf_counter() - t)
    except MemoryError as e:
        err = f"MemoryError: {e}"
    except Exception as e:                                  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    out_q.put({"t": min(ts) if ts else None, "n": len(ts),
               "rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
               "err": err})


def bench(fn_name, args, reps=3, timeout=600):
    q = mp.Queue(); p = mp.Process(target=_worker, args=(fn_name, args, q, reps), daemon=True)
    p.start(); p.join(timeout)
    if p.is_alive():
        p.terminate(); p.join()
        return {"t": None, "rss_mb": None, "err": f"timeout>{timeout}s"}
    if not q.empty():
        return q.get()
    return {"t": None, "rss_mb": None, "err": f"process died (exit={p.exitcode}) — вероятно OOM стенда"}


def peak_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# ---------------------------------------------------------------- main
def decomp(n_chunks: int, dim: int, chunk_chars: int, workdir: str, reps: int = 3):
    """Раскладывает as-is путь на фазы: видно, какая часть распараллеливается BLAS,
    а какая — однопоточный Python (и потому НЕ ускоряется от 20 ядер Spark)."""
    os.makedirs(workdir, exist_ok=True)
    db = os.path.join(workdir, "dec.db")
    npy = os.path.join(workdir, "dec.npy")
    made = build_db(db, npy, n_chunks, dim, chunk_chars, max(20, n_chunks // 130))
    q = _qvec(dim)
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    mm = np.memmap(npy, dtype=np.float32, mode="r", shape=(made, dim))
    print(f"\n# Декомпозиция as-is /query: chunks={made}, dim={dim}, cores={os.cpu_count()}")
    out = {}

    def t(label, fn):
        ts = []
        r = None
        for _ in range(reps):
            s = time.perf_counter(); r = fn(); ts.append(time.perf_counter() - s)
        ms = min(ts) * 1000
        out[label] = round(ms, 1)
        par = "  (BLAS, все ядра)" if "matmul" in label else "  (1 поток Python)"
        print(f"  {label:<32} {ms:>9.1f} ms{par}")
        return r

    rows = t("1 sqlite fetchall text+emb", lambda: c.execute("select doc, idx, text, emb from chunks").fetchall())
    arrs = t("2 np.frombuffer построчно", lambda: [np.frombuffer(x["emb"], dtype=np.float32) for x in rows])
    M = t("3 np.stack -> матрица", lambda: np.stack(arrs))
    sim = t("4 matmul M @ q", lambda: M @ q)
    t("4b matmul из memmap", lambda: mm @ q)
    t("5 argsort top-k", lambda: np.argsort(sim)[::-1][:6])
    total = out["1 sqlite fetchall text+emb"] + out["2 np.frombuffer построчно"] + \
        out["3 np.stack -> матрица"] + out["4 matmul M @ q"] + out["5 argsort top-k"]
    par = out["4 matmul M @ q"] + out["5 argsort top-k"]
    print(f"  {'ИТОГО (1+2+3+4+5)':<32} {total:>9.1f} ms")
    print(f"  из них распараллеливается только {par:.0f} ms ({par / total * 100:.0f}%); "
          f"{total - par:.0f} ms ({(total - par) / total * 100:.0f}%) — однопоточный Python")
    print(f"  → переход с 2 ядер на 20 ускорит as-is максимум в {total / (total - par + par / 10):.1f}×, "
          f"а не в 10×")
    print(f"  peak RSS процесса: {peak_mb():.0f} MB на ОДИН запрос")
    for p in (db, npy):
        if os.path.exists(p):
            os.remove(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", default="1000,10000,50000",
                    help="размеры, которые реально строим и меряем")
    ap.add_argument("--extrapolate", default="105000,500000",
                    help="размеры, для которых считаем прогноз по наклону")
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--chunk-chars", type=int, default=900)
    ap.add_argument("--chunks-per-doc", type=int, default=130,
                    help="130 ≈ одна КР на 40 страниц при chunk=900/overlap=150")
    ap.add_argument("--workdir", default="/tmp/ragbench")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--v2", default="", help="размеры для замера настоящего rag-svc v2 (через запятую)")
    ap.add_argument("--decomp", type=int, default=0,
                    help="вместо полного прогона: декомпозиция as-is на фазы для N чанков")
    a = ap.parse_args()

    if a.decomp:
        decomp(a.decomp, a.dim, a.chunk_chars, a.workdir)
        return 0
    if a.v2:
        bench_v2([int(x) for x in a.v2.split(",") if x.strip()], a.dim, a.chunk_chars,
                 a.chunks_per_doc, a.workdir)
        return 0

    measure = [int(x) for x in a.measure.split(",") if x.strip()]
    extra = [int(x) for x in a.extrapolate.split(",") if x.strip()]
    os.makedirs(a.workdir, exist_ok=True)

    print(f"# Замер rag-svc: dim={a.dim}, chunk={a.chunk_chars} chars, cores={os.cpu_count()}, "
          f"RAM доступность не контролируется — OOM возможен на больших размерах")
    print(f"# процесс-родитель peak RSS до начала: {peak_mb():.0f} MB\n")
    hdr = (f"{'chunks':>8} {'docs':>6} {'DB MB':>7} {'emb MB':>7} | "
           f"{'A as-is':>9} {'A RSS':>7} | {'B memmap':>9} | {'C hybrid':>9} {'C cand':>7} | {'D filt':>9}")
    print(hdr); print("-" * len(hdr))

    res = []
    for n in measure:
        nd = max(20, n // a.chunks_per_doc)
        db = os.path.join(a.workdir, f"index_{n}.db")
        npy = os.path.join(a.workdir, f"emb_{n}.npy")
        t0 = time.time()
        try:
            made = build_db(db, npy, n, a.dim, a.chunk_chars, nd)
        except MemoryError as e:
            print(f"{n:>8} — построение корпуса не влезло в RAM стенда: {e}")
            continue
        build_s = time.time() - t0
        db_mb = os.path.getsize(db) / 1e6
        emb_mb = made * a.dim * 4 / 1e6

        A = bench("A", {"db": db, "dim": a.dim, "k": 6}, a.reps)
        B = bench("B", {"npy": npy, "dim": a.dim, "k": 6, "db": db}, a.reps)
        C = bench("C", {"db": db, "npy": npy, "dim": a.dim, "k": 6}, a.reps)
        D = bench("D", {"npy": npy, "dim": a.dim, "k": 6, "db": db}, a.reps)

        def cell(r):
            return f"{r['t']:.4f}" if r.get("t") else (r.get("err") or "OOM")[:9]
        a_rss = f"{A['rss_mb']:.0f}" if A.get("rss_mb") else "-"
        print(f"{made:>8} {nd:>6} {db_mb:>7.0f} {emb_mb:>7.0f} | "
              f"{cell(A):>9} {a_rss:>7} | {cell(B):>9} | {cell(C):>9} {'500':>7} | {cell(D):>9}")
        res.append(dict(chunks=made, docs=nd, db_mb=round(db_mb), emb_mb=round(emb_mb),
                        build_s=round(build_s, 1), ingest_chunks_per_s=round(made / max(build_s, .01)),
                        A=A, B=B, C=C, D=D))
        if not a.keep:
            for p in (db, npy):
                if os.path.exists(p):
                    os.remove(p)

    # ---- экстраполяция по наклону A и B
    def slope(key):
        pts = [(r["chunks"], r[key]["t"]) for r in res if r[key]["t"]]
        if len(pts) < 2:
            return None
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        return (y2 - y1) / (x2 - x1), y1 - (y2 - y1) / (x2 - x1) * x1

    print("\n# Экстраполяция (линейная по N, наклон из замеров выше) — на ЭТОМ стенде:")
    for key, label in (("A", "A as-is"), ("B", "B memmap"), ("D", "D filtered")):
        sl = slope(key)
        if not sl:
            continue
        k, b = sl
        line = "  ".join(f"{n:>7}: {k * n + b:>8.2f}s" for n in extra)
        print(f"  {label:<11} {line}")
    sa = slope("A")
    if sa:
        print(f"\n  наклон A = {sa[0] * 1e6:.2f} мкс/чанк, intercept = {sa[1]:.3f} с")
        print(f"  → A на 105k чанков ≈ {sa[0] * 105000 + sa[1]:.1f} с/запрос на {os.cpu_count()} ядрах")
    if res:
        print(f"\n  объём БД на 1 чанк ≈ {res[-1]['db_mb'] / res[-1]['chunks'] * 1000:.1f} КБ "
              f"(текст + эмбеддинг {a.dim}d float32 + FTS5)")

    print("\n# JSON:")
    print(json.dumps({"measured": res, "extrapolate": extra,
                      "cores": os.cpu_count(), "dim": a.dim, "chunk_chars": a.chunk_chars},
                     ensure_ascii=False, default=str))
    if not a.keep:
        shutil.rmtree(a.workdir, ignore_errors=True)



# --------------------------------------------------------------------------- v2: реальный SqliteIndex
def bench_v2(sizes, dim, chunk_chars, chunks_per_doc, workdir, reps=3):
    """Меряет НАСТОЯЩИЙ код rag-svc v2 (SqliteIndex), а не модель: индексация,
    dense/hybrid/bm25/фильтр, память. Каждый прогон — в отдельном процессе."""
    import importlib.util
    from pathlib import Path
    svc = Path(__file__).resolve().parent.parent / "agents" / "orchestrator" / "rag-svc.py"
    print(f"\n# Замер rag-svc v2 ({svc.name}) — настоящий SqliteIndex")
    os.makedirs(workdir, exist_ok=True)
    hdr = (f"{'chunks':>8} {'docs':>6} {'DB MB':>7} | {'ingest c/s':>10} | "
           f"{'dense':>8} {'hybrid':>8} {'bm25':>8} {'filter':>8} | {'peak RSS':>9}")
    print(hdr); print("-" * len(hdr))
    out = []
    for n in sizes:
        nd = max(20, n // chunks_per_doc)
        sub = os.path.join(workdir, f"v2_{n}")
        os.makedirs(sub, exist_ok=True)
        db = os.path.join(sub, "index.db")
        vec = os.path.join(sub, "vectors.f32")
        q = mp.Queue()
        p = mp.Process(target=_v2_worker,
                       args=(str(svc), db, vec, dim, chunk_chars, chunks_per_doc, nd, n, reps, q),
                       daemon=True)
        p.start(); p.join(1800)
        if p.is_alive():
            p.terminate(); p.join()
            print(f"{n:>8} — timeout>1800s")
            continue
        r = q.get() if not q.empty() else {"err": f"процесс умер (exit={p.exitcode}) — вероятно OOM стенда"}
        if r.get("err"):
            print(f"{n:>8} — {r['err']}")
            continue
        print(f"{r['chunks']:>8} {nd:>6} {r['db_mb']:>7.0f} | {r['ingest_cps']:>10.0f} | "
              f"{r['dense']:>8.4f} {r['hybrid']:>8.4f} {r['bm25']:>8.4f} {r['filter']:>8.4f} | "
              f"{r['rss_mb']:>8.0f}M")
        out.append(r)
        shutil.rmtree(sub, ignore_errors=True)
    print("\n# JSON v2:")
    print(json.dumps(out, ensure_ascii=False))
    return out


def _v2_worker(svc_path, db, vec, dim, chunk_chars, chunks_per_doc, nd, n, reps, out_q):
    import importlib.util
    import resource as _res
    import warnings
    warnings.simplefilter("ignore")
    try:
        os.environ["RAG_SVC_DB"] = db
        os.environ["RAG_DIM"] = str(dim)
        os.environ["RAG_BACKEND"] = "sqlite"
        os.environ["RAG_QUERY_MODE"] = "hybrid"
        spec = importlib.util.spec_from_file_location("svc_under_bench", svc_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ix = mod.SqliteIndex(db_path=db, vec_path=vec, dim=dim)
        g = np.random.default_rng(11)
        rng = random.Random(11)
        t0 = time.perf_counter()
        made = 0
        for d in range(nd):
            if made >= n:
                break
            texts = [synth_text(chunk_chars, rng) for _ in range(chunks_per_doc)]
            e = g.standard_normal((len(texts), dim)).astype(np.float32)
            e /= np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-9)
            ix.upsert_doc(f"kr_{d:05d}.pdf", texts, e,
                          {"title": f"КР {d}", "specialty": SPECS[d % len(SPECS)],
                           "doctype": "clinrec", "current": 1,
                           "mkb10": f"I{d % 90}"}, f"h{d}")
            made += len(texts)
        ingest_s = time.perf_counter() - t0
        q = _qvec(dim)

        def t(fn):
            ts = []
            for _ in range(reps):
                s = time.perf_counter(); fn(); ts.append(time.perf_counter() - s)
            return min(ts)

        dense = t(lambda: ix.search(q, k=6, filters={"current": 1}, mode="dense"))
        raw = " ".join(query_terms()) + " терапия"
        hyb = t(lambda: ix.search(q, k=6, filters={"current": 1}, mode="hybrid", raw=raw))
        bm = t(lambda: ix.search(q, k=6, filters={"current": 1}, mode="bm25", raw=raw))
        flt = t(lambda: ix.search(q, k=6,
                                  filters={"current": 1, "specialty": "кардиология"},
                                  mode="hybrid", raw=raw))
        st = ix.stats()
        out_q.put({"chunks": made, "docs": nd, "db_mb": st["db_bytes"] / 1e6,
                   "vec_mb": st["vectors_bytes"] / 1e6,
                   "matrix_mb": st["dense_matrix_mb"], "resident": st["dense_resident"],
                   "ingest_s": round(ingest_s, 1), "ingest_cps": round(made / max(ingest_s, .01)),
                   "dense": round(dense, 4), "hybrid": round(hyb, 4),
                   "bm25": round(bm, 4), "filter": round(flt, 4),
                   "rss_mb": round(_res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024.0)})
    except MemoryError as e:
        out_q.put({"err": f"MemoryError: {e}"})
    except Exception as e:                                              # noqa: BLE001
        import traceback
        out_q.put({"err": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}"})


if __name__ == "__main__":
    mp.set_start_method("fork")
    sys.exit(main())
