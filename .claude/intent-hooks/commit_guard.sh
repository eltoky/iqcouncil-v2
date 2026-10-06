#!/usr/bin/env bash
# Shared guard logic. Arg1: newline-separated changed file list. Exit 2 = block, 0 = pass.
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
CFG=".claude/intent-hooks/intent-guard.config"
# PARSED, never sourced (CLAUDE.md rule 9). This file used to `source "$CFG"`, which runs whatever a
# repository-controlled file contains, as the user, on every commit — while session_status.sh in the
# same skill already parsed the same file for exactly that reason. One reader shape for both: the
# first KEY= line, value up to an inline comment, whitespace trimmed. Nothing is executed.
_cfg(){ v=$(grep -m1 "^$1=" "$CFG" 2>/dev/null \
            | sed 's/^[^=]*=//; s/[[:space:]]*#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')
        echo "${v:-$2}"; }
REGISTER=$(_cfg REGISTER docs/intent/INTENT_REGISTER.md)
TRACE=$(_cfg TRACE docs/intent/INTENT_TRACE.yaml)
ENFORCE_LEVEL=$(_cfg ENFORCE_LEVEL block)
TRACE_REQUIRED=$(_cfg TRACE_REQUIRED false)
SPEC_PATHS=$(_cfg SPEC_PATHS "openspec/ docs/specs/")
R="$REGISTER"; T="$TRACE"
LEVEL="$ENFORCE_LEVEL"
COUNTER=".claude/intent-hooks/.ignored_warnings"
FILES="$1"
[ -z "$FILES" ] && exit 0

emit() { # emit <message>  -> block or warn+count per ENFORCE_LEVEL
  if [ "$LEVEL" = "block" ]; then
    echo "[intent-flow] BLOCKED — $1" >&2
    echo "[intent-flow] This is an ACTION ITEM for the current turn, not a log line. Resolve it, then retry the commit." >&2
    exit 2
  else
    echo "[intent-flow] WARNING (ENFORCE_LEVEL=warn) — $1" >&2
    echo "[intent-flow] Treat this as an action item NOW. Proceeding without resolving it is what warn-mode drift looks like." >&2
    echo $(( $(cat "$COUNTER" 2>/dev/null || echo 0) + 1 )) > "$COUNTER" 2>/dev/null || true
  fi
}

spec_touched=0; reg_touched=0; trace_touched=0
for p in ${SPEC_PATHS:-openspec/ docs/specs/}; do echo "$FILES" | grep -q "^${p}" && spec_touched=1; done
echo "$FILES" | grep -qx "$R" && reg_touched=1
echo "$FILES" | grep -qx "$T" && trace_touched=1

if [ $spec_touched -eq 1 ] && [ $reg_touched -eq 0 ]; then
  emit "spec files changed without touching $R. Run intent-spec-gate on the change (install the skill if missing), then intent-register-update, and stage the register — or confirm the gate verdict was PASS with no updates and note that in the commit message."
fi
if [ "${TRACE_REQUIRED:-false}" = "true" ] && [ $spec_touched -eq 1 ] && [ $trace_touched -eq 0 ]; then
  emit "spec files changed without touching $T (TRACE_REQUIRED=true). Refresh the trace map (intent-trace-map) or stage the updated $T."
fi
if [ $reg_touched -eq 1 ]; then
  if ! git diff --cached -- "$R" 2>/dev/null | grep -qE '^\+.*(Changelog|SUPERSEDED|C-[0-9]+)'; then
    emit "$R modified but no changelog/claim line added — register edits must go through intent-register-update discipline."
  fi
fi
exit 0
