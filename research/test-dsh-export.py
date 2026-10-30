#!/usr/bin/env python3
"""
test-dsh-export.py — тесты экспорта скиллов в DeepSeek Harness (dsh) БЕЗ dsh и сети.

Что проверяется:
  1. Экспорт собирается: 22 скилла (16 курируемых vendor + 4 роль-карточки P4 +
     spark-code-line + caveman), у каждого name == имя каталога, description
     1-1024 символа, тело непустое, frontmatter — валидный YAML (если PyYAML есть).
  2. dsh-ограничения: ровно один уровень SKILL.md под корнем, необязательные ключи
     из списка dsh, disable-model-invocation/user-invocable — булевы.
  3. Роль-карточки: текст из /etc/agents-roles (fallback scripts/16) перенесён,
     служебный комментарий «имя файла = роль» не утёк в скилл.
  4. Наборы: --set code = 11 скиллов, --only работает, неизвестный источник →
     предупреждение, а не падение.
  5. install.sh ставит в ~/.agents/skills и в <repo>/.dsh/skills (--project).
  6. Обрезка description >1024 и zip+sha256.

Запуск:  python3 research/test-dsh-export.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
EXPORTER = PROJ / "agents" / "dsh" / "export-dsh-skills.py"

FAILS = []
PASSES = [0]

try:
    import yaml
except ImportError:
    yaml = None


def check(name, cond, extra=""):
    if cond:
        PASSES[0] += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"  FAIL {name} {extra}")


def run_export(out, *extra):
    cmd = [sys.executable, str(EXPORTER), "--out", str(out),
           "--roles-dir", str(PROJ / "no-such-roles"), *extra]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJ))
    return p


def fm_of(skill_md: Path):
    """(frontmatter-текст, тело) без PyYAML."""
    t = skill_md.read_text(encoding="utf-8")
    assert t.startswith("---\n"), str(skill_md)
    end = t.index("\n---\n", 4)
    return t[4:end], t[end + 5:]


tmp = Path(tempfile.mkdtemp(prefix="dsh-exp-"))
try:
    print("\n[1] Полный экспорт (набор all)")
    out = tmp / "all"
    p = run_export(out, "--set", "all")
    check("экспорт завершился успешно", p.returncode == 0, p.stdout[-600:] + p.stderr[-600:])
    skills = sorted(x.name for x in (out / "skills").iterdir() if x.is_dir())
    check("скиллов 22", len(skills) == 22, str(len(skills)) + " " + str(skills))
    expected = {"mp-code-review", "mp-diagnosing-bugs", "mp-tdd", "mp-implement",
                "mp-to-spec", "mp-research", "mp-handoff", "mp-writing-for-agents",
                "mp-wizard", "ant-xlsx", "ant-docx", "ant-pptx", "ant-pdf",
                "ant-doc-coauthoring", "unlazy", "taste-skill",
                "spark-role-implementer", "spark-role-qa", "spark-role-reviewer",
                "spark-role-security", "spark-code-line", "caveman"}
    check("состав = 16 vendor + 4 роли + процесс + стиль", set(skills) == expected,
          str(set(skills) ^ expected))

    bad = []
    for name in skills:
        f = out / "skills" / name / "SKILL.md"
        fm, body = fm_of(f)
        m_name = re.search(r'^name: "(.+)"$', fm, re.M)
        m_desc = re.search(r'^description: "(.+)"$', fm, re.M)
        if not m_name or m_name.group(1) != name:
            bad.append(f"{name}: name != каталог ({m_name.group(1) if m_name else None})")
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", name):
            bad.append(f"{name}: не kebab-case")
        if not m_desc:
            bad.append(f"{name}: нет description")
        else:
            d = json.loads('"' + m_desc.group(1) + '"')
            if not 1 <= len(d) <= 1024:
                bad.append(f"{name}: description {len(d)} символов (лимит 1-1024)")
        if not body.strip():
            bad.append(f"{name}: пустое тело")
    check("frontmatter/имя/description/тело корректны у всех", not bad, "; ".join(bad))

    if yaml:
        ybad = []
        for name in skills:
            fm, _ = fm_of(out / "skills" / name / "SKILL.md")
            try:
                data = yaml.safe_load(fm)
            except Exception as e:
                ybad.append(f"{name}: {e}")
                continue
            allowed = {"name", "description", "license", "compatibility", "allowed-tools",
                       "whenToUse", "disable-model-invocation", "user-invocable", "metadata"}
            extra_keys = set(data) - allowed
            if extra_keys:
                ybad.append(f"{name}: ключи вне списка dsh {extra_keys}")
            for k in ("disable-model-invocation", "user-invocable"):
                if k in data and not isinstance(data[k], bool):
                    ybad.append(f"{name}: {k} не boolean")
            if not isinstance(data.get("metadata"), dict):
                ybad.append(f"{name}: metadata не словарь")
        check("YAML валиден, ключи только из списка dsh, флаги булевы", not ybad, "; ".join(ybad))
    else:
        print("  --   PyYAML не установлен: YAML-проверка пропущена")

    nested = [x for x in (out / "skills").rglob("SKILL.md")
              if len(x.relative_to(out / "skills").parts) != 2]
    check("нет вложенных SKILL.md (dsh видит ровно один уровень)", not nested, str(nested[:3]))

    man = (out / "MANIFEST.md").read_text(encoding="utf-8")
    check("MANIFEST.md: таблица состава и вызовы /имя", "| `/mp-tdd`" in man and "## Установка" in man)
    mjson = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    check("manifest.json: 22 записи", len(mjson["skills"]) == 22, str(len(mjson["skills"])))

    print("\n[2] Роль-карточки P4")
    sec = (out / "skills" / "spark-role-security" / "SKILL.md").read_text(encoding="utf-8")
    check("карточка security перенесена (инъекции, секреты, CVE)",
          "инъекции" in sec and "CVE" in sec)
    check("служебный комментарий «имя файла = роль» не утёк", "имя файла = роль" not in sec)
    check("объяснено: в dsh это скилл, а не субагент", "не субагент" in sec and "subagent" in sec)
    qa = (out / "skills" / "spark-role-qa" / "SKILL.md").read_text(encoding="utf-8")
    check("карточка qa: gate «как выглядит готово» (unlazy) на месте",
          "как выглядит готово" in qa and "PASS/FAIL" in qa)
    imp = (out / "skills" / "spark-role-implementer" / "SKILL.md").read_text(encoding="utf-8")
    check("карточка implementer: принципы Karpathy перенесены", "Принципы Karpathy" in imp)
    check("роль-скиллы вызываются пользователем (user-invocable: true)",
          all(re.search(r"^user-invocable: true$",
                        fm_of(out / "skills" / f"spark-role-{r}" / "SKILL.md")[0], re.M)
              for r in ("implementer", "qa", "reviewer", "security")))

    print("\n[3] Наборы и --only")
    out2 = tmp / "code"
    p2 = run_export(out2, "--set", "code")
    code = sorted(x.name for x in (out2 / "skills").iterdir())
    check("--set code = 11 скиллов", p2.returncode == 0 and len(code) == 11, str(code))
    check("--set code: документные скиллы не попали", "ant-xlsx" not in code and "taste-skill" not in code)
    check("--set code: роли и unlazy на месте",
          {"unlazy", "mp-tdd", "spark-role-qa"} <= set(code), str(code))
    out3 = tmp / "only"
    p3 = run_export(out3, "--only", "unlazy,caveman")
    check("--only выбирает ровно указанное",
          sorted(x.name for x in (out3 / "skills").iterdir()) == ["caveman", "unlazy"])
    cav = fm_of(out3 / "skills" / "caveman" / "SKILL.md")[0]
    check("caveman: disable-model-invocation: true (только /caveman)",
          re.search(r"^disable-model-invocation: true$", cav, re.M) is not None, cav)

    print("\n[4] Устойчивость: битый источник, обрезка description, zip")
    broken = tmp / "broken"
    p4 = subprocess.run([sys.executable, str(EXPORTER), "--out", str(broken), "--no-zip",
                         "--only", "нет-такого-скилла"], capture_output=True, text=True, cwd=str(PROJ))
    check("пустой выбор → код 1 и внятная причина, не трейсбек",
          p4.returncode == 1 and "нечего экспортировать" in (p4.stdout + p4.stderr),
          (p4.stdout + p4.stderr)[-300:])
    sys.path.insert(0, str(EXPORTER.parent))
    g = {"__name__": "not_main", "__file__": str(EXPORTER)}
    exec(compile(EXPORTER.read_text(encoding="utf-8"), str(EXPORTER), "exec"), g)
    fit = g["fit_desc"]
    d, cut = fit("x" * 2000)
    check("description >1024 обрезается до лимита", cut and len(d) <= 1024, str(len(d)))
    dw, cutw = fit("слово " * 400)
    check("обрезка по границе слова, суффикс входит в лимит",
          cutw and len(dw) <= 1024 and dw.endswith("…") and "слово" in dw, f"{len(dw)} {dw[-24:]!r}")
    d2, cut2 = fit("короткое описание")
    check("короткое description не трогается", not cut2 and d2 == "короткое описание")
    zipped = tmp / "zip"
    pz = run_export(zipped, "--set", "roles")
    zips = sorted(tmp.glob("dsh-skills-export-*.zip"))
    check("zip создан рядом с каталогом", pz.returncode == 0 and zips, str(pz.stdout[-200:]))
    if zips:
        import hashlib
        import zipfile
        h = hashlib.sha256(zips[-1].read_bytes()).hexdigest()
        check("sha256 напечатан и совпадает", h in pz.stdout, h[:16])
        names = zipfile.ZipFile(zips[-1]).namelist()
        check("в zip есть skills/, MANIFEST.md и install.sh",
              any(n.endswith("MANIFEST.md") for n in names)
              and any(n.endswith("install.sh") for n in names)
              and any("/skills/" in n for n in names), str(names[:5]))

    print("\n[5] install.sh: пользовательский и проектный корни")
    fakehome = tmp / "home"
    fakehome.mkdir()
    env = dict(os.environ, HOME=str(fakehome))
    r = subprocess.run(["bash", "install.sh"], cwd=str(zipped), capture_output=True, text=True, env=env)
    got = sorted(x.name for x in (fakehome / ".agents" / "skills").iterdir()) if r.returncode == 0 else []
    check("install.sh → ~/.agents/skills (ранг 500)",
          r.returncode == 0 and got == sorted(x.name for x in (zipped / "skills").iterdir()),
          r.stdout + r.stderr + str(got))
    proj_repo = tmp / "repo"
    (proj_repo / ".git").mkdir(parents=True)
    r2 = subprocess.run(["bash", "install.sh", "--project", str(proj_repo)], cwd=str(zipped),
                        capture_output=True, text=True)
    check("install.sh --project → <repo>/.dsh/skills (ранг 100)",
          r2.returncode == 0 and (proj_repo / ".dsh" / "skills").is_dir()
          and any((proj_repo / ".dsh" / "skills").iterdir()), r2.stdout + r2.stderr)
    r3 = subprocess.run(["bash", "install.sh", "--project", str(proj_repo)], cwd=str(zipped),
                        capture_output=True, text=True)
    check("повторная установка идемпотентна", r3.returncode == 0, r3.stdout + r3.stderr)

    print("\n[6] Обвязка проекта")
    s21 = (PROJ / "scripts" / "21-export-dsh-skills.sh").read_text(encoding="utf-8")
    check("scripts/21 запускает экспортер и сам находит корень проекта",
          'cd "$(dirname "$0")/.."' in s21 and "export-dsh-skills.py" in s21)
    check("scripts/21: shebang bash (запуск `bash scripts/21…`: unzip не хранит бит +x)",
          s21.split("\n", 1)[0] == "#!/usr/bin/env bash", s21[:40])
    check("bash -n scripts/21", subprocess.run(["bash", "-n", str(PROJ / "scripts" / "21-export-dsh-skills.sh")],
                                              capture_output=True).returncode == 0)
    guide = (PROJ / "agents" / "dsh" / "DEEPSEEK-HARNESS-GUIDE.md").read_text(encoding="utf-8")
    check("гайд dsh содержит раздел про скиллы (§7)", "## 7." in guide and "skills" in guide)
    inv = (PROJ / "SKILLS-INVENTORY.md").read_text(encoding="utf-8")
    check("SKILLS-INVENTORY.md: все 22 имени экспорта перечислены",
          all(f"`{n}`" in inv for n in sorted(
              json.loads((out / "manifest.json").read_text(encoding="utf-8"))["skills"][i]["name"]
              for i in range(22))), "")
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 70)
print(f"ИТОГ: {PASSES[0]} проверка(ок) пройдено, {len(FAILS)} провалов")
for f in FAILS:
    print("  ✗", f)
sys.exit(1 if FAILS else 0)
