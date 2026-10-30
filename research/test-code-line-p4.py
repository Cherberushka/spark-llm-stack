#!/usr/bin/env python3
"""
test-code-line-p4.py — тесты кодовой линии P4 БЕЗ Spark, Pi, LiteLLM и Postgres.

Что проверяется:
  1. w_pi (Pi headless worker) на фейковом раннере: промпт, артефакты, diff,
     вердикты ролей, отказ при неработающем Pi, таймаут, выход за AGENTS_REPOS.
  2. run_step: gate-шаг ставит задачу в awaiting_approval; шаг без явного worker
     уходит в Pi, если раннер установлен (и в заглушку SKEL, если нет).
  3. SEED_TEMPLATES/SEED_REGISTRY: шаблоны code-fix/code-review/code-feature
     существуют, роли в них есть в реестре, гейт — последний шаг.
  4. Роль-карточки из scripts/16 совпадают с PI_ROLES (имя файла = роль).
  5. Конфиг Pi под 1.4.0: headless-ключ командой (без env), синтаксис --model
     через слэш, смоук Pi в scripts/16 (иначе «No models available»).
  6. Диф-артефакт гейта (v3.12.5): git зовётся от владельца репозитория
     (оркестратор — root, иначе «dubious ownership» и пустой stdout), база дифа
     = ревизия на старте задачи, .diff пишется ВСЕГДА (пустой диф — тоже факт),
     неотслеживаемые файлы видны в артефакте, репо без коммитов → пустое дерево.
  7. База ЗАДАЧИ, а не шага (v3.12.7): маркер base-rev.txt в артефактах, коммит
     роли не обнуляет диф гейта, мёртвый/чужой маркер пересчитывается; ошибка
     git определяется кодом возврата, а не текстом stderr (локаль ru).

Запуск:  python3 research/test-code-line-p4.py
"""
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
ORCH = PROJ / "agents" / "orchestrator" / "orchestrator.py"
S16 = PROJ / "scripts" / "16-setup-code-line.sh"

FAILS = []
PASSES = [0]


def check(name, cond, extra=""):
    if cond:
        PASSES[0] += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {extra}".strip())
        print(f"  FAIL {name} {extra}")


def load_orch(env_extra, art, repos):
    """Импорт модуля оркестратора с нужными env-путями (fresh module each time)."""
    env = {
        "DATABASE_URL": "postgresql://stub:stub@127.0.0.1:5432/stub",
        "ORCH_API_KEY": "test-orch-key",
        "AGENTS_ART_DIR": str(art),
        "AGENTS_REPOS_DIR": str(repos),
        "AGENTS_PI_RUNNER": str(repos.parent / "agent-run-pi.sh"),
        "AGENTS_PI_TIMEOUT_S": "5",
    }
    env.update(env_extra)
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        if "orch" in sys.modules:
            del sys.modules["orch"]
        spec = importlib.util.spec_from_file_location("orch", ORCH)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


FAKE_RUNNER = r"""#!/usr/bin/env bash
# Фейковый раннер вместо /usr/local/sbin/agent-run-pi.sh (Pi не нужен).
set -uo pipefail
ROLE="$1"; WD="$2"; TF="$3"; OUT="$4"; TO="${5:-60}"
MODE="${FAKE_MODE:-ok}"
printf 'role=%s\nwd=%s\ntaskfile=%s\ntimeout=%s\n---\n' "$ROLE" "$WD" "$TF" "$TO" > "$OUT"
cat "$TF" >> "$OUT"
if [[ "$MODE" == "implement" ]]; then
  printf '\ndef divide(a, b):\n    if b == 0:\n        raise ValueError("division by zero")\n    return a / b\n' >> "$WD/calc.py"
  printf '\ndef test_divide():\n    assert divide(4, 2) == 2\n' >> "$WD/test_calc.py"
fi
case "$MODE" in
  implement) echo "Изменено: calc.py, test_calc.py" >> "$OUT" ;;
  qa-pass)   echo "Вердикт: PASS — 2 теста зелёные" >> "$OUT" ;;
  qa-fail)   echo "Вердикт: FAIL — нет теста на деление на ноль" >> "$OUT" ;;
  review-ok) echo "Вердикт: APPROVE" >> "$OUT" ;;
  review-bad) echo "Вердикт: REQUEST_CHANGES" >> "$OUT" ;;
  sec-clean) echo "замечаний нет; проверено: инъекции, секреты" >> "$OUT" ;;
  sec-crit)  echo "находка: crit — секрет в коде" >> "$OUT" ;;
  rc7)       echo "pi exit=7" >> "$OUT"; exit 7 ;;
  hang)      echo "pi работает..."; sleep 60; exit 0 ;;
  *)         echo "Изменено: calc.py" >> "$OUT" ;;
esac
exit 0
"""


def mkrepo(root, name="demo"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (d / "test_calc.py").write_text("def test_add():\n    assert add(2, 2) == 4\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
    return d


def _git(git_bin, repo, *args):
    return subprocess.run([git_bin, "-C", str(repo), *args], capture_output=True,
                          text=True).stdout


def write_runner(path):
    path.write_text(FAKE_RUNNER, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class FakeCursor:
    def __init__(self):
        self.rows = []
        self.executed = []

    def execute(self, q, a=()):
        self.executed.append((q, a))
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def commit(self):
        pass


def fake_db(cur):
    class _Conn:
        def __enter__(self):
            return cur

        def __exit__(self, *a):
            return False

    return lambda: _Conn()


def task_stub(m, tid=1, payload=None, current_step=0):
    return {"id": tid, "template": "code-fix", "payload": payload or {
        "repo": "demo", "text": "Добавить divide(a,b) с проверкой на ноль"},
        "requester": "test", "grp": "development", "status": "running",
        "current_step": current_step, "token_budget": 200000, "tokens_used": 0,
        "artifacts": []}


def step_stub(m, idx=0, role="implementer", kind="worker", spec=None, sid=1):
    return {"id": sid, "task_id": 1, "idx": idx, "role": role, "kind": kind,
            "spec": spec if spec is not None else {"worker": "pi"},
            "status": "pending", "output": None, "started_at": None, "finished_at": None}


# ══════════════════════════════════════════════════════════════════════════
print("\n[1] w_pi: цепочка implementer → qa → reviewer на фейковом раннере")
tmp = Path(tempfile.mkdtemp(prefix="p4-"))
try:
    art, repos = tmp / "artifacts", tmp / "repos"
    art.mkdir(); repos.mkdir()
    write_runner(tmp / "agent-run-pi.sh")
    repo = mkrepo(repos)
    m = load_orch({}, art, repos)
    t = task_stub(m)

    os.environ["FAKE_MODE"] = "implement"      # фейковый Pi реально правит calc.py
    out, arts = m.w_pi(t, step_stub(m, 0, "implementer"), {"worker": "pi"})
    check("implementer: вердикт ok", "implementer: ok" in out, out)
    check("implementer: OUT-артефакт создан", any(Path(a).name.startswith("OUT-") for a in arts), arts)
    check("implementer: diff-артефакт создан", any(a.endswith(".diff") for a in arts), arts)
    check("implementer: код реально изменён", "divide" in (repo / "calc.py").read_text())
    check("implementer: ветка задачи", subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True,
        text=True).stdout.strip() == "task-1")
    prompt = (art / "task-1" / "TASK-1-0-implementer.md").read_text(encoding="utf-8")
    check("промпт содержит задачу", "divide(a,b)" in prompt)
    check("промпт содержит путь репо", str(repo) in prompt)
    check("промпт НЕ содержит diff для implementer", "GIT DIFF" not in prompt)
    diff_txt = (art / "task-1" / "task-1-implementer.diff").read_text(encoding="utf-8")
    check("diff содержит правку", "+def divide" in diff_txt.replace("++", "+"))
    check("diff-артефакт: шапка с базой задачи", "# база задачи: " in diff_txt, diff_txt[:120])
    check("diff-артефакт: счётчик изменённых файлов", "# изменено файлов: 2" in diff_txt,
          diff_txt[:200])

    os.environ["FAKE_MODE"] = "qa-pass"
    out, arts = m.w_pi(task_stub(m, current_step=1), step_stub(m, 1, "qa", sid=2),
                       {"worker": "pi", "timeout_s": 60})
    check("qa: PASS распознан", "qa: PASS" in out, out)
    qa_prompt = (art / "task-1" / "TASK-1-1-qa.md").read_text(encoding="utf-8")
    check("qa получил контекст цепочки", "КОНТЕКСТ ЦЕПОЧКИ" in qa_prompt and "OUT-1-0-implementer.md" in qa_prompt)

    os.environ["FAKE_MODE"] = "review-ok"
    out, arts = m.w_pi(task_stub(m, current_step=2), step_stub(m, 2, "reviewer", sid=3), {"worker": "pi"})
    check("reviewer: APPROVE распознан", "reviewer: APPROVE" in out, out)
    os.environ.pop("FAKE_MODE", None)
    rev_prompt = (art / "task-1" / "TASK-1-2-reviewer.md").read_text(encoding="utf-8")
    check("reviewer получил GIT DIFF от базы задачи",
          "GIT DIFF от базы задачи" in rev_prompt and "+def divide" in rev_prompt.replace("++", "+"))
    check("промпт reviewer: база = ревизия на старте (не литерал HEAD)",
          "GIT DIFF HEAD" not in rev_prompt)

    # ── [1b] регрессии v3.12.5: git от владельца репо, .diff всегда, untracked ──
    print("\n[1b] Диф-артефакт: untracked, пустой диф, чужой владелец репо, первый коммит")

    # (a) Роль создала НОВЫЙ файл → git diff его не видит; артефакт обязан.
    runner = tmp / "agent-run-pi.sh"
    newfile_block = (
        'if [[ "$MODE" == "newfile" ]]; then' + chr(10)
        + '  { echo "#!/bin/sh"; echo "hardcoded secret=abc"; } > "$WD/deploy.sh"' + chr(10)
        + 'fi' + chr(10))
    runner.write_text(FAKE_RUNNER.replace(
        'if [[ "$MODE" == "implement" ]]; then',
        newfile_block + 'if [[ "$MODE" == "implement" ]]; then'), encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["FAKE_MODE"] = "newfile"
    try:
        out, arts = m.w_pi(task_stub(m, tid=1), step_stub(m, 0, "implementer"), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    d_txt = (art / "task-1" / "task-1-implementer.diff").read_text(encoding="utf-8")
    check("untracked-файл попал в диф-артефакт", "deploy.sh" in d_txt and "НЕОТСЛЕЖИВАЕМЫЕ" in d_txt,
          d_txt[-300:])
    check("untracked: содержимое нового файла видно гейту", "hardcoded secret=abc" in d_txt)
    check("сводка шага считает неотслеживаемые", "неотслеж." in out, out)
    (repo / "deploy.sh").unlink(missing_ok=True)

    # (b) git от root в чужой репозиторий: симуляция «dubious ownership».
    #     shim живёт ТОЛЬКО внутри вызова _git_cmd: иначе он ломает git в самом
    #     тесте (git init) и проверку fallback (shutil.which находит shim).
    shim = tmp / "shim"
    shim.mkdir(exist_ok=True)
    real_git = shutil.which("git")
    git_shim = shim / "git"
    git_shim.write_text(
        "#!/bin/sh" + chr(10)
        + '# симуляция git>=2.35: от root в чужой репо — отказ, stdout пуст' + chr(10)
        + 'if [ "$1" != "-c" ]; then' + chr(10)
        + '  echo "fatal: detected dubious ownership in repository at $2" >&2' + chr(10)
        + "  exit 128" + chr(10)
        + "fi" + chr(10)
        + 'exec "' + real_git + '" "$@"' + chr(10), encoding="utf-8")
    sudo_shim = shim / "sudo"
    sudo_shim.write_text(
        "#!/bin/sh" + chr(10)
        + '# фейковый sudo: -n -u <user> git ... → настоящий git (владелец совпал)' + chr(10)
        + '[ "$1" = "-n" ] && [ "$2" = "-u" ] && shift 4' + chr(10)
        + 'exec "' + real_git + '" "$@"' + chr(10), encoding="utf-8")
    for f in (git_shim, sudo_shim):
        f.chmod(0o755)

    repo2 = mkrepo(repos, "demo2")
    (repo2 / "calc.py").write_text("def add(a, b):" + chr(10) + "    return a + b" + chr(10)
                                  + "# step" + chr(10), encoding="utf-8")
    old_path, old_euid = os.environ["PATH"], m.os.geteuid
    m._GIT_CACHE.clear()
    os.environ["PATH"] = str(shim) + os.pathsep + old_path   # оркестратор — root (юнит без User=)
    m.os.geteuid = lambda: 0
    try:
        cmd = m._git_cmd(repo2)
    finally:
        os.environ["PATH"] = old_path
        m.os.geteuid = old_euid
        # кэш _git_cmd успел запомнить префикс с shim'анным sudo — сбрасываем,
        # иначе следующий вызов полезет за sudo, которого в PATH уже нет
        m._GIT_CACHE.clear()
    check("чужой репо: git зовётся через sudo -u владельца",
          cmd[:4] == ["sudo", "-n", "-u", m.PI_WORKDIR_USER], str(cmd))

    os.environ["FAKE_MODE"] = "ok"
    try:
        out2, arts2 = m.w_pi(task_stub(m, tid=21, payload={"repo": "demo2", "text": "правка"}),
                             step_stub(m, 0, "implementer"), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    d2 = (art / "task-21" / "task-21-implementer.diff").read_text(encoding="utf-8")
    check("чужой репо: диф-артефакт создан и НЕ пуст", "+# step" in d2, d2[:300])
    check("чужой репо: ветка задачи создана (checkout прошёл)",
          "task-21" in _git(real_git, repo2, "branch", "--show-current"))
    check("чужой репо: «Switched to a new branch» не считается ошибкой git",
          "git вернул ошибки" not in d2, d2[:300])

    # sudo недоступен + git отказывает по владельцу → разовый -c safe.directory
    # (в gitconfig ничего не пишется).Probe подменяем: в песочнице владелец репо
    # совпадает с оркестратором, и «dubious ownership» иначе не получить.
    m._GIT_CACHE.clear()
    real_run = m.subprocess.run

    def _no_owner_git(cmd, *a, **kw):
        if cmd[0] == "git" and "-c" not in cmd:  # probe от root в чужой репо
            return subprocess.CompletedProcess(
                cmd, 128, "", "fatal: detected dubious ownership in repository")
        if cmd[0] == "sudo":                     # sudo есть, но не пускает
            return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required")
        return real_run(cmd, *a, **kw)

    m.subprocess.run = _no_owner_git
    try:
        cmd3 = m._git_cmd(repo2)
    finally:
        m.subprocess.run = real_run
        m._GIT_CACHE.clear()
    check("sudo не пускает: fallback -c safe.directory на время вызова",
          cmd3[0] == "git" and "safe.directory=" + str(repo2) in " ".join(cmd3), str(cmd3))

    # sudo вообще не установлен (FileNotFoundError) → тот же fallback, шаг жив
    m._GIT_CACHE.clear()

    def _no_sudo_bin(cmd, *a, **kw):
        if cmd[0] == "sudo":
            raise FileNotFoundError(2, "No such file or directory: 'sudo'")
        return _no_owner_git(cmd, *a, **kw)

    m.subprocess.run = _no_sudo_bin
    try:
        cmd4 = m._git_cmd(repo2)
    finally:
        m.subprocess.run = real_run
        m._GIT_CACHE.clear()
    check("нет бинаря sudo: тот же fallback, без падения шага",
          cmd4[0] == "git" and "safe.directory=" + str(repo2) in " ".join(cmd4), str(cmd4))

    # репо, где на старте задачи нет НИ ОДНОГО коммита: база = пустое дерево,
    # иначе диф первого коммита/новых файлов для гейта просто не существует
    empty = repos / "demo3"
    empty.mkdir()
    subprocess.run([real_git, "init", "-q", str(empty)], check=True)
    (empty / "a.txt").write_text("первый файл" + chr(10), encoding="utf-8")
    check("репо без коммитов: _git_rev(HEAD) пуст", m._git_rev(empty) == "")
    os.environ["FAKE_MODE"] = "ok"
    try:
        m.w_pi(task_stub(m, tid=22, payload={"repo": "demo3", "text": "правка"}),
               step_stub(m, 0, "qa"), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    d3 = (art / "task-22" / "task-22-qa.diff").read_text(encoding="utf-8")
    check("репо без коммитов: база = пустое дерево",
          m.GIT_EMPTY_TREE in d3 and "пустое дерево" in d3, d3[:200])
    check("репо без коммитов: новый файл виден гейту",
          "a.txt" in d3 and "первый файл" in d3, d3[:400])
    m._GIT_CACHE.clear()

    # (c) Роль ничего не изменила → артефакт всё равно есть (гейт не слепой).
    subprocess.run([real_git, "-C", str(repo2), "add", "-A"], check=True)
    subprocess.run([real_git, "-C", str(repo2), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "правка шага"], check=True)   # база = текущий HEAD
    os.environ["FAKE_MODE"] = "review-ok"
    try:
        out3, arts3 = m.w_pi(task_stub(m, tid=23, payload={"repo": "demo2", "text": "только посмотреть"}),
                             step_stub(m, 0, "reviewer"), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    d4p = art / "task-23" / "task-23-reviewer.diff"
    check("пустой диф: артефакт создан", d4p.is_file(), str(arts3))
    d4 = d4p.read_text(encoding="utf-8")
    check("пустой диф: сказано прямо, что изменений нет", "диф пуст" in d4, d4[:200])
    check("пустой диф: счётчик файлов = 0", "# изменено файлов: 0" in d4, d4[:200])

    # ── [1e] регрессии v3.12.7: база задачи переживает коммит роли; git-ошибки по rc ──
    print("\n[1e] База задачи vs шаг: коммит роли не обнуляет диф гейта; ошибки git по коду возврата")

    # (a) Роль внутри задачи сделала КОММИТ. До v3.12.7 база считалась на старте
    #     шага → reviewer видел пустой диф и «коммитов после базы: 0».
    repo4 = mkrepo(repos, "demo4")
    os.environ["FAKE_MODE"] = "implement"
    try:
        m.w_pi(task_stub(m, tid=31, payload={"repo": "demo4", "text": "divide"}),
               step_stub(m, 0, "implementer"), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    base_f = art / "task-31" / "base-rev.txt"
    check("маркер базы задачи создан", base_f.is_file(), str(base_f))
    marker = base_f.read_text(encoding="utf-8").rstrip("\n").split("\t")
    check("маркер: путь репо + полная ревизия",
          marker[0] == str(repo4) and len(marker[1]) == 40, str(marker))
    check("маркер: база = HEAD на старте задачи", marker[1] == m._git_rev(repo4), marker[1][:12])
    subprocess.run([real_git, "-C", str(repo4), "add", "-A"], check=True)
    subprocess.run([real_git, "-C", str(repo4), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "divide от роли"], check=True)
    os.environ["FAKE_MODE"] = "review-ok"
    try:
        m.w_pi(task_stub(m, tid=31, payload={"repo": "demo4", "text": "divide"}),
               step_stub(m, 1, "reviewer", sid=2), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    d5 = (art / "task-31" / "task-31-reviewer.diff").read_text(encoding="utf-8")
    check("коммит роли: база задачи НЕ сдвинулась", f"# база задачи: {marker[1]}" in d5, d5[:250])
    check("коммит роли: диф гейта не пуст", "+def divide" in d5.replace("++", "+"), d5[:400])
    check("коммит роли: коммитов после базы = 1", "# коммитов после базы: 1" in d5, d5[:400])
    check("коммит роли: примечание HEAD != база", "# примечание: HEAD на старте шага" in d5, d5[:400])
    rp4 = next(iter(sorted((art / "task-31").glob("TASK-31-*-reviewer.md")))).read_text(encoding="utf-8")
    check("коммит роли: reviewer получил диф в промпте", "+def divide" in rp4.replace("++", "+"))

    # (b) Маркер с мёртвой ревизией → база пересчитана, шаг не падает
    base_f.write_text(f"{repo4}\t{'0' * 40}\n", encoding="utf-8")
    os.environ["FAKE_MODE"] = "review-ok"
    try:
        m.w_pi(task_stub(m, tid=31, payload={"repo": "demo4", "text": "divide"}),
               step_stub(m, 2, "reviewer", sid=3), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    d6 = (art / "task-31" / "task-31-reviewer.diff").read_text(encoding="utf-8")
    check("мёртвый маркер: база пересчитана + примечание",
          "не найден — база пересчитана" in d6, d6[:300])
    check("мёртвый маркер: новая база записана в маркер",
          base_f.read_text(encoding="utf-8").split("\t")[1].strip() == m._git_rev(repo4),
          base_f.read_text(encoding="utf-8"))

    # (c) Маркер чужого репозитория не наследуется
    base_f.write_text(f"{repo2}\t{m._git_rev(repo2)}\n", encoding="utf-8")
    os.environ["FAKE_MODE"] = "ok"
    try:
        m.w_pi(task_stub(m, tid=31, payload={"repo": "demo4", "text": "divide"}),
               step_stub(m, 3, "qa", sid=4), {"worker": "pi"})
    finally:
        os.environ.pop("FAKE_MODE", None)
    check("маркер чужого репо проигнорирован и перезаписан",
          base_f.read_text(encoding="utf-8").split("\t")[0] == str(repo4),
          base_f.read_text(encoding="utf-8"))

    # (d) Ошибка git определяется кодом возврата, а не лексикой stderr: git на
    #     русской локали пишет «Сброс ветки «task-N»» при успехе (rc=0).
    class _RunStub:
        def __init__(self):
            self.cp = subprocess.CompletedProcess(["git"], 0, "true\n", "Сброс ветки «task-31»\n")

        def __call__(self, cmd, *a, **kw):
            return self.cp

    stub, real_run2 = _RunStub(), m.subprocess.run
    m.subprocess.run = stub
    try:
        o_ok, e_ok = m._git_out(repo4, "checkout", "-B", "task-31")
        stub.cp = subprocess.CompletedProcess(["git"], 128, "", "fatal: bad revision\n")
        o_bad, e_bad = m._git_out(repo4, "diff", "HEAD")
        stub.cp = subprocess.CompletedProcess(["git"], 1, "", "")
        _, e_quiet = m._git_out(repo4, "rev-parse", "--verify", "--quiet", "nope^{commit}")
    finally:
        m.subprocess.run = real_run2
    check("rc=0 + русский stderr: ошибкой не считается", e_ok == "" and o_ok == "true\n", repr(e_ok))
    check("rc=128: stderr и код возврата видны",
          "fatal: bad revision" in e_bad and "[exit 128]" in e_bad, e_bad)
    check("rc=1 без stderr: ошибка не пустая", "stderr пуст" in e_quiet and "[exit 1]" in e_quiet, e_quiet)

    print("\n[2] Вердикты ролей (pi_verdict) — без «зелено по умолчанию»")
    check("qa без вердикта → FAIL", m.pi_verdict("qa", "я посмотрел код").startswith("FAIL"))
    check("qa FAIL → FAIL", m.pi_verdict("qa", "Вердикт: FAIL") == "FAIL")
    check("reviewer без вердикта → не APPROVE", "без явного вердикта" in m.pi_verdict("reviewer", "норм"))
    check("reviewer REQUEST_CHANGES", m.pi_verdict("reviewer", "REQUEST_CHANGES: 1. стиль") == "REQUEST_CHANGES")
    check("security crit → находки", m.pi_verdict("security", "находка crit") == "есть находки crit/high")
    check("security чисто", m.pi_verdict("security", "замечаний нет") == "замечаний нет")
    check("слово 'failed' в логе qa не даёт PASS", m.pi_verdict("qa", "1 test failed").startswith("FAIL"))

    print("\n[3] Отказы: репозиторий, роль, раннер, код возврата, таймаут")
    for name, payload, role, expect in [
        ("нет repo", {"text": "x"}, "implementer", "payload.repo обязателен"),
        ("нет text", {"repo": "demo"}, "implementer", "payload.text обязателен"),
        ("репо не найден", {"repo": "nope", "text": "x"}, "implementer", "репозиторий не найден"),
        ("repo=../evil не выходит за AGENTS_REPOS", {"repo": "../evil", "text": "x"}, "implementer", "репозиторий не найден"),
        ("чужая роль", {"repo": "demo", "text": "x"}, "docs", "не исполняется Pi"),
    ]:
        try:
            m.w_pi(task_stub(m, payload=payload), step_stub(m, role=role), {"worker": "pi"})
            check(name, False, "исключение не поднято")
        except ValueError as e:
            check(name, expect in str(e), str(e))

    os.environ["FAKE_MODE"] = "rc7"
    try:
        m.w_pi(t, step_stub(m), {"worker": "pi"})
        check("Pi rc!=0 → ошибка шага", False)
    except ValueError as e:
        check("Pi rc!=0 → ошибка шага", "кодом 7" in str(e), str(e))
    finally:
        os.environ.pop("FAKE_MODE", None)

    m2 = load_orch({"AGENTS_PI_RUNNER": str(tmp / "no-such-runner.sh")}, art, repos)
    check("_pi_available()=False без раннера", m2._pi_available() is False)
    try:
        m2.w_pi(t, step_stub(m2), {"worker": "pi"})
        check("нет раннера → внятная ошибка", False)
    except ValueError as e:
        check("нет раннера → внятная ошибка", "16-setup-code-line" in str(e), str(e))

    os.environ["FAKE_MODE"] = "hang"
    try:
        m3 = load_orch({"AGENTS_PI_TIMEOUT_S": "1"}, art, repos)
        try:
            m3.w_pi(task_stub(m3, tid=9), step_stub(m3), {"worker": "pi"})
            check("таймаут Pi → ошибка шага", False)
        except ValueError as e:
            check("таймаут Pi → ошибка шага", "таймаут" in str(e), str(e))
    finally:
        os.environ.pop("FAKE_MODE", None)

    print("\n[4] run_step: gate и авто-маршрут роли в Pi")
    cur = FakeCursor()
    m.db = fake_db(cur)
    m.ev = lambda *a, **k: None
    m.registry_roles = lambda: [(r, r) for r in ("implementer", "qa", "reviewer", "security", "planner", "docs")]
    res = m.run_step(cur, task_stub(m), step_stub(m, 3, "reviewer", kind="gate",
                                                 spec={"note": "принять правку"}, sid=9))
    check("gate возвращает pause", res == "pause", res)
    check("gate: шаг → awaiting_approval",
          any("awaiting_approval" in q and a == (9,) for q, a in cur.executed), str(cur.executed)[:300])
    check("gate: задача → awaiting_approval",
          any("update tasks set status='awaiting_approval'" in q for q, _ in cur.executed))

    os.environ["FAKE_MODE"] = "implement"
    cur = FakeCursor()
    m.db = fake_db(cur)
    res = m.run_step(cur, task_stub(m, tid=4), step_stub(m, 0, "implementer", spec={}))
    check("шаг без worker → Pi (auto)", res == "next", res)
    check("шаг без worker → артефакты записаны",
          any("array_cat(artifacts" in q for q, _ in cur.executed))
    check("шаг без worker → OUT-файл есть", bool(list((art / "task-4").glob("OUT-4-*-implementer.md"))))
    os.environ.pop("FAKE_MODE", None)

    m4 = load_orch({"AGENTS_PI_RUNNER": str(tmp / "no-such-runner.sh")}, art, repos)
    cur = FakeCursor()
    m4.db = fake_db(cur)
    m4.ev = lambda *a, **k: None
    res = m4.run_step(cur, task_stub(m4, tid=5), step_stub(m4, 0, "implementer", spec={}))
    check("нет раннера → SKEL-заглушка, шаг не падает", res == "next", res)
    check("SKEL помечен в output шага", any("SKEL" in str(a) for _, a in cur.executed))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════════
print("\n[5] Шаблоны и реестр (SEED_TEMPLATES / SEED_REGISTRY)")
tmp = Path(tempfile.mkdtemp(prefix="p4b-"))
try:
    art, repos = tmp / "artifacts", tmp / "repos"
    art.mkdir(); repos.mkdir()
    m = load_orch({}, art, repos)
    tpl = {t[0]: t for t in m.SEED_TEMPLATES}
    reg = {r[0] for r in m.SEED_REGISTRY}
    for name in ("code-fix", "code-review", "code-feature"):
        check(f"шаблон {name} есть", name in tpl)
        if name in tpl:
            groups, steps = tpl[name][2], tpl[name][3]
            check(f"{name}: группа development", "development" in groups, str(groups))
            check(f"{name}: роли есть в реестре", all(s["role"] in reg for s in steps),
                  str([s["role"] for s in steps]))
            check(f"{name}: гейт последний", steps[-1]["kind"] == "gate", str(steps[-1]))
            check(f"{name}: pi-worker у рабочих шагов",
                  all(s["spec"].get("worker") == "pi" for s in steps
                      if s["kind"] == "worker" and not s["spec"].get("plan")),
                  str(steps))
    cf = [s["role"] for s in tpl["code-fix"][3]]
    check("code-fix: implementer → qa → reviewer → gate",
          cf == ["implementer", "qa", "reviewer", "reviewer"], str(cf))
    check("code-feature начинается с planner", tpl["code-feature"][3][0]["spec"].get("plan") is True)
    check("WORKERS содержит pi", "pi" in m.WORKERS)
    check("PI_ROLES = implementer/qa/reviewer/security",
          set(m.PI_ROLES) == {"implementer", "qa", "reviewer", "security"}, str(m.PI_ROLES))
    old_tpl = {t[0] for t in m.SEED_TEMPLATES}
    check("старые шаблоны не потеряны",
          {"echo", "doc-convert", "doc-qa", "feature", "db-report"} <= old_tpl, str(old_tpl))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n[6] scripts/16: карточки ролей = PI_ROLES, смоук защищён")
s16 = S16.read_text(encoding="utf-8")
cards = set(re.findall(r"/etc/agents-roles/([a-z-]+)\.md", s16))
check("карточки ролей совпадают с PI_ROLES", cards == {"implementer", "qa", "reviewer", "security"}, str(cards))
check("смоук проверяет активность оркестратора", "systemctl is-active --quiet agents-orchestrator" in s16)
check("смоук проверяет наличие шаблона code-fix", 'grep -q \'"code-fix"\'' in s16)
check("смоук валидирует TID", '[[ "$TID" =~ ^[0-9]+$ ]]' in s16)
check("раннер ищет бинарь pi явно (не полагается на PATH)",
      '/home/agent-dev/.pi/agent/bin/pi' in s16 and '"$PI_BIN" --model spark/qwen3.8-27b' in s16)
check("раннер отдаёт 127 и внятный текст, если pi не найден",
      'pi не найден' in s16 and 'exit 127' in s16)
check("демо-репо создаётся до смоука", "/srv/agents/repos/demo" in s16)
# Регрессия: шаг [2/7] не должен переустанавливать Pi на каждом запуске.
# `command -v pi` в неlogin-шелле не видит бинарь install.sh (PATH правится в
# ~/.bashrc) → скрипт лез в сеть за `curl | sh` и тянул latest.
check("шаг 2 ищет бинарь pi по путям, а не через command -v в неlogin-шелле",
      "PI_EXIST=" in s16 and
      "sudo -u agent-dev bash -c 'command -v pi >/dev/null 2>&1 || curl" not in s16)
check("шаг 2 ставит Pi только когда бинаря нет",
      'echo "  pi уже установлен: $PI_EXIST' in s16)
check("нет сети до pi.dev → внятная ошибка и инструкция офлайн-установки",
      "OFFLINE_PI" in s16 and "npm pack @earendil-works/pi-coding-agent" in s16)

print("\n[7] Конфиг Pi 1.4.0: headless-ключ и синтаксис --model")
MH = PROJ / "agents" / "pi" / "models.headless.json"
MW = PROJ / "agents" / "pi" / "models.json"
check("есть headless-шаблон models.headless.json", MH.exists())
if MH.exists():
    import json as _json
    try:
        _h = _json.loads(MH.read_text(encoding="utf-8"))["providers"]["spark"]
    except Exception as _e:            # невалидный JSON = Pi молча проигнорирует провайдер
        _h = {}
        check("models.headless.json — валидный JSON", False, str(_e))
    else:
        check("models.headless.json — валидный JSON", True)
    check("headless: ключ командой из файла (не зависит от env)",
          str(_h.get("apiKey", "")).startswith("!cat "))
    check("headless: api = openai-completions", _h.get("api") == "openai-completions")
    check("headless: baseUrl содержит порт шлюза 4000 и <SPARK_IP>",
          ":4000/v1" in str(_h.get("baseUrl")) and "<SPARK_IP>" in str(_h.get("baseUrl")))
    check("headless: модель qwen3.8-27b объявлена",
          [m.get("id") for m in _h.get("models", [])] == ["qwen3.8-27b"])
_w = _json.loads(MW.read_text(encoding="utf-8"))["providers"]["spark"] if MW.exists() else {}
check("шаблон для рабочих станций по-прежнему на $SPARK_LLM_KEY (export в профиль)",
      _w.get("apiKey") == "$SPARK_LLM_KEY")
check("scripts/16 ставит headless-шаблон (не станционный)", "models.headless.json" in s16)
check("scripts/16 подставляет путь к файлу ключа", "s|<KEY_FILE>|" in s16)
check("scripts/16 валидирует JSON перед запуском Pi",
      "json.load(open(sys.argv[1]))" in s16)
check("scripts/16 падает на пустом CODE_WORKER_KEY",
      "CODE_WORKER_KEY в $ENVF пуст" in s16 and '[[ -s "$KEYFILE" ]]' in s16)
check("scripts/16 падает, если LiteLLM не выдал ключ", "LiteLLM не выдал ключ" in s16)
check("смоук Pi: --list-models spark", '--list-models spark' in s16)
check("смоук Pi: эхо-запрос под env -i (без SPARK_LLM_KEY)",
      "env -i HOME=" in s16 and "PONG" in s16)
check("нигде не осталось синтаксиса Pi 0.10 (--model spark:...)",
      "--model spark:" not in s16)
check("раннер проверяет пустой ключ воркера (exit 126)",
      "pi exit=126" in s16)
check("раннер ищет pi и в managed-установке (install/releases/*/bin/pi)",
      "install/releases/*/bin/pi" in s16)

print(f"\n{'=' * 70}\nИТОГ: {PASSES[0]} проверка(ок) пройдено, {len(FAILS)} провалов")
for f in FAILS:
    print("  ✗ " + f)
sys.exit(1 if FAILS else 0)
