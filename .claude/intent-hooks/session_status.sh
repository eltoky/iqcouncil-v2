#!/usr/bin/env bash
# SessionStart hook: print intent-flow status into session context. Read-only, fast.
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
# PARSED, never sourced. `source` on a committed, repository-controlled file runs whatever that
# file contains — as the user, at session start, before they type anything — and `2>/dev/null ||
# true` hid it completely. Every other config in this suite is read with a grep; this one file was
# the exception. The SAME reader as commit_guard.sh, character for character (an invariant compares
# them): this copy did not strip quotes, so `ENFORCE_LEVEL="warn"` read as `"warn"` here — matching
# neither value — while the guard read `warn`.
CFG=.claude/intent-hooks/intent-guard.config
_cfg(){ v=$(grep -m1 "^$1=" "$CFG" 2>/dev/null \
            | sed 's/^[^=]*=//; s/[[:space:]]*#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')
        echo "${v:-$2}"; }
# One default, `block`, matching commit_guard.sh and the skill. This said `warn`, so a config
# missing the key was reported as ADVISORY while the guard actually blocked (contradiction M14).
ENFORCE_LEVEL=$(_cfg ENFORCE_LEVEL block)
R="${REGISTER:-docs/intent/INTENT_REGISTER.md}"; T="${TRACE:-docs/intent/INTENT_TRACE.yaml}"
COUNTER=".claude/intent-hooks/.ignored_warnings"

# ---- release drift: this repo's hooks and workflows are COPIES of the machine's suite ----
# Checked BEFORE the register early-exit: drift does not depend on a register existing, and the
# failure mode is silent — new skills on the machine, old hooks and old acceptance workflow in the
# repo, with nothing saying so until a gate behaves oddly.
REPO_VER=$(sed -n 's/^version=//p' docs/intent/.suite-version 2>/dev/null | head -1)
MACH_VER=$(sed -n 's/^version=//p' "$HOME/.claude/skills/.intent-suite-version" 2>/dev/null | head -1)
if [ -n "${MACH_VER:-}" ] && [ "${REPO_VER:-none}" != "$MACH_VER" ]; then
  echo "[intent-flow] RELEASE DRIFT: repo armed by ${REPO_VER:-pre-1.1.0/unknown}, machine has $MACH_VER."
  echo "[intent-flow] >>> The repo's hooks and .github/workflows are copies from the older release. Refresh them:"
  echo "[intent-flow]     bash ~/.claude/skills/intent-init/scripts/install_runtime.sh     # keeps your governance.yaml"
  echo "[intent-flow]     then run intent-init to refresh the CLAUDE.md block and migrate artifacts"
fi

# ---- Mem0 rule drift: the rules that steer the agent mid-task may be older than the suite ----
# A stale memory rule is the worst kind of staleness here, because it is invisible: every file on
# disk says one thing and the agent's recalled rule says another, and memory usually wins mid-task.
# Compared by digest of the rule TEXTS, so a release that did not change any rule stays quiet.
EMIT="$HOME/.claude/skills/intent-init/scripts/mem0_emit_rules.py"
if [ -f "$EMIT" ] && [ -f docs/intent/.mem0-sync ]; then
  WANT=$(python3 "$EMIT" --format digest 2>/dev/null | sed -n 's/^digest=\([0-9a-f]*\).*/\1/p')
  HAVE=$(sed -n 's/^digest=//p' docs/intent/.mem0-sync 2>/dev/null | head -1)
  if [ -n "${WANT:-}" ] && [ -n "${HAVE:-}" ] && [ "$WANT" != "$HAVE" ]; then
    echo "[intent-flow] MEM0 RULE DRIFT: synced rules ($HAVE) differ from the installed table ($WANT)."
    echo "[intent-flow] >>> A memory rule whose text changed still steers this session. Re-sync:"
    echo "[intent-flow]     run intent-init  (the procedure: python3 $EMIT --format plan --project <scope> --root .)"
  fi
fi
if [ ! -f "$R" ]; then echo "[intent-flow] No $R in this repo. Run intent-kickoff (greenfield) or intent-register-builder (existing docs)."; exit 0; fi
RV=$(grep -m1 -oE 'Version: *[0-9]+' "$R" | grep -oE '[0-9]+')
OQ=$(awk '/^## Open Questions/{f=1;next}/^## /{f=0}f&&/^[0-9]+\./{c++}END{print c+0}' "$R")
echo "[intent-flow] Register v${RV:-?} — Open Questions: $OQ"
if [ -f "$T" ]; then
  TV=$(grep -m1 -oE 'register_version: *[0-9]+' "$T" | grep -oE '[0-9]+')
  if [ "$TV" != "$RV" ]; then echo "[intent-flow] STALE trace map: built for register v${TV:-?}, register is v${RV:-?}. Run intent-trace-map refresh THIS SESSION."; fi
else
  echo "[intent-flow] No $T — diff-scoped PR reviews will be slow. Run intent-trace-map."
fi
if [ "${ENFORCE_LEVEL:-block}" = "warn" ]; then
  IGN=$(tr -dc "0-9" < "$COUNTER" 2>/dev/null | head -c 9); IGN=${IGN:-0}
  echo "[intent-flow] ENFORCE_LEVEL=warn — guards are ADVISORY. Ignored warnings so far: $IGN."
  if [ "$IGN" -gt 0 ]; then
    echo "[intent-flow] >>> $IGN guard warnings were bypassed. Warn mode is for calibration only — flip ENFORCE_LEVEL=block in .claude/intent-hooks/intent-guard.config, or resolve the outstanding register/trace gaps now."
  fi
fi

# The loop's own armed state, computed rather than assumed. Everything above checks ONE artifact at
# a time; this answers the question those checks cannot — is the loop this session is being told to
# follow actually in force. The state that makes it necessary: CLAUDE.md announces the governed loop
# while the trust ref was never created (so the stage floors come from the working tree) and the
# register is UNINITIALIZED (so no claim can be checked). Both were Declared enforcement, held up
# by an ordered next-actions list printed once into a transcript the operator later closed.
#
# --quiet, deliberately: a fully armed repository prints nothing. A status line that appears on
# every session when there is nothing wrong is a line people stop reading, and then it is worth
# nothing on the session where something IS wrong.
STATUS="$(dirname "$0")/intent_status.py"
if [ -f "$STATUS" ]; then
  python3 "$STATUS" --root . --quiet 2>/dev/null
else
  echo "[intent-flow] intent_status.py is not installed, so whether this loop is actually armed is unknown. Re-run install_runtime.sh."
fi
