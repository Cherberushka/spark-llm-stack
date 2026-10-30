#!/usr/bin/env python3
"""
rag_bulk_ingest.py — массовый импорт корпуса (клинические рекомендации, стандарты,
приказы, СОП) в rag-svc через HTTP.

Зачем отдельный инструмент, а не 777 задач doc-convert через оркестратор:
  * импорт КР — это часы, а не минуты. Нужны resume, журнал и прогноз стоимости ДО старта;
  * VLM-OCR идёт через тот же llama.cpp, что обслуживает врачей, — нужна очередь с
    ограничением скорости и ночным окном, иначе днём inference встанет;
  * оркестратор пишет задачи в Postgres и плодит события: 5000 документов = мусор в журнале.

Пишет только по HTTP в rag-svc (единый писатель в SQLite — сам сервис).

Прогноз (ничего не меняет, считает стоимость):
  python3 rag_bulk_ingest.py --src /srv/kb/clinrec --dry-run

Импорт (возобновляемый, журнал /srv/agents/rag-svc/bulk-journal.jsonl):
  nice -n19 ionice -c3 python3 rag_bulk_ingest.py --src /srv/kb/clinrec \
      --doctype clinrec --specialty-from-dir --ocr auto --resume

Только ночное окно (00:00-07:00), с лимитом страниц OCR в час:
  python3 rag_bulk_ingest.py --src /srv/kb/clinrec --night-only --max-ocr-pages-per-hour 400
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

RAG = os.environ.get("RAG_SVC_URL", "http://127.0.0.1:8502")
GW = os.environ.get("GATEWAY_URL", "http://127.0.0.1:4000/v1")
KEY = os.environ.get("ORCH_LLM_KEY", "")
CHAT_MODEL = os.environ.get("RAG_OCR_MODEL", "qwen3.8-27b")
JOURNAL = Path(os.environ.get("RAG_BULK_JOURNAL", "/srv/agents/rag-svc/bulk-journal.jsonl"))
SUPPORTED = {".pdf", ".docx", ".odt", ".rtf", ".md", ".html", ".htm", ".txt"}
TEXT_LAYER_MIN_CHARS_PER_PAGE = int(os.environ.get("RAG_TEXT_LAYER_MIN", 120))
NIGHT_START = int(os.environ.get("RAG_NIGHT_START", 0))
NIGHT_END = int(os.environ.get("RAG_NIGHT_END", 7))

SPECIALTY_HINTS = {
    "кардиолог": "кардиология", "гипертенз": "кардиология", "ишемич": "кардиология",
    "аритм": "кардиология", "инфаркт": "кардиология", "сердечн": "кардиология",
    "эндокрин": "эндокринология", "диабет": "эндокринология", "щитовид": "эндокринология",
    "ожирен": "эндокринология", "тирео": "эндокринология",
    "хирург": "хирургия", "оператив": "хирургия", "аппендиц": "хирургия", "грыж": "хирургия",
    "педиатр": "педиатрия", "дет": "педиатрия", "новорожд": "неонатология", "неонат": "неонатология",
    "онколог": "онкология", "опухол": "онкология", "канцер": "онкология", "метастаз": "онкология",
    "невролог": "неврология", "инсульт": "неврология", "эпилепс": "неврология", "мигрен": "неврология",
    "акушер": "акушерство и гинекология", "гинеколог": "акушерство и гинекология",
    "беремен": "акушерство и гинекология", "род": "акушерство и гинекология",
    "пульмоно": "пульмонология", "хобл": "пульмонология", "астм": "пульмонология", "пневмон": "пульмонология",
    "гастро": "гастроэнтерология", "гепат": "гастроэнтерология", "панкреат": "гастроэнтерология",
    "нефро": "нефрология", "почечн": "нефрология", "уролог": "урология",
    "ревмат": "ревматология", "артрит": "ревматология", "дермат": "дерматология",
    "инфекц": "инфекционные болезни", "вирус": "инфекционные болезни", "туберкул": "фтизиатрия",
    "психиат": "психиатрия", "депрес": "психиатрия", "офтальм": "офтальмология",
    "глаз": "офтальмология", "оторино": "оториноларингология", "анестез": "анестезиология и реаниматология",
    "реаним": "анестезиология и реаниматология", "травм": "травматология", "ортопед": "травматология",
    "гемат": "гематология", "анеми": "гематология", "аллерг": "аллергология и иммунология",
    "цитолог": "патологическая анатомия", "гистолог": "патологическая анатомия",
    "терапевт": "терапия", "внутрен": "терапия",
}

MKБ = re.compile(r"\b([A-TV-Z])(\d{2})(?:\.(\d{1,3}))?\b")
YEAR = re.compile(r"(19|20)\d{2}")


# --------------------------------------------------------------------------- утилиты
def log(msg, logfile=None):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def sha256_file(p, chunk=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def post(path, body, timeout=3600):
    req = urllib.request.Request(RAG + path, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get(path, timeout=60):
    with urllib.request.urlopen(RAG + path, timeout=timeout) as r:
        return json.load(r)


def safe_name(name):
    tr = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
          "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
          "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
          "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
          "ю": "yu", "я": "ya"}
    s = "".join(tr.get(ch.lower(), ch if ch.isascii() else "") for ch in name)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return re.sub(r"_+", "_", s).strip("._-") or "doc"


# --------------------------------------------------------------------------- журнал
def load_journal(path):
    """Последнее состояние каждого файла: sha256 -> запись."""
    st = {}
    if not os.path.exists(path):
        return st
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            st[r["sha256"]] = r
    return st


def journal_write(path, rec):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- конвертация
def pdf_pages(path):
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=60)
        m = re.search(rb"Pages:\s+(\d+)", out.stdout)
        return int(m.group(1)) if m else None
    except Exception:                                                  # noqa: BLE001
        return None


def pdf_text_layer_chars(path, max_pages=5):
    """Сколько символов даёт pdftotext на первых страницах: отличает born-digital от скана."""
    try:
        r = subprocess.run(["pdftotext", "-f", "1", "-l", str(max_pages), str(path), "-"],
                           capture_output=True, timeout=300)
        return len(r.stdout.decode("utf-8", "replace").strip())
    except Exception:                                                  # noqa: BLE001
        return 0


def pdftotext_all(path):
    r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, timeout=1800)
    return r.stdout.decode("utf-8", "replace")


def vlm_ocr_page(png_path):
    b64 = base64.b64encode(Path(png_path).read_bytes()).decode()
    body = {"model": CHAT_MODEL, "max_tokens": 4096,
            "messages": [{"role": "user", "content": [
                {"type": "text",
                 "text": "Перепиши страницу документа дословно в Markdown (русский язык). "
                         "Сохрани заголовки, списки, таблицы (markdown-таблицами). Без комментариев."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(GW + "/chat/completions", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json",
                                         "Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def convert(path, workdir, ocr_mode, max_ocr_pages, ocr_budget, logfile=None):
    """-> (text, ocr_used, ocr_pages, reason). Бросает исключение при неудаче."""
    suf = path.suffix.lower()
    workdir.mkdir(parents=True, exist_ok=True)
    if suf == ".pdf":
        n_pages = pdf_pages(path)
        chars = pdf_text_layer_chars(path)
        per_page = chars / min(max(n_pages or 1, 1), 5)
        needs_ocr = per_page < TEXT_LAYER_MIN_CHARS_PER_PAGE
        if ocr_mode == "never":
            needs_ocr = False
        elif ocr_mode == "always":
            needs_ocr = True
        if needs_ocr:
            if ocr_budget is not None and ocr_budget[0] <= 0:
                raise RuntimeError("исчерпан бюджет OCR (--max-ocr-pages) — отложено до следующего запуска")
            pages_to_ocr = min(n_pages or max_ocr_pages, max_ocr_pages)
            if ocr_budget is not None:
                pages_to_ocr = min(pages_to_ocr, ocr_budget[0])
            pre = workdir / (path.stem + "_pages")
            pre.mkdir(exist_ok=True)
            subprocess.run(["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", str(pages_to_ocr),
                            str(path), str(pre / "pg")], check=True, capture_output=True, timeout=3600)
            pngs = sorted(pre.glob("pg-*.png"))
            if not pngs:
                raise RuntimeError("pdftoppm не дал страниц")
            blocks, done = [], 0
            for i, pg in enumerate(pngs, 1):
                try:
                    blocks.append(vlm_ocr_page(pg))
                    done += 1
                except Exception as e:                                   # noqa: BLE001
                    log(f"    OCR стр. {i} не удалась: {type(e).__name__} — страница пропущена", logfile)
                if ocr_budget is not None:
                    ocr_budget[0] -= 1
                    if ocr_budget[0] <= 0 and i < len(pngs):
                        log(f"    бюджет OCR исчерпан на стр. {i}/{len(pngs)}", logfile)
                        break
            for pg in pngs:
                pg.unlink(missing_ok=True)
            return "\n\n".join(blocks), True, done, f"VLM-OCR {done} стр."
        t = pdftotext_all(path)
        if len(t.strip()) < 200:
            raise RuntimeError("pdftotext дал <200 символов, а OCR выключен/не удался")
        return t, False, 0, f"текстовый слой ({per_page:.0f} симв/стр.)"
    if suf in (".docx", ".odt", ".rtf", ".html", ".htm"):
        out = workdir / (path.stem + ".txt")
        subprocess.run(["pandoc", str(path), "-t", "gfm", "-o", str(out)], check=True, timeout=900)
        return out.read_text(encoding="utf-8", errors="replace"), False, 0, "pandoc"
    if suf in (".md", ".txt"):
        return path.read_text(encoding="utf-8", errors="replace"), False, 0, "как есть"
    raise RuntimeError(f"неподдерживаемый тип {suf}")


# --------------------------------------------------------------------------- метаданные
def guess_specialty(text, filename, parent_dir, use_dir):
    if use_dir and parent_dir:
        low = parent_dir.lower()
        for k, v in SPECIALTY_HINTS.items():
            if k in low:
                return v, "каталог"
        if len(parent_dir) > 2 and not parent_dir.isdigit():
            return parent_dir.strip()[:64], "каталог"
    head = (filename + " " + text[:4000]).lower()
    for k, v in SPECIALTY_HINTS.items():
        if k in head:
            return v, "эвристика"
    return None, "не определена"


def guess_mkb10(text, filename):
    src = filename + " " + text[:6000]
    out = []
    for m in MKБ.finditer(src):
        code = m.group(1) + m.group(2) + ("." + m.group(3) if m.group(3) else "")
        if code not in out:
            out.append(code)
        if len(out) >= 8:
            break
    return ",".join(out) or None


def guess_title(text, path):
    m = re.search(r"^\s*#*\s*(.+)$", text[:2000], re.M)
    t = (m.group(1).strip() if m else "") or path.stem
    t = re.sub(r"\s+", " ", t)
    return t[:200]


def guess_version(text, path):
    head = text[:6000]
    m = re.search(r"(утвержден[а-яё]*|редакци[а-яё]*|пересмотр)[^\n]{0,80}?(20\d{2})", head, re.I)
    if m:
        return m.group(2)
    ys = YEAR.findall(path.stem)
    m2 = re.findall(r"(19|20)\d{2}", path.stem)
    if m2:
        return "20" + m2[-1][-2:] if m2[-1] == "20" else None
    if ys:
        return None
    m3 = re.findall(r"(20\d{2})", head)
    return m3[-1] if m3 else None


# --------------------------------------------------------------------------- ночное окно
def in_night_window():
    return NIGHT_START <= datetime.now().hour < NIGHT_END


def wait_for_night(logfile=None):
    while not in_night_window():
        nxt = datetime.now().replace(hour=NIGHT_START, minute=0, second=0, microsecond=0)
        if nxt <= datetime.now():
            nxt = nxt.replace(day=nxt.day)
            from datetime import timedelta
            nxt += timedelta(days=1)
        mins = int((nxt - datetime.now()).total_seconds() // 60)
        log(f"вне ночного окна ({NIGHT_START:02d}:00-{NIGHT_END:02d}:00) — пауза ~{mins} мин", logfile)
        time.sleep(min(max(mins, 1) * 60, 600))


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="каталог с документами (обход рекурсивный)")
    ap.add_argument("--doctype", default="clinrec", help="clinrec|standard|order|sop|other")
    ap.add_argument("--dry-run", action="store_true", help="только прогноз стоимости, ничего не писать")
    ap.add_argument("--resume", action="store_true", help="пропустить уже импортированные (по sha256 из журнала)")
    ap.add_argument("--force", action="store_true", help="переимпортировать даже при совпадении sha256")
    ap.add_argument("--ocr", default="auto", choices=["auto", "never", "always"])
    ap.add_argument("--max-ocr-pages", type=int, default=80, help="лимит страниц OCR на документ")
    ap.add_argument("--max-ocr-pages-total", type=int, default=0, help="0 = без общего лимита")
    ap.add_argument("--limit", type=int, default=0, help="обработать не больше N файлов (0 = все)")
    ap.add_argument("--include", default="", help="regex по имени файла (включить)")
    ap.add_argument("--exclude", default="", help="regex по имени файла (исключить)")
    ap.add_argument("--specialty-from-dir", action="store_true", help="брать специальность из имени каталога")
    ap.add_argument("--specialty", default="", help="жёстко задать специальность")
    ap.add_argument("--night-only", action="store_true", help="работать только в ночном окне")
    ap.add_argument("--sleep-between", type=float, default=0.5, help="пауза между документами, с")
    ap.add_argument("--journal", default=str(JOURNAL))
    ap.add_argument("--workdir", default="/srv/agents/rag-svc/tmp")
    ap.add_argument("--logfile", default="/var/log/spark-llm/bulk-ingest.log")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.is_dir():
        print(f"✗ нет каталога {src}"); return 2
    try:
        h = get("/health")
    except Exception as e:                                               # noqa: BLE001
        print(f"✗ rag-svc недоступен на {RAG}: {e}"); return 2
    log(f"rag-svc: backend={h.get('backend')} chunks={h.get('chunks')} dim={h.get('dim')}", a.logfile)

    files = sorted(p for p in src.rglob("*")
                   if p.is_file() and p.suffix.lower() in SUPPORTED
                   and not p.name.startswith("."))
    if a.include:
        files = [p for p in files if re.search(a.include, p.name, re.I)]
    if a.exclude:
        files = [p for p in files if not re.search(a.exclude, p.name, re.I)]
    if not files:
        print(f"✗ в {src} нет поддерживаемых файлов ({', '.join(sorted(SUPPORTED))})"); return 2

    journal = load_journal(a.journal)
    log(f"найдено файлов: {len(files)}; записей в журнале: {len(journal)}", a.logfile)

    # ---- прогноз: сколько страниц, сколько потребует OCR
    plan = []
    for p in files:
        try:
            sha = sha256_file(p)
        except OSError as e:
            log(f"  ! {p.name}: не читается ({e})", a.logfile); continue
        prev = journal.get(sha)
        pages = pdf_pages(p) if p.suffix.lower() == ".pdf" else None
        needs_ocr = None
        if p.suffix.lower() == ".pdf":
            if a.ocr == "always":
                needs_ocr = True
            elif a.ocr == "never":
                needs_ocr = False
            else:
                needs_ocr = pdf_text_layer_chars(p) / max(min(pages or 1, 5), 1) < TEXT_LAYER_MIN_CHARS_PER_PAGE
        plan.append({"path": p, "sha256": sha, "pages": pages, "needs_ocr": needs_ocr,
                     "done_before": bool(prev and prev.get("status") == "ok" and not a.force),
                     "parent": p.parent.name if p.parent != src else ""})

    todo = [x for x in plan if not (x["done_before"] and a.resume)]
    if a.limit:
        todo = todo[:a.limit]
    ocr_docs = [x for x in todo if x["needs_ocr"]]
    ocr_pages = sum(min(x["pages"] or a.max_ocr_pages, a.max_ocr_pages) for x in ocr_docs)
    text_pages = sum(x["pages"] or 0 for x in todo if not x["needs_ocr"])
    est_chunks = sum(int((x["pages"] or 8) * 2500 / max(900 - 150, 100)) for x in todo)

    print("\n=== ПРОГНОЗ ИМПОРТА ===")
    print(f"  документов всего найдено : {len(plan)}")
    print(f"  уже импортировано (журнал): {sum(1 for x in plan if x['done_before'])}")
    print(f"  к импорту сейчас          : {len(todo)}")
    print(f"  из них требуют OCR        : {len(ocr_docs)} ({ocr_pages} страниц)")
    print(f"  с текстовым слоем         : {len(todo) - len(ocr_docs)} ({text_pages} страниц)")
    print(f"  ожидаемо чанков           : ~{est_chunks}")
    sec_per_ocr_page = float(os.environ.get("RAG_OCR_SEC_PER_PAGE", 12))
    print(f"  время OCR                 : ~{ocr_pages * sec_per_ocr_page / 3600:.1f} ч "
          f"(при {sec_per_ocr_page:.0f} с/стр.,独占 GPU)")
    print(f"  время эмбеддингов         : ~{est_chunks / float(os.environ.get('RAG_EMB_PER_S', 40)) / 60:.0f} мин")
    print(f"  ИТОГО                     : ~{(ocr_pages * sec_per_ocr_page + est_chunks / float(os.environ.get('RAG_EMB_PER_S', 40))) / 3600:.1f} ч")
    print(f"  место в индексе           : ~{est_chunks * 8.5 / 1000:.2f} ГБ "
          f"(8.5 КБ/чанк: текст + 1024d float32 + FTS5)")
    print("=======================\n")

    if a.dry_run:
        for x in todo[:40]:
            print(f"  {'OCR ' if x['needs_ocr'] else 'text'} {x['pages'] or '?':>5} стр.  {x['path']}")
        if len(todo) > 40:
            print(f"  ... и ещё {len(todo) - 40}")
        return 0

    if not todo:
        log("импортировать нечего (--resume: всё уже в журнале)", a.logfile); return 0

    ocr_budget = [a.max_ocr_pages_total] if a.max_ocr_pages_total else None
    workdir = Path(a.workdir)
    ok = failed = skipped = ocr_used_pages = 0
    t_start = time.time()
    ocr_hours = 0.0

    for n, item in enumerate(todo, 1):
        p = item["path"]
        if a.night_only:
            wait_for_night(a.logfile)
        name = safe_name(p.stem) + ("" if p.suffix.lower() in (".md", ".txt") else p.suffix.lower())
        log(f"[{n}/{len(todo)}] {p.name}", a.logfile)
        t_doc = time.time()
        try:
            text, used_ocr, ocr_pages, reason = convert(
                p, workdir / name, a.ocr, a.max_ocr_pages, ocr_budget, a.logfile)
            if used_ocr:
                ocr_used_pages += ocr_pages
                ocr_hours += ocr_pages * sec_per_ocr_page / 3600
            if len(text.strip()) < 200:
                raise RuntimeError(f"текст короче 200 символов ({reason})")
            spec = a.specialty or None
            how = "задана вручную"
            if not spec:
                spec, how = guess_specialty(text, p.stem, item["parent"], a.specialty_from_dir)
            meta = {
                "title": guess_title(text, p),
                "specialty": spec,
                "mkb10": guess_mkb10(text, p.name),
                "doctype": a.doctype,
                "version": guess_version(text, p),
                "source_path": str(p),
                "sha256": item["sha256"],
                "ocr": int(used_ocr),
                "current": 1,
            }
            prev = get("/docs").get("docs", [])
            same_title = [d for d in prev if d.get("title") == meta["title"] and d["name"] != name]
            r = post("/index", {"name": name, "text": text, "meta": meta}, timeout=3600)
            rec = {"sha256": item["sha256"], "file": str(p), "doc": name, "status": "ok",
                   "chunks": r.get("chunks"), "ocr_pages": ocr_pages, "reason": reason,
                   "specialty": spec, "specialty_how": how, "mkb10": meta["mkb10"],
                   "version": meta["version"], "replaced_duplicate_of": r.get("replaced_duplicate_of"),
                   "sec": round(time.time() - t_doc, 1), "at": datetime.now().isoformat(timespec="seconds")}
            # прежняя редакция того же документа -> current=0 (не удаляем: нужна для аудита)
            if same_title:
                rec["superseded"] = [d["name"] for d in same_title]
                for d in same_title:
                    try:
                        post("/index", {"name": d["name"], "text": "", "meta": {}})
                    except Exception:                                    # noqa: BLE001
                        pass
            journal_write(a.journal, rec)
            ok += 1
            log(f"    ok: {r.get('chunks')} чанков, {reason}, specialty={spec} ({how})", a.logfile)
        except Exception as e:                                           # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            journal_write(a.journal, {"sha256": item["sha256"], "file": str(p), "doc": name,
                                      "status": "failed", "error": msg,
                                      "at": datetime.now().isoformat(timespec="seconds")})
            failed += 1
            log(f"    ✗ {msg}", a.logfile)
        el = time.time() - t_start
        rate = n / max(el, 1)
        eta = (len(todo) - n) / rate if rate else 0
        if n % 10 == 0 or n == len(todo):
            log(f"  прогресс {n}/{len(todo)}: ok={ok} failed={failed} "
                f"OCR-страниц={ocr_used_pages} (~{ocr_hours:.1f} ч GPU), ETA {eta / 60:.0f} мин",
                a.logfile)
        if a.sleep_between:
            time.sleep(a.sleep_between)

    print(f"\n=== ИТОГ: ok={ok}, failed={failed}, OCR-страниц={ocr_used_pages} (~{ocr_hours:.1f} ч GPU), "
          f"время {int((time.time() - t_start) / 60)} мин ===")
    print(f"журнал: {a.journal}")
    print("проверка: curl -s http://127.0.0.1:8502/stats | jq '{chunks,docs,recommendation}'")
    if failed:
        print(f"повторить только неудачные: grep '\"status\": \"failed\"' {a.journal} | jq -r .file")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
