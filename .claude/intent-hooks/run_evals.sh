#!/usr/bin/env bash
# Run the intent eval suite non-interactively. Usage: run_evals.sh [CASES_DIR] [OUT_MD]
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
set -uo pipefail
CASES="${1:-docs/intent/evals/cases}"; OUT="${2:-docs/intent/reports/EVAL_$(date +%F).md}"
# The CHECKER is code that decides whether a case passed. Under test, the candidate's own check.sh
# would grade itself — and run with the API key in scope. CI points EVALS_CHECK at the protected
# branch's copy; locally the repository's own checker is used, which is yours to trust.
CHECK="${EVALS_CHECK:-$(dirname "$CASES")/check.sh}"
[ -f "$CHECK" ] || { echo "[evals] NO CHECKER at $CHECK — nothing can be graded"; exit 3; }
mkdir -p "$(dirname "$OUT")"; pass=0; fail=0; rows=""
for c in "$CASES"/*.json; do
  # No cases is NOTHING MEASURED, not a pass. This used to exit 0, so a suite with every case
  # deleted reported green — the suite's first rule, broken by the eval runner.
  [ -e "$c" ] || { echo "[evals] NO CASES in $CASES — nothing was measured, which is not a pass"; exit 3; }
  id=$(jq -r '.id' "$c"); prompt=$(jq -r '.prompt' "$c")
  tools=$(jq -r '.allowedTools // "Read,Grep"' "$c")
  claude -p "$prompt" --allowedTools "$tools" --output-format json > /tmp/result.json 2>/dev/null
  if bash "$CHECK" "$c" /tmp/result.json; then
    pass=$((pass+1)); rows="$rows| $id | PASS |\n"
  else
    fail=$((fail+1)); rows="$rows| $id | **FAIL** |\n"
  fi
done
total=$((pass+fail)); rate=$(( total>0 ? pass*100/total : 0 ))
{ echo "# Eval run — $(date +%F)"; echo; echo "Pass rate: **${rate}%** ($pass/$total)"; echo;
  echo "| Case | Result |"; echo "|---|---|"; printf "%b" "$rows";
  echo; echo "> A suite at 100% for months is measuring nothing — check it still discriminates."; } > "$OUT"
echo "[evals] $pass/$total passed (${rate}%) → $OUT"
[ "$fail" -eq 0 ]
