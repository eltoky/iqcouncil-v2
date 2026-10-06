#!/usr/bin/env bash
# CI check: register/trace version coherence. Exit 1 = fail build.
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
set -e
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
source .claude/intent-hooks/intent-guard.config 2>/dev/null || true
R="${REGISTER:-docs/intent/INTENT_REGISTER.md}"; T="${TRACE:-docs/intent/INTENT_TRACE.yaml}"
[ -f "$R" ] || { echo "[intent-flow] $R missing"; exit 1; }
RV=$(grep -m1 -oE 'Version: *[0-9]+' "$R" | grep -oE '[0-9]+')
if [ -f "$T" ]; then
  TV=$(grep -m1 -oE 'register_version: *[0-9]+' "$T" | grep -oE '[0-9]+')
  if [ "$TV" != "$RV" ]; then echo "[intent-flow] FAIL: trace map register_version=$TV, register Version=$RV — regenerate with intent-trace-map"; exit 1; fi
elif [ "${TRACE_REQUIRED:-false}" = "true" ]; then
  echo "[intent-flow] FAIL: TRACE_REQUIRED=true but $T missing"; exit 1
fi
echo "[intent-flow] OK: register v$RV, trace coherent"
