#!/usr/bin/env python3
"""
rag_migrate_v1.py — перенос корпуса rag-svc v1 -> v2 БЕЗ повторного эмбеддинга.

v1: одна таблица chunks(doc, idx, text, emb BLOB, hash) — вектор внутри строки.
v2: текст+метаданные в SQLite (chunks со slot), векторы одним непрерывным
    vectors.f32, BM25 в chunks_fts.

Эмбеддинги переносятся как есть (bge-m3, dim определяется по первому блобу), поэтому
миграция занимает минуты, а не часы: пересчёт 100k векторов через шлюз стоил бы ~1-3 ч.

Usage:
  python3 rag_migrate_v1.py --old /srv/agents/rag-svc/index.db \
                            --new-dir /srv/agents/rag-svc/v2 [--dim 1024] [--commit]
Без --commit работает в dry-run: считает и проверяет, файлы не подменяет.
Подмену файлов на боевые делает scripts/17-rag-v2-migrate.sh (с бэкапом и откатом).
"""
import argparse
import importlib.util
import os
import shutil
import sqlite3
import sys
import time


def load_module(path, env):
    for k, v in env.items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location("rag_svc_v2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def v1_layout(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    tables = [r["name"] for r in c.execute("select name from sqlite_master where type='table'")]
    if "chunks" not in tables:
        return None, "нет таблицы chunks"
    cols = [r["name"] for r in c.execute("pragma table_info(chunks)")]
    if "emb" not in cols:
        return None, f"схема не v1 (колонки: {','.join(cols)})"
    return c, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="index.db версии v1")
    ap.add_argument("--new-dir", required=True, help="каталог для index.db + vectors.f32 версии v2")
    ap.add_argument("--module", default=None, help="путь к rag-svc.py (по умолчанию — рядом с этим файлом)")
    ap.add_argument("--dim", type=int, default=0, help="0 = определить по первому эмбеддингу")
    ap.add_argument("--commit", action="store_true", help="без этого флага — dry-run")
    a = ap.parse_args()

    if not os.path.exists(a.old):
        print(f"✗ нет файла {a.old}"); return 2
    old, why = v1_layout(a.old)
    if old is None:
        print(f"✗ {a.old}: {why}"); return 2

    mod_path = a.module or os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag-svc.py")
    if not os.path.exists(mod_path):
        mod_path = "/opt/orchestrator/rag-svc.py"

    docs = [r["doc"] for r in old.execute("select distinct doc from chunks order by doc")]
    total = old.execute("select count(*) from chunks").fetchone()[0]
    first = old.execute("select emb from chunks where emb is not null limit 1").fetchone()
    if not first or not first["emb"]:
        print("✗ в v1-базе нет ни одного эмбеддинга — мигрировать нечего, проще переимпортировать")
        return 2
    dim = a.dim or (len(first["emb"]) // 4)
    print(f"v1: {len(docs)} документов, {total} чанков, dim={dim}")
    print(f"модуль v2: {mod_path}")

    os.makedirs(a.new_dir, exist_ok=True)
    new_db = os.path.join(a.new_dir, "index.db")
    new_vec = os.path.join(a.new_dir, "vectors.f32")
    for p in (new_db, new_vec):
        if os.path.exists(p):
            os.remove(p)

    import numpy as np
    mod = load_module(mod_path, {
        "RAG_SVC_DB": new_db,
        "RAG_BACKEND": "sqlite",
        "RAG_DIM": str(dim),
        "RAG_DENSE_RESIDENT_MB": "0",          # миграция пишет через memmap, RAM не нужны
    })
    # VEC_FILE в модуле считается от каталога БД на импорте — подменяем явно
    mod.VEC_FILE = new_vec
    idx = mod.SqliteIndex(db_path=new_db, vec_path=new_vec, dim=dim)

    t0 = time.time()
    done = moved = failed = 0
    for n, doc in enumerate(docs, 1):
        rows = old.execute("select idx, text, hash from chunks where doc=? order by idx",
                           (doc,)).fetchall()
        embs = []
        ok = True
        for r in rows:
            e = old.execute("select emb from chunks where doc=? and idx=?", (doc, r["idx"])).fetchone()
            if not e or not e["emb"] or len(e["emb"]) != dim * 4:
                ok = False
                break
            embs.append(np.frombuffer(e["emb"], dtype=np.float32))
        if not ok:
            print(f"  ! {doc}: эмбеддинги неполные/другой dim — документ пропущен (переимпортировать)")
            failed += 1
            continue
        texts = [r["text"] for r in rows]
        h = rows[0]["hash"] or ""
        meta = {"title": doc, "sha256": h, "current": 1, "doctype": "unknown",
                "source_path": None, "specialty": None, "mkb10": None, "version": None,
                "nosology": None, "valid_from": None, "superseded_by": None, "ocr": 0}
        cnt, _ = idx.upsert_doc(doc, texts, np.vstack(embs), meta, h)
        moved += cnt
        done += 1
        if n % 25 == 0 or n == len(docs):
            el = time.time() - t0
            print(f"  {n}/{len(docs)} документов, {moved} чанков, {el:.0f} с "
                  f"({moved / max(el, .01):.0f} чанков/с)")

    st = idx.stats()
    print(f"\nперенесено: {done} документов, {moved} чанков (в v1 было {total})")
    if failed:
        print(f"пропущено из-за повреждённых эмбеддингов: {failed} документов")
    print(f"новый index.db: {st['db_bytes'] / 1e6:.0f} МБ, vectors.f32: {st['vectors_bytes'] / 1e6:.0f} МБ")
    print(f"слотов выделено: {st['capacity_slots']}, фрагментация: {st['fragmentation']}")

    # контрольный поиск по первому документу — убедиться, что векторы легли корректно
    probe = old.execute("select doc, text from chunks limit 1").fetchone()
    if probe:
        import numpy as np
        v = old.execute("select emb from chunks where doc=? limit 1", (probe["doc"],)).fetchone()
        q = np.frombuffer(v["emb"], dtype=np.float32)
        res, info = idx.search(q, k=3, filters={}, mode="dense")
        top = res[0]["doc"] if res else None
        same = top == probe["doc"]
        print(f"контрольный поиск по вектору из v1: top1={top} — {'СОВПАДАЕТ' if same else 'НЕ СОВПАДАЕТ'}")
        if not same:
            print("✗ векторы легли неверно — миграцию не применять")
            return 3

    if not a.commit:
        print("\ndry-run: файлы не записаны. Применить: добавьте --commit")
        for p in (new_db, new_vec):
            if os.path.exists(p):
                os.remove(p)
        return 0

    print(f"\n✓ v2-файлы готовы (старая база НЕ тронута):")
    print(f"   {new_db}")
    print(f"   {new_vec}")
    print("\nПодмену боевых файлов с бэкапом, остановкой сервиса и откатом делает:")
    print("   sudo bash scripts/17-rag-v2-migrate.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
