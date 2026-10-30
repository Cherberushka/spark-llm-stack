#!/usr/bin/env python3
"""Экспорт скиллов проекта в формат DeepSeek Harness (dsh).

Что делает: берёт курируемые скиллы из vendor-skills/, роль-карточки кодовой
линии P4 (/etc/agents-roles/*.md, fallback — heredoc'и scripts/16) и служебные
скиллы из agents/dsh/skills-src/, приводит frontmatter к требованиям dsh и
складывает всё в каталог, готовый к копированию в корень скиллов dsh.

Формат dsh (проверено по docs dsh-skill-filesystem, 2026-10):
  * скилл = каталог <name>/SKILL.md ИЛИ плоский <name>.md в корне сканирования;
  * frontmatter: name (kebab-case, 1-64) и description (1-1024) обязательны;
    необязательны license, compatibility, metadata, allowed-tools, whenToUse,
    disable-model-invocation, user-invocable;
  * вложенные **/SKILL.md НЕ обнаруживаются — ровно один уровень под корнем;
  * корни (ранг, меньший побеждает при коллизии имён):
      100 <projectRoot>/.dsh/skills   200 <projectRoot>/.agents/skills
      300 Config.customSkillDirs      400 <dshHome>/skills (~/.dsh)
      500 <agentsHome>/skills (~/.agents)
  * в системный промпт попадают name+description; тело грузится инструментом
    skill по имени или жестом /name в сообщении.

Запуск:
  python3 agents/dsh/export-dsh-skills.py --out ~/dsh-skills-export
  python3 agents/dsh/export-dsh-skills.py --set code --no-zip
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DESC_MAX = 1024
DSH_KEYS = ("name", "description", "license", "compatibility", "allowed-tools",
            "whenToUse", "disable-model-invocation", "user-invocable", "metadata")
ROLE_HEREDOC = {"implementer": "ROLE_IMP", "qa": "ROLE_QA",
                "reviewer": "ROLE_REV", "security": "ROLE_SEC"}

# name, source (относительно PROJ или "role:<роль>"), наборы, происхождение,
# локальные зависимости на машине dsh, замена description (если у источника её нет)
SKILLS = [
    dict(name="mp-code-review", src="vendor-skills/mattpocock/code-review",
         sets={"code"}, upstream="mattpocock/skills · engineering/code-review",
         deps="нет (чистый markdown)"),
    dict(name="mp-diagnosing-bugs", src="vendor-skills/mattpocock/diagnosing-bugs",
         sets={"code"}, upstream="mattpocock/skills · engineering/diagnosing-bugs",
         deps="нет (чистый markdown)"),
    dict(name="mp-tdd", src="vendor-skills/mattpocock/tdd",
         sets={"code"}, upstream="mattpocock/skills · engineering/tdd",
         deps="нет (чистый markdown)"),
    dict(name="mp-implement", src="vendor-skills/mattpocock/implement",
         sets={"code"}, upstream="mattpocock/skills · engineering/implement",
         deps="нет (чистый markdown)"),
    dict(name="mp-to-spec", src="vendor-skills/mattpocock/to-spec",
         sets={"code"}, upstream="mattpocock/skills · engineering/to-spec",
         deps="нет (чистый markdown)"),
    dict(name="mp-research", src="vendor-skills/mattpocock/research",
         sets={"code"}, upstream="mattpocock/skills · engineering/research",
         deps="нет для чтения; сам процесс research предполагает доступ к источникам"),
    dict(name="mp-handoff", src="vendor-skills/mattpocock/handoff",
         sets={"docs"}, upstream="mattpocock/skills · productivity/handoff",
         deps="нет (чистый markdown)"),
    dict(name="mp-writing-for-agents", src="vendor-skills/mattpocock/writing-for-agents",
         sets={"docs"}, upstream="mattpocock/skills · productivity/writing-for-agents",
         deps="нет (чистый markdown)"),
    dict(name="mp-wizard", src="vendor-skills/mattpocock/wizard",
         sets={"ops"}, upstream="mattpocock/skills · engineering/wizard",
         deps="bash на машине dsh (генерирует интерактивные мастера)"),
    dict(name="ant-xlsx", src="vendor-skills/anthropics/xlsx",
         sets={"docs"}, upstream="anthropics/skills · xlsx",
         deps="python3 + openpyxl/pandas; для recalc — LibreOffice (soffice)"),
    dict(name="ant-docx", src="vendor-skills/anthropics/docx",
         sets={"docs"}, upstream="anthropics/skills · docx",
         deps="node + docx/pptxgenjs/markitdown; LibreOffice для accept-changes"),
    dict(name="ant-pptx", src="vendor-skills/anthropics/pptx",
         sets={"docs"}, upstream="anthropics/skills · pptx",
         deps="node + pptxgenjs/react-icons/sharp; LibreOffice"),
    dict(name="ant-pdf", src="vendor-skills/anthropics/pdf",
         sets={"docs"}, upstream="anthropics/skills · pdf",
         deps="python3 + pypdf/pdfplumber; для OCR — pytesseract, pdf2image, tesseract"),
    dict(name="ant-doc-coauthoring", src="vendor-skills/anthropics/doc-coauthoring",
         sets={"docs"}, upstream="anthropics/skills · doc-coauthoring",
         deps="нет (чистый markdown)"),
    dict(name="unlazy", src="vendor-skills/unlazy",
         sets={"code"}, upstream="Leonxlnx/unlazy",
         deps="нет (чистый markdown)"),
    dict(name="taste-skill", src="vendor-skills/taste-skill",
         sets={"frontend"}, upstream="Leonxlnx/taste-skill (design-taste-frontend)",
         deps="node 22+ и локальный Chrome/Edge/Chromium; assets.mjs — интернет",
         note="имя в dsh = taste-skill (в upstream-файле design-taste-frontend); "
              "библиотека 1,7 МБ идёт целиком"),
    dict(name="spark-role-implementer", src="role:implementer",
         sets={"roles", "code"}, upstream="роль-карточка оркестратора P4",
         deps="нет (процедура)",
         desc="Роль implementer кодовой линии P4 (DGX Spark): внести МИНИМАЛЬНЫЕ "
              "правки по спецификации задачи, не трогая несвязанный код, и честно "
              "перечислить ВСЕ изменённые файлы. Использовать, когда код правится "
              "по чужой спецификации и результат пойдёт на человеческий гейт.",
         invocable={"user-invocable": "true"}),
    dict(name="spark-role-qa", src="role:qa",
         sets={"roles", "code"}, upstream="роль-карточка оркестратора P4",
         deps="нет (процедура)",
         desc="Роль qa кодовой линии P4 (DGX Spark): прогнать тесты проекта, при "
              "отсутствии тестов на изменённый код написать минимальные, проверить "
              "gate «как выглядит готово» и выдать вердикт PASS или FAIL со списком "
              "дефектов (файл:строка). Использовать перед ревью правки.",
         invocable={"user-invocable": "true"}),
    dict(name="spark-role-reviewer", src="role:reviewer",
         sets={"roles", "code"}, upstream="роль-карточка оркестратора P4",
         deps="нет (процедура)",
         desc="Роль reviewer кодовой линии P4 (DGX Spark), read-only: прочитать git "
              "diff и выводы qa, оценить корректность, безопасность, стиль проекта и "
              "достаточность тестов, выдать вердикт APPROVE или REQUEST_CHANGES с "
              "нумерованными замечаниями (файл:строка). Файлы не менять.",
         invocable={"user-invocable": "true"}),
    dict(name="spark-role-security", src="role:security",
         sets={"roles", "code"}, upstream="роль-карточка оркестратора P4",
         deps="нет (процедура)",
         desc="Роль security-reviewer кодовой линии P4 (DGX Spark), read-only: "
              "чек-лист инъекций (SQL/команды/шаблоны), секретов в коде и логах, "
              "уязвимых зависимостей, обработки входа и загрузок файлов, прав и path "
              "traversal; вывод — находки с уровнем crit/high/med/low и рекомендацией.",
         invocable={"user-invocable": "true"}),
    dict(name="spark-code-line", src="agents/dsh/skills-src/spark-code-line.md",
         sets={"ops", "roles"}, upstream="OPS-CARD.md §3 + agents/orchestrator",
         deps="доступ к борду :8500 (SSH-туннель) и /srv/agents/repos",
         desc="Цикл кодовой линии P4 оркестратора задач DGX Spark: как создать "
              "задачу code-fix/code-review/code-feature, какие артефакты читать у "
              "человеческого гейта и в каком порядке, что делают approve и reject, "
              "кто коммитит. Использовать при работе с бордом задач :8500.",
         invocable={"user-invocable": "true"}),
    dict(name="caveman", src="agents/dsh/skills-src/caveman.md",
         sets={"chat"}, upstream="JuliusBrussee/caveman (рабочий текст правил чата)",
         deps="нет (стиль ответа)",
         desc="Стиль ответов «caveman»: ответ первым, без церемоний и итоговых "
              "резюме, предложения до ~20 слов, команды, пути, числа и тексты "
              "ошибок дословно. Применять только по явному вызову /caveman.",
         invocable={"disable-model-invocation": "true"}),
]

SETS = ("code", "docs", "frontend", "ops", "roles", "chat")


# ── минимальный разбор frontmatter (без PyYAML: на Spark его может не быть) ──
def parse_frontmatter(text: str):
    """(словарь ключей верхнего уровня, тело после frontmatter)."""
    if not text.startswith("---"):
        return {}, text.lstrip("\n")
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text.lstrip("\n")
    fm, body = lines[1:end], "\n".join(lines[end + 1:]).lstrip("\n")
    data, i = {}, 0
    while i < len(fm):
        ln = fm[i]
        km = re.match(r"^([A-Za-z0-9_-]+):(?:\s+(.*))?$", ln)
        if not km:
            i += 1
            continue
        key, val = km.group(1), (km.group(2) or "").strip()
        if val in (">", ">-", "|", "|-", "|+", ">+", ""):
            block, i = [], i + 1
            while i < len(fm) and (fm[i][:1].isspace() or not fm[i].strip()):
                block.append(fm[i].strip())
                i += 1
            sep = "\n" if val.startswith("|") else " "
            data[key] = sep.join(x for x in block if x).strip()
            continue
        data[key] = _unquote(val)
        i += 1
    return data, body


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if v[0] == '"' else inner
    return v


def yq(v) -> str:
    """YAML-скаляр в двойных кавычках (JSON-экранирование совместимо с YAML)."""
    return json.dumps(str(v), ensure_ascii=False)


def fit_desc(d: str):
    """Однострочное description в пределах DESC_MAX (суффикс входит в лимит)."""
    d = " ".join(str(d or "").split())
    if len(d) <= DESC_MAX:
        return d, False
    suffix = " …"
    cut = d[:DESC_MAX - len(suffix)]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + suffix, True


def emit_frontmatter(name, desc, data, extra=None):
    out = ["---", f"name: {yq(name)}", f"description: {yq(desc)}"]
    for k in ("license", "compatibility", "allowed-tools", "whenToUse",
              "disable-model-invocation", "user-invocable"):
        v = (extra or {}).get(k, data.get(k, ""))
        if v in ("", None):
            continue
        if k in ("disable-model-invocation", "user-invocable"):
            out.append(f"{k}: {'true' if str(v).lower() in ('true', 'yes', '1') else 'false'}")
        else:
            out.append(f"{k}: {yq(v)}")
    meta = {"exported-by": "agents/dsh/export-dsh-skills.py",
            "exported-at": datetime.date.today().isoformat()}
    for k, v in sorted(data.items()):
        if k not in DSH_KEYS and str(v).strip():
            meta[f"upstream-{k}"] = v
    for k, v in sorted((extra or {}).get("metadata", {}).items()):
        meta[k] = v
    out.append("metadata:")
    for k, v in sorted(meta.items()):
        out.append(f"  {k}: {yq(v)}")
    out.append("---")
    return "\n".join(out) + "\n"


# ── роль-карточки P4 ────────────────────────────────────────────────────────
def role_text(role: str, roles_dir: str):
    """(текст карточки, источник). Сначала /etc/agents-roles, затем scripts/16."""
    p = Path(roles_dir) / f"{role}.md"
    try:
        if p.is_file():
            txt = "\n".join(ln for ln in p.read_text(encoding="utf-8").split("\n")
                            if not ln.startswith("# имя файла = роль")).strip()
            if txt:
                return txt, str(p)
    except OSError:
        pass
    s16 = PROJ / "scripts" / "16-setup-code-line.sh"
    try:
        raw = s16.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    tag = ROLE_HEREDOC[role]
    m = re.search(rf"<<'{tag}'\n(.*?)\n{tag}\n", raw, re.S)
    if not m:
        return "", ""
    # служебный комментарий карточки («имя файла = роль в реестре») — не текст роли
    card = "\n".join(ln for ln in m.group(1).split("\n")
                     if not ln.startswith("# имя файла = роль")).strip()
    return card, f"scripts/16-setup-code-line.sh ({tag})"


def role_body(role: str, card: str, src: str) -> str:
    return (f"# Роль {role} кодовой линии P4 (оркестратор задач DGX Spark)\n\n"
            "Карточка роли из оркестратора, источник: `" + src + "`.\n\n"
            "В dsh это **скилл-процедура, а не субагент**: dsh создаёт дочерних "
            "агентов инструментами `subagent`/`subagent_fork` и не читает "
            "/etc/agents-roles. Вызов: `/spark-role-" + role + "` и сразу суть "
            "задачи + путь к репозиторию; для изолированного прогона попросите "
            "модель вызвать `subagent` с этим текстом как заданием.\n\n"
            "## Правила роли\n\n" + card + "\n\n"
            "## Границы\n\n"
            "- вывод роли — текст-вердикт, а не молчаливое согласие; без явного "
            "вердикта задача не считается готовой;\n"
            "- read-only роли (reviewer, security) файлы НЕ меняют;\n"
            "- финальное решение за человеком: оркестратор не коммитит и не мержит "
            "после approve (OPS-CARD.md §3.5).\n")


# ── сборка ──────────────────────────────────────────────────────────────────
def collect(args):
    """Отбор скиллов под --set/--only + подготовка тела и frontmatter каждого."""
    picked, warn = [], []
    for s in SKILLS:
        if args.only and s["name"] not in args.only:
            continue
        if not args.only and args.set != "all" and args.set not in s["sets"]:
            continue
        src = s["src"]
        if src.startswith("role:"):
            role = src.split(":", 1)[1]
            card, rsrc = role_text(role, args.roles_dir)
            if not card:
                warn.append(f"{s['name']}: карточка роли {role} не найдена "
                            f"(ни {args.roles_dir}, ни scripts/16) — скилл пропущен")
                continue
            s["_body"], s["_fm"], s["_src_label"], s["_dir"] = role_body(role, card, rsrc), {}, rsrc, None
            picked.append(s)
            continue
        p = (PROJ / src).resolve()
        f = p / "SKILL.md" if p.is_dir() else p
        if not f.is_file():
            warn.append(f"{s['name']}: источник не найден {src} — скилл пропущен")
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError as e:
            warn.append(f"{s['name']}: {f} не читается ({e}) — пропущен")
            continue
        fm, body = parse_frontmatter(raw)
        nested = [x for x in p.rglob("SKILL.md") if x != f] if p.is_dir() else []
        if nested:
            warn.append(f"{s['name']}: вложенные SKILL.md ({len(nested)}) — dsh их не "
                        "обнаруживает (ровно один уровень под корнем)")
        s["_body"], s["_fm"] = body, fm
        s["_src_label"] = str(f.relative_to(PROJ)) if PROJ in f.parents else str(f)
        s["_dir"] = p if p.is_dir() else None
        picked.append(s)
    return picked, warn


def build(args, picked, warn, out: Path):
    sk_dir = out / "skills"
    if out.exists():
        shutil.rmtree(out)
    sk_dir.mkdir(parents=True)
    rows = []
    for s in picked:
        name = s["name"]
        if not NAME_RE.match(name):
            warn.append(f"{name}: имя не подходит под dsh (kebab-case, 1-64) — пропущен")
            continue
        fm = s.get("_fm", {})
        desc = s.get("desc") or fm.get("description") or ""
        if not desc:
            warn.append(f"{name}: нет description ни в источнике, ни в замене — пропущен")
            continue
        desc, truncated = fit_desc(desc)
        if truncated:
            warn.append(f"{name}: description обрезан до {DESC_MAX} символов (лимит dsh)")
        meta = {"upstream-name": fm.get("name", name), "origin": s["upstream"],
                "source-path": s["_src_label"], "sets": ",".join(sorted(s["sets"]))}
        extra = dict(s.get("invocable", {}))
        extra["metadata"] = meta
        text = emit_frontmatter(name, desc, fm, extra) + "\n" + s["_body"].lstrip("\n")
        d = sk_dir / name
        if s.get("_dir"):
            shutil.copytree(s["_dir"], d, symlinks=False,
                            ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
            (d / "SKILL.md").write_text(text, encoding="utf-8")
        else:
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(text, encoding="utf-8")
        rows.append(dict(name=name, sets=sorted(s["sets"]), origin=s["upstream"],
                         source=s["_src_label"], deps=s["deps"], desc_chars=len(desc),
                         files=sum(1 for _ in d.rglob("*") if _.is_file()),
                         bytes=sum(x.stat().st_size for x in d.rglob("*") if x.is_file()),
                         invocable="/" + name,
                         note=s.get("note", "")))
    return rows


INSTALL_SH = """#!/usr/bin/env bash
# Установка экспорта скиллов в корень dsh. По умолчанию — пользовательский
# ~/.agents/skills (ранг 500). Вариант: ./install.sh --project /path/to/repo
# → <repo>/.dsh/skills (ранг 100, ездит вместе с репозиторием).
set -euo pipefail
cd "$(dirname "$0")"
MODE="user"; DIR="${DSH_SKILLS_DIR:-$HOME/.agents/skills}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) MODE="project"; DIR="$2/.dsh/skills"; shift 2 ;;
    --dir)     DIR="$2"; shift 2 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$DIR"
n=0
for d in skills/*/; do
  name="$(basename "$d")"
  rm -rf "$DIR/$name"
  cp -R "$d" "$DIR/$name"
  n=$((n + 1))
done
echo "установлено скиллов: $n → $DIR (режим $MODE)"
echo "корни dsh сканируются без перезапуска; проверка в сессии: /<имя-скилла>"
"""

INSTALL_PS1 = """# Установка экспорта скиллов в корень dsh (Windows).
# По умолчанию — пользовательский $HOME\\.agents\\skills (ранг 500).
# Вариант: .\\install.ps1 -Project C:\\work\\repo → <repo>\\.dsh\\skills (ранг 100).
[CmdletBinding()]
param(
  [string]$Project = "",
  [string]$Dir = ""
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
if ($Dir) { $target = $Dir }
elseif ($Project) { $target = Join-Path $Project ".dsh\\skills" }
else { $target = Join-Path $HOME ".agents\\skills" }
New-Item -ItemType Directory -Force -Path $target | Out-Null
$n = 0
Get-ChildItem -Path "skills" -Directory | ForEach-Object {
  $dst = Join-Path $target $_.Name
  if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
  Copy-Item -Recurse -Force $_.FullName $dst
  $n++
}
Write-Host "установлено скиллов: $n -> $target"
Write-Host "корни dsh сканируются без перезапуска; проверка в сессии: /<имя-скилла>"
"""


def write_manifest(out: Path, rows, warn, args):
    total_b = sum(r["bytes"] for r in rows)
    lines = [
        "# Экспорт скиллов для DeepSeek Harness (dsh)",
        "",
        f"Собрано: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"набор `{args.set}` · скиллов {len(rows)} · {total_b / 1024 / 1024:.1f} МБ",
        f"Источник: `{PROJ}` (роль-карточки: `{args.roles_dir}`)",
        "",
        "## Установка",
        "",
        "```bash",
        "# пользовательский корень ~/.agents/skills (ранг 500)",
        "bash install.sh",
        "# или проектный корень <repo>/.dsh/skills (ранг 100, ездит с репозиторием)",
        "bash install.sh --project /path/to/repo",
        "```",
        "Windows: `.\\install.ps1` либо `.\\install.ps1 -Project C:\\work\\repo`.",
        "",
        "Корни dsh сканируются на лету — перезапуск не нужен. В сессии скилл виден",
        "по имени в каталоге модели; явный вызов — `/<имя>`.",
        "",
        "## Состав",
        "",
        "| скилл (вызов) | наборы | происхождение | локальные зависимости | описание, симв. | файлов |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: x["name"]):
        lines.append(f"| `{r['invocable']}` | {', '.join(r['sets'])} | {r['origin']} "
                     f"| {r['deps']} | {r['desc_chars']} | {r['files']} |")
    lines += [
        "",
        "## Ограничения dsh, которые учтены при экспорте",
        "",
        "- `name` — kebab-case 1-64, `description` — 1-1024 символа; длиннее обрезается",
        "  (факт обрезки — в разделе «Предупреждения»);",
        "- обнаружение ровно на один уровень вглубь корня: вложенные `**/SKILL.md` не видны;",
        "- имена и описания ВСЕХ скиллов попадают в системный промпт, тела — по вызову;",
        "  при >100 записей часть уходит из каталога модели (лимит `maxIndexEntries`);",
        "- скиллы с `disable-model-invocation: true` модель сама не зовёт — только `/имя`;",
        "- зависимости в таблице ставятся на машину, где запущен dsh, а не на Spark.",
        "",
        "## Предупреждения",
        "",
    ]
    lines += [f"- {w}" for w in warn] if warn else ["- нет"]
    lines += ["", "## Проверка после установки", "",
              "```bash",
              "ls ~/.agents/skills | head -30",
              "grep -L '^description:' ~/.agents/skills/*/SKILL.md || echo 'frontmatter у всех есть'",
              "```",
              "В сессии dsh: `/mp-code-review` — тело скилла должно подставиться в шаг.",
              ""]
    (out / "MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "manifest.json").write_text(
        json.dumps(dict(generated=datetime.datetime.now().isoformat(timespec="seconds"),
                        set=args.set, roles_dir=args.roles_dir, skills=rows,
                        warnings=warn), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "install.sh").write_text(INSTALL_SH, encoding="utf-8")
    (out / "install.sh").chmod(0o755)
    (out / "install.ps1").write_text(INSTALL_PS1, encoding="utf-8")


def make_zip(out: Path):
    stamp = datetime.date.today().strftime("%Y%m%d")
    zpath = out.parent / f"dsh-skills-export-{stamp}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(x for x in out.rglob("*") if x.is_file()):
            z.write(f, f"{out.name}/{f.relative_to(out)}")
    h = hashlib.sha256(zpath.read_bytes()).hexdigest()
    return zpath, h


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path.home() / "dsh-skills-export"))
    ap.add_argument("--set", default="all", choices=("all",) + SETS)
    ap.add_argument("--only", default="")
    ap.add_argument("--roles-dir", default="/etc/agents-roles")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()
    args.only = {x.strip() for x in args.only.split(",") if x.strip()}

    out = Path(args.out).expanduser().resolve()
    picked, warn = collect(args)
    rows = build(args, picked, warn, out)
    if not rows:
        print("✗ нечего экспортировать (см. предупреждения)", file=sys.stderr)
        for w in warn:
            print(f"  ! {w}", file=sys.stderr)
        return 1
    write_manifest(out, rows, warn, args)
    print(f"скиллов: {len(rows)} → {out}")
    print(f"байт: {sum(r['bytes'] for r in rows) / 1024 / 1024:.1f} МБ")
    print("состав:")
    for r in sorted(rows, key=lambda x: x["name"]):
        print(f"  /{r['name']:<24} {r['desc_chars']:>4} симв. desc, {r['files']:>3} файлов")
    if warn:
        print("предупреждения:")
        for w in warn:
            print(f"  ! {w}")
    if not args.no_zip:
        zpath, h = make_zip(out)
        print(f"zip: {zpath} ({zpath.stat().st_size / 1024 / 1024:.1f} МБ)")
        print(f"sha256 {h}")
    print("установка: bash install.sh  (или install.ps1 на Windows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
