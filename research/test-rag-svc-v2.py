#!/usr/bin/env python3
"""
test-rag-svc-v2.py — автономная проверка rag-svc v2 (оба бэкенда), без Ollama и без GPU.

Эмбеддинги подменяются детерминированным bag-of-words вектором (1024d), поэтому
проверяется вся механика: слоты, переиспользование после удаления, фильтры по
метаданным, current/supersede, гибрид BM25+dense (RRF), /stats, /compact, /reindex,
а также миграция v1 -> v2.

Запуск (на Spark — тем же venv, что и сервис):
  /opt/rag-venv/bin/python research/test-rag-svc-v2.py
  /opt/rag-venv/bin/python research/test-rag-svc-v2.py --backend qdrant   # нужен qdrant-client
Требует: fastapi, httpx, numpy (для qdrant-ветки — qdrant-client).
"""
import argparse
import hashlib
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
SVC = REPO / "agents" / "orchestrator" / "rag-svc.py"
DIM = 1024

FAIL = []


def check(cond, label, extra=""):
    if cond:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label} {extra}")
        FAIL.append(label)


def fake_embed_factory():
    """Псевдо-эмбеддинг из мешка слов: совпадение токенов -> косинусная близость.
    Достаточно, чтобы dense-поиск вёл себя как настоящий."""
    cache = {}

    def _vec(token):
        h = hashlib.sha256(token.encode()).digest()
        g = np.random.default_rng(int.from_bytes(h[:8], "big"))
        v = g.standard_normal(DIM).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed(texts):
        out = []
        for t in texts:
            if t in cache:
                out.append(cache[t])
                continue
            toks = [w for w in "".join(
                ch if ch.isalnum() else " " for ch in t.lower()).split() if len(w) > 2]
            v = np.zeros(DIM, dtype=np.float32)
            for w in toks:
                v += _vec(w)
            n = np.linalg.norm(v)
            v = v / n if n > 1e-9 else _vec("пусто")
            cache[t] = v
            out.append(v)
        return np.vstack(out) if out else np.zeros((0, DIM), dtype=np.float32)
    return embed


def load_service(tmpdir, backend="sqlite"):
    os.environ["RAG_SVC_DB"] = str(Path(tmpdir) / "index.db")
    os.environ["RAG_SVC_DIR"] = tmpdir
    os.environ["RAG_BACKEND"] = backend
    os.environ["RAG_DIM"] = str(DIM)
    os.environ["RAG_QUERY_MODE"] = "hybrid"
    os.environ["RAG_DENSE_RESIDENT_MB"] = "4096"
    os.environ["QDRANT_URL"] = ":memory:"
    os.environ["RAG_CURRENT_ONLY"] = "1"
    for k in ("RAG_SVC_DB",):
        pass
    spec = importlib.util.spec_from_file_location("rag_svc_under_test", SVC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.VEC_FILE = str(Path(tmpdir) / "vectors.f32")
    mod.IDX = mod.build_backend()
    mod.STARTUP_ERROR = None
    mod.embed = fake_embed_factory()
    return mod


DOCS = [
    ("kr_arterialnaya_gipertenziya.pdf", "кардиология",
     "Артериальная гипертензия. Клинические рекомендации.\n\n"
     "Первая линия терапии — ингибиторы АПФ и сартаны. Целевое артериальное давление "
     "ниже 140/90 мм рт. ст. Код МКБ-10: I10, I11.\n\n"
     "При неэффективности добавляют диуретик индапамид.",
     {"title": "Артериальная гипертензия", "mkb10": "I10,I11", "doctype": "clinrec",
      "version": "2024", "specialty": "кардиология", "source_path": "/srv/kb/kardio/ag.pdf"}),
    ("kr_saharnyy_diabet_2.pdf", "эндокринология",
     "Сахарный диабет 2 типа. Клинические рекомендации.\n\n"
     "Первая линия — метформин. Целевой уровень глюкозы и HbA1c ниже 7%. "
     "Код МКБ-10: E11. Контроль артериального давления обязателен.",
     {"title": "Сахарный диабет 2 типа", "mkb10": "E11", "doctype": "clinrec",
      "version": "2025", "specialty": "эндокринология", "source_path": "/srv/kb/endo/sd2.pdf"}),
    ("prikaz_o_citologii.pdf", None,
     "Приказ о порядке оказания помощи по профилю патологическая анатомия.\n\n"
     "Цитологическое исследование, гистологическое исследование, сроки хранения "
     "препаратов, маркировка направительного документа.",
     {"title": "Приказ о цитологии и гистологии", "doctype": "order",
      "specialty": "патологическая анатомия"}),
]


def run_suite(mod, backend):
    from fastapi.testclient import TestClient
    c = TestClient(mod.app)
    print(f"\n=== бэкенд: {backend} ===")

    r = c.get("/health").json()
    check(r.get("status") == "ok" and r.get("backend") == backend, "health: сервис стартовал",
          extra=str(r))

    # --- индексация
    for name, spec, text, meta in DOCS:
        rr = c.post("/index", json={"name": name, "text": text, "meta": meta})
        check(rr.status_code == 200 and rr.json()["chunks"] > 0, f"index {name}: {rr.json().get('chunks')} чанков",
              extra=rr.text[:200])

    st = c.get("/stats").json()
    check(st["chunks"] > 0 and st["docs"] == 3, f"stats: docs={st['docs']} chunks={st['chunks']}",
          extra=str(st)[:300])
    check(st["backend"] == backend, "stats: backend совпадает")

    # --- поиск: гибридный
    q = c.post("/query", json={"q": "первая линия терапии при артериальной гипертензии", "k": 3}).json()
    check(len(q["results"]) > 0, f"query hybrid: найдено {len(q['results'])}")
    check(q["results"][0]["doc"] == "kr_arterialnaya_gipertenziya.pdf",
          "query hybrid: top1 = КР по гипертензии",
          extra=str([x["doc"] for x in q["results"]]))
    check(q.get("search", {}).get("mode") == "hybrid", "query: режим hybrid по умолчанию")

    # --- поиск: точный термин должен вытащить BM25-ветка
    q2 = c.post("/query", json={"q": "индапамид", "k": 3, "mode": "hybrid"}).json()
    check(any("индапамид" in x["text"].lower() for x in q2["results"]),
          "query: редкий термин «индапамид» найден (гибрид)")

    # --- фильтр по специальности
    q3 = c.post("/query", json={"q": "клинические рекомендации", "k": 5,
                                "filters": {"specialty": "эндокринология"}}).json()
    docs3 = {x["doc"] for x in q3["results"]}
    check(docs3 <= {"kr_saharnyy_diabet_2.pdf"}, f"filter specialty=эндокринология: {docs3 or 'пусто'}",
          extra=str(docs3))

    # --- фильтр по типу документа
    q4 = c.post("/query", json={"q": "цитологическое исследование хранение препаратов", "k": 5,
                                "filters": {"doctype": "order"}}).json()
    check({x["doc"] for x in q4["results"]} == {"prikaz_o_citologii.pdf"},
          f"filter doctype=order: {[x['doc'] for x in q4['results']]}")

    # --- supersede: устаревшая редакция уходит из поиска по умолчанию
    c.post("/index", json={"name": "kr_ag_2020.pdf", "text":
                          "Артериальная гипертензия, старая редакция 2020 года. "
                          "Устаревшие целевые значения артериального давления 150/90.",
                          "meta": {"title": "Артериальная гипертензия", "specialty": "кардиология",
                                   "doctype": "clinrec", "version": "2020"}})
    sup = c.post("/supersede", json={"name": "kr_ag_2020.pdf", "by": "kr_arterialnaya_gipertenziya.pdf",
                                     "current": False}).json()
    check(sup.get("chunks", 0) > 0, f"supersede: помечено чанков {sup.get('chunks')}")
    q5 = c.post("/query", json={"q": "устаревшие целевые значения артериального давления", "k": 5}).json()
    check(all(x["doc"] != "kr_ag_2020.pdf" for x in q5["results"]),
          "current_only=1: устаревшая редакция НЕ в выдаче",
          extra=str([x["doc"] for x in q5["results"]]))
    q6 = c.post("/query", json={"q": "устаревшие целевые значения артериального давления", "k": 5,
                                "current_only": False}).json()
    check(any(x["doc"] == "kr_ag_2020.pdf" for x in q6["results"]),
          "current_only=false: устаревшая редакция видна (аудит)")

    # --- /docs и метаданные
    d = c.get("/docs").json()
    names = {x["name"] for x in d["docs"]}
    check(names == {"kr_arterialnaya_gipertenziya.pdf", "kr_saharnyy_diabet_2.pdf",
                    "prikaz_o_citologii.pdf", "kr_ag_2020.pdf"},
          f"docs: {len(names)} документов", extra=str(names))
    ag = [x for x in d["docs"] if x["name"] == "kr_arterialnaya_gipertenziya.pdf"][0]
    check(ag["version"] == "2024" and ag["doctype"] == "clinrec",
          f"docs: карточка документа (version={ag['version']}, doctype={ag['doctype']})")
    check(ag["specialty"] == "кардиология", f"docs: specialty сохранена ({ag['specialty']})")
    check(ag["current"] is True, "docs: документ действующий")
    old = [x for x in d["docs"] if x["name"] == "kr_ag_2020.pdf"][0]
    check(old["current"] is False, "docs: устаревшая редакция помечена current=false")
    ch = c.get("/docs/kr_saharnyy_diabet_2.pdf/chunks?k=2").json()
    check(len(ch["preview"]) > 0, f"doc_chunks: {len(ch['preview'])} превью")

    # --- переиспользование слотов (регрессия против бага с _max_slot)
    if backend == "sqlite":
        ix = mod.IDX
        before_cap = ix._capacity()
        before_rows = ix._c.execute("select count(*) from chunks").fetchone()[0]
        c.post("/delete", json={"name": "kr_ag_2020.pdf"})
        free_n = ix._c.execute("select count(*) from free_slots").fetchone()[0]
        check(free_n > 0, f"delete: слоты освобождены ({free_n})")
        # повторная вставка того же объёма не должна раздуть файл
        c.post("/index", json={"name": "kr_ag_2020.pdf", "text":
                              "Артериальная гипертензия, редакция 2021. Диуретик и сартан.",
                              "meta": {"title": "Артериальная гипертензия 2021",
                                       "specialty": "кардиология", "doctype": "clinrec",
                                       "version": "2021"}})
        after_cap = ix._capacity()
        after_rows = ix._c.execute("select count(*) from chunks").fetchone()[0]
        check(after_cap == before_cap, f"слоты переиспользованы: capacity {before_cap} -> {after_cap}")
        check(after_rows == before_rows, f"число строк вернулось: {before_rows} -> {after_rows}")
        slots = [r[0] for r in ix._c.execute("select slot from chunks where doc=?",
                                             ("kr_ag_2020.pdf",))]
        check(len(set(slots)) == len(slots), f"слоты уникальны (регрессия _max_slot): {slots}")
        q7 = c.post("/query", json={"q": "редакция 2021 диуретик сартан", "k": 3}).json()
        check(any(x["doc"] == "kr_ag_2020.pdf" for x in q7["results"]),
              "перезаписанный документ находится (векторы не разъехались со строками)")

    # --- регрессия: запас роста векторного файла не должен быть кратным
    if backend == "sqlite":
        ix = mod.IDX
        n_ch = ix.count()
        cap = ix._capacity()
        check(cap <= max(1024, int(n_ch * 1.4)),
              f"запас роста в норме: capacity={cap} при {n_ch} чанков (<={max(1024, int(n_ch * 1.4))})",
              extra="— был баг с двойным счётом в _grow, файл раздувался в 2.4 раза")
        alloc = os.stat(ix.vec_path).st_blocks * 512
        check(alloc <= cap * DIM * 4,
              f"векторный файл разреженный: занято {alloc / 1e6:.1f} МБ из {cap * DIM * 4 / 1e6:.1f} МБ")

    # --- /compact
    cp = c.post("/compact").json()
    check("chunks" in cp or "noop" in cp, f"compact: {str(cp)[:120]}")
    if backend == "sqlite":
        q8 = c.post("/query", json={"q": "первая линия терапии при артериальной гипертензии", "k": 3}).json()
        check(q8["results"][0]["doc"] == "kr_arterialnaya_gipertenziya.pdf",
              "после compact поиск не сломался", extra=str([x["doc"] for x in q8["results"]]))

    # --- /reindex (пересчёт векторов из сохранённых текстов)
    if backend == "qdrant":
        ri = c.post("/reindex", json={}).json()
        check(ri.get("reindexed_docs", 0) >= 3, f"reindex в Qdrant из SQLite-зеркала: {ri.get('reindexed_docs')} док.",
              extra=str(ri)[:200])
        sy = c.post("/reindex", json={"use_existing_vectors": True}).json()
        check(sy.get("points", 0) >= 3, f"sync_from_mirror без пере-эмбеддинга: {sy.get('points')} точек",
              extra=str(sy)[:200])
        bad = c.post("/reindex", json={"use_existing_vectors": True})
        check(bad.status_code == 200, "sync_from_mirror доступен только в qdrant-режиме")
    else:
        bad = c.post("/reindex", json={"use_existing_vectors": True})
        check(bad.status_code == 400, "sqlite-бэкенд отклоняет use_existing_vectors (400)",
              extra=str(bad.status_code))

    # --- пустой запрос / узкий фильтр не должны падать
    q9 = c.post("/query", json={"q": "абракадабра", "k": 3,
                                "filters": {"specialty": "нет-такой"}}).json()
    check(q9["results"] == [] and "note" in q9, "несуществующий фильтр -> пустая выдача с note")
    return c


def test_migration(tmpdir):
    """v1 -> v2: строим базу v1, мигрируем, проверяем, что векторы легли верно."""
    print("\n=== миграция v1 -> v2 ===")
    v1_dir = Path(tmpdir) / "v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    v1_db = v1_dir / "index.db"
    c = sqlite3.connect(v1_db)
    c.execute("create table chunks(doc text, idx int, text text, emb blob, hash text, "
              "primary key(doc, idx))")
    g = np.random.default_rng(3)
    texts = {"doc_a": ["ингибиторы АПФ первая линия при гипертензии",
                       "целевое артериальное давление ниже 140/90"],
             "doc_b": ["метформин первая линия при диабете 2 типа",
                       "контроль глюкозы и HbA1c"]}
    for doc, ts in texts.items():
        for i, t in enumerate(ts):
            e = g.standard_normal(DIM).astype(np.float32)
            e /= np.linalg.norm(e)
            c.execute("insert into chunks values (?,?,?,?,?)",
                      (doc, i, t, e.tobytes(), hashlib.sha256(t.encode()).hexdigest()[:16]))
    c.commit(); c.close()

    mig = REPO / "agents" / "orchestrator" / "rag_migrate_v1.py"
    import subprocess
    new_dir = Path(tmpdir) / "v2"
    env = dict(os.environ, RAG_SVC_DB=str(new_dir / "index.db"), RAG_BACKEND="sqlite",
               RAG_DIM=str(DIM), RAG_DENSE_RESIDENT_MB="0", PYTHONPATH="")
    r = subprocess.run([sys.executable, str(mig), "--old", str(v1_db),
                        "--new-dir", str(new_dir), "--dim", str(DIM), "--commit"],
                       capture_output=True, text=True, env=env, timeout=300)
    out = r.stdout + r.stderr
    check(r.returncode == 0, f"миграция завершилась (rc={r.returncode})", extra=out[-400:])
    check("СОВПАДАЕТ" in out, "контрольный поиск по вектору v1: top1 совпал",
          extra=[l for l in out.splitlines() if "контрольный" in l][:1])
    check((new_dir / "index.db").exists() and (new_dir / "vectors.f32").exists(),
          "созданы index.db и vectors.f32")

    # открываем мигрированную базу сервисом и ищем
    spec = importlib.util.spec_from_file_location("rag_svc_migrated", SVC)
    mod = importlib.util.module_from_spec(spec)
    os.environ["RAG_SVC_DB"] = str(new_dir / "index.db")
    os.environ["RAG_DIM"] = str(DIM)
    os.environ["RAG_BACKEND"] = "sqlite"
    os.environ["RAG_DENSE_RESIDENT_MB"] = "0"
    spec.loader.exec_module(mod)
    mod.VEC_FILE = str(new_dir / "vectors.f32")
    mod.IDX = mod.SqliteIndex(db_path=str(new_dir / "index.db"),
                              vec_path=str(new_dir / "vectors.f32"), dim=DIM)
    check(mod.IDX.count() == 4, f"после миграции чанков: {mod.IDX.count()} (ожидалось 4)")
    st = mod.IDX.stats()
    check(st["docs"] == 2, f"после миграции документов: {st['docs']}")
    check(st["dense_resident"] is False, "resident=0 -> матрица в memmap (экономия RAM при миграции)")
    # v2-сервис должен отказаться открывать v1-базу
    spec2 = importlib.util.spec_from_file_location("rag_svc_v1guard", SVC)
    mod2 = importlib.util.module_from_spec(spec2)
    os.environ["RAG_SVC_DB"] = str(v1_db)
    try:
        spec2.loader.exec_module(mod2)
        mod2.VEC_FILE = str(Path(tmpdir) / "guard.f32")
        mod2.SqliteIndex(db_path=str(v1_db), vec_path=str(Path(tmpdir) / "guard.f32"), dim=DIM)
        guard_ok = False
    except RuntimeError as e:
        guard_ok = "v1" in str(e)
    check(guard_ok, "v2 отказывается открывать схему v1 (защита от тихой потери векторов)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="both", choices=["sqlite", "qdrant", "both"])
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="ragtest-")
    print(f"# тестовый каталог: {tmp}")
    print(f"# сервис: {SVC}")
    try:
        backends = ["sqlite", "qdrant"] if a.backend == "both" else [a.backend]
        for b in backends:
            sub = Path(tmp) / b
            sub.mkdir(parents=True, exist_ok=True)
            try:
                mod = load_service(str(sub), b)
            except ImportError as e:
                print(f"  ! бэкенд {b} недоступен ({e}) — пропускаю")
                continue
            run_suite(mod, b)
        test_migration(tmp)
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL:
        print(f"✗ ПРОВАЛЕНО: {len(FAIL)}")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print("✓ все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
