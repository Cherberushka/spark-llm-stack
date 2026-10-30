#!/usr/bin/env bash
# =============================================================================
# 10-install-pi-skills.sh — curated agent skills for Pi (token-hygiene edition)
#
# Usage:
#   bash scripts/10-install-pi-skills.sh            # user-level (default)
#   sudo -E bash scripts/10-install-pi-skills.sh    # for the invoking user's ~/.pi
#   PI_SKILLS_DIR=/srv/pi-shared/skills bash ...    # shared dir (server Pi)
#   OFFLINE=1 bash ...                              # only vendored copies, no network
#   FULL=1 bash ...                                 # install ALL mattpocock skills (needs network)
#
# Sources: git clone (if network available) with automatic fallback to the
# vendored offline copies in ../vendor-skills/ (shipped inside the installer
# zip). Network audit of every curated skill: see SKILLS-APPLIED.md §3.
#
# Why curated: every skill's description lands in the agent's system prompt on
# every request. 8 hand-picked skills ≈ 1 KB of index; the full repo ≈ 5-6 KB
# plus decision noise. Review quarterly (TUNING-GUIDE step 6).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$SCRIPT_DIR/../vendor-skills"

if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" && -z "${PI_SKILLS_DIR:-}" ]]; then
  TARGET_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
  TARGET_HOME="$HOME"
fi
DEST="${PI_SKILLS_DIR:-$TARGET_HOME/.pi/agent/skills}"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# fetch <url> <tmp-name> <vendor-subpath>: clone if allowed, else/fallback vendor
fetch() {
  local url="$1" name="$2" vpath="${3:-}"
  if [[ "${OFFLINE:-0}" != "1" ]] && git clone --depth 1 "$url" "$TMP/$name" >/dev/null 2>&1; then
    echo "  ~ $name: git clone OK"
  elif [[ -n "$vpath" && -d "$VENDOR/$vpath" ]]; then
    cp -r "$VENDOR/$vpath" "$TMP/$name"
    echo "  ~ $name: vendored offline copy ($( [[ "${OFFLINE:-0}" == "1" ]] && echo "OFFLINE=1" || echo "clone failed" ))"
  else
    echo "  ! $name: no network and no vendor copy — skipped" >&2
    return 1
  fi
}

echo "=== [1/3] Fetch skill repos (OFFLINE=${OFFLINE:-0}) ==="
fetch https://github.com/mattpocock/skills.git     mattpocock mattpocock || true
fetch https://github.com/leonxlnx/taste-skill.git  taste      taste-skill || true
fetch https://github.com/leonxlnx/unlazy.git       unlazy     unlazy || true
fetch https://github.com/anthropics/skills.git     anthropics anthropics || true

# Vendor layout is flat (curated dirs only); repo layout is nested. Normalise
# both into $TMP/src/{mattpocock-engineering,mattpocock-productivity,taste,unlazy,anthropics}.
mkdir -p "$TMP/src"
if [[ -d "$TMP/mattpocock" ]]; then
  if [[ -d "$TMP/mattpocock/skills/engineering" ]]; then
    ln -s "$TMP/mattpocock/skills/engineering" "$TMP/src/mp-eng"
    ln -s "$TMP/mattpocock/skills/productivity" "$TMP/src/mp-prod"
  else
    ln -s "$TMP/mattpocock" "$TMP/src/mp-eng"; ln -s "$TMP/mattpocock" "$TMP/src/mp-prod"
  fi
fi
[[ -d "$TMP/taste/skills/taste-skill" ]] && ln -s "$TMP/taste/skills/taste-skill" "$TMP/src/taste"
[[ -d "$TMP/taste" && ! -e "$TMP/src/taste" ]] && ln -s "$TMP/taste" "$TMP/src/taste"
if [[ -d "$TMP/unlazy/skills/unlazy" ]]; then ln -s "$TMP/unlazy/skills/unlazy" "$TMP/src/unlazy"
elif [[ -f "$TMP/unlazy/SKILL.md" ]]; then ln -s "$TMP/unlazy" "$TMP/src/unlazy"; fi
if [[ -d "$TMP/anthropics/skills" ]]; then ln -s "$TMP/anthropics/skills" "$TMP/src/anthropics"
elif [[ -d "$TMP/anthropics" ]]; then ln -s "$TMP/anthropics" "$TMP/src/anthropics"; fi

echo "=== [2/3] Install into $DEST ==="
copy() { # src destname
  if [[ -d "$1" || -f "$1/SKILL.md" ]]; then rm -rf "$DEST/$2"; cp -rL "$1" "$DEST/$2"; rm -rf "$DEST/$2/.git"; echo "  + $2"; else echo "  ! missing: $1"; fi
}

if [[ "${FULL:-0}" == "1" ]]; then
  # FULL needs the complete repo → network (vendor carries only the curated set)
  for d in "$TMP"/mattpocock/skills/engineering/*/ "$TMP"/mattpocock/skills/productivity/*/; do
    [[ -d "$d" ]] && copy "$d" "mp-$(basename "$d")"
  done
else
  # curated: code quality + diagnosis + spec discipline + handoff + docs + research
  copy "$TMP/src/mp-eng/code-review"        mp-code-review
  copy "$TMP/src/mp-eng/diagnosing-bugs"    mp-diagnosing-bugs
  copy "$TMP/src/mp-eng/tdd"                mp-tdd
  copy "$TMP/src/mp-eng/research"           mp-research
  copy "$TMP/src/mp-eng/to-spec"            mp-to-spec
  copy "$TMP/src/mp-eng/implement"          mp-implement
  copy "$TMP/src/mp-prod/handoff"           mp-handoff
  copy "$TMP/src/mp-prod/writing-for-agents" mp-writing-for-agents
  copy "$TMP/src/mp-eng/wizard"             mp-wizard
fi
# taste-skill: frontend/design taste (needs Node 22+ & local Chrome; assets.mjs needs internet — see SKILLS-APPLIED §3)
[[ -e "$TMP/src/taste" ]] && copy "$TMP/src/taste" taste-skill
# unlazy: gate-чеклисты «как выглядит готово» (та же философия вшита в qa-роль оркестратора)
[[ -e "$TMP/src/unlazy" ]] && copy "$TMP/src/unlazy" unlazy
# anthropics document-skills: xlsx/docx/pdf/pptx — руки Pi для офисных форматов
for d in xlsx docx pdf pptx doc-coauthoring; do
  [[ -d "$TMP/src/anthropics/$d" ]] && copy "$TMP/src/anthropics/$d" "ant-$d"
done
# find-skills (vercel-labs): мета-скилл самообнаружения из реестра skills.sh.
# Требует ИНТЕРНЕТ в момент использования (npx skills find). Ставьте только на
# станции с выходом в сеть: FIND_SKILLS=1 bash scripts/10-install-pi-skills.sh
if [[ "${FIND_SKILLS:-0}" == "1" && "${OFFLINE:-0}" != "1" ]]; then
  git clone --depth 1 https://github.com/vercel-labs/skills.git "$TMP/vskills" >/dev/null 2>&1 || true
  copy "$TMP/vskills/skills/find-skills" find-skills
fi

echo "=== [3/3] Installed ==="
ls -1 "$DEST"
cat <<'EOF'

Verify in Pi: start a session and ask "какие скиллы тебе доступны?" —
the model reads the skills index; or check slash-commands (/skills, build-dependent).

Network requirements (full audit: SKILLS-APPLIED.md §3):
  offline at runtime : mp-* (9), unlazy, ant-xlsx/docx/pdf/pptx/doc-coauthoring,
                       taste-skill core (draw/lint/inspect/scenes — local Chrome)
  needs internet     : find-skills (npx skills find), taste-skill assets.mjs
                       (Openverse/Wikimedia/NASA photo search — optional feature)
  local deps only    : ant-* need LibreOffice + python/node libs (fallback
                       pip/npm install in their SKILL.md needs network or mirror)

Hygiene rule (TUNING-GUIDE step 6): quarterly, delete skills nobody invoked
(Pi session history shows usage); re-run this script to refresh the rest.
EOF
