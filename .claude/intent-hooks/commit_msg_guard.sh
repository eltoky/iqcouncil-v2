#!/usr/bin/env bash
# git commit-msg hook — H1 and H5 for agent commits.
# Install: ln -s ../../.claude/intent-hooks/commit_msg_guard.sh .git/hooks/commit-msg
# (chain it if a commit-msg hook already exists). $1 = path to the message file.
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
MSG_FILE="$1"; [ -f "$MSG_FILE" ] || exit 0
CFG="docs/intent/provenance.config"
# `cut -d= -f2` truncates any value containing '=' and `tr -d " "` deletes INTERIOR spaces, not
# just surrounding ones — which silently rewrote configured paths elsewhere in the suite.
get(){ v=$(grep -m1 "^$1=" "$CFG" 2>/dev/null \
           | sed 's/^[^=]*=//; s/[[:space:]]*#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^"\(.*\)"$/\1/'); echo "${v:-$2}"; }
# A hard rule must not be switched off by a spelling. `True`, `TRUE`, `yes` and `"true"` all read
# as "not true" under an exact lowercase comparison, so a typo disabled H1 or H5 silently and the
# commit sailed through. Anything unrecognised is treated as ON and reported, never as OFF.
truthy(){ case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
            true|yes|on|1) return 0 ;;
            false|no|off|0) return 1 ;;
            *) printf '[provenance] WARNING: unrecognised setting value "%s" — treating it as enabled\n' "$1" >&2; return 0 ;;
          esac; }

# Is this an agent session?
agent=0
IFS=',' read -ra MARKERS <<< "$(get AGENT_MARKERS CLAUDE_CODE_CHILD_SESSION,INTENT_AGENT_ID)"
for m in "${MARKERS[@]}"; do [ -n "${!m:-}" ] && [ "${!m}" != "0" ] && agent=1; done
[ "$(get ALLOW_CLAUDECODE_FALLBACK false)" = "true" ] && [ "${CLAUDECODE:-}" = "1" ] && agent=1
[ "$agent" = "1" ] || exit 0          # human commit: no constraints, humans may sign off

# Normalise before matching. A trailer that is PRESENT must never be reported as missing:
#  - \r (CRLF from Windows editors and some GUI clients) made '...$' fail on every line;
#  - trailing spaces, which editors and copy-paste add invisibly, did the same.
# Both produced the "missing attribution trailer" message while the trailer was right there.
body=$(grep -v '^#' "$MSG_FILE" | tr -d '\r' | sed 's/[[:space:]]*$//')
fail=0
if truthy "$(get FORBID_AGENT_SIGNOFF true)" && echo "$body" | grep -qiE '^Signed-off-by:'; then
  echo "[provenance] BLOCKED (H5): an agent must never add Signed-off-by. Only a human can certify a contribution." >&2
  echo "[provenance] Remove the trailer; the human who reviews and can defend this change adds their own sign-off." >&2
  fail=1
fi
if truthy "$(get REQUIRE_ASSISTED_BY true)"; then
  if ! echo "$body" | grep -qiE '^Assisted-by:[[:space:]]+[A-Za-z0-9._/@-]+:[A-Za-z0-9._/@-]+([[:space:]]+[A-Za-z0-9._/@-]+)*$'; then
    # MALFORMED and MISSING are different mistakes and deserve different messages. Reporting
    # "missing" for a trailer that is present, only mis-shaped, is why this hook was hard to satisfy.
    if echo "$body" | grep -qiE '^Assisted.by:'; then
      echo "[provenance] BLOCKED (H1): the Assisted-by trailer is present but MALFORMED." >&2
      echo "  found:    $(echo "$body" | grep -iE '^Assisted.by:' | head -1)" >&2
      echo "  expected: Assisted-by: AGENT:MODEL [TOOL]...   (one colon, no spaces inside AGENT or MODEL)" >&2
      echo "  e.g.      Assisted-by: Claude:claude-opus-5 graphify crg" >&2
      echo "[provenance] 'Claude Code:claude-opus-5' fails — write it as Claude-Code:claude-opus-5." >&2
    else
      echo "[provenance] BLOCKED (H1): agent commits must carry an attribution trailer:" >&2
      echo "  Assisted-by: AGENT:MODEL [TOOL1] [TOOL2]" >&2
      echo "  e.g. Assisted-by: Claude:claude-opus-5 graphify crg" >&2
    fi
    echo "[provenance] List specialised tools only — not git, editors, compilers or test runners." >&2
    echo "[provenance] Other trailers are fine and do NOT replace this one. A harness that asks for" >&2
    echo "  Co-Authored-By / Claude-Session satisfies neither H1 nor H2 — add BOTH, e.g.:" >&2
    echo "      Assisted-by: Claude:claude-opus-5 graphify" >&2
    echo "      Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" >&2
    fail=1
  fi
fi
# Second line of defence for MANDATORY model routing. The PreToolUse hook enforces that a decision
# was recorded before a subagent spawned; this catches the other half — a commit whose Assisted-by
# model contradicts the recorded decision. Only fires when the trailer names a tier keyword, so
# model strings we do not recognise are never a reason to refuse a commit.
LEDGER="docs/intent/governance/MODEL_ROUTING.log"
if [ "$(get MODEL_ROUTE_CHECK true)" = "true" ] && [ -f "$LEDGER" ]; then
  trailer_model=$(echo "$body" | sed -n 's/^Assisted-by: [^:]*:\([^ ]*\).*/\1/p' | head -1)
  tier=$(printf '%s' "$trailer_model" | grep -oiE 'haiku|sonnet|opus|fable' | head -1 | tr 'A-Z' 'a-z')
  if [ -n "$tier" ]; then
    # tail FIRST, then extract. Extracting first and tailing took the last line that happened to
    # match `[a-z]*`, so a newer decision naming a model with a digit or hyphen ("claude-opus-5")
    # did not match and the guard quietly compared the commit against an older decision.
    recorded=$(tail -1 "$LEDGER" 2>/dev/null | sed -n 's/.*"model"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9._-]*\)".*/\1/p')
    if [ -n "$recorded" ] && [ "$recorded" != "$tier" ]; then
      echo "[provenance] BLOCKED: this commit says Assisted-by ...:$trailer_model ($tier), but the last" >&2
      echo "  recorded routing decision was '$recorded'. H1 must name the model that DID the work." >&2
      echo "  Either the work ran on the wrong model, or the decision was never re-recorded after" >&2
      echo "  escalating a tier. Re-run model_route.py --record for the work you actually did." >&2
      fail=1
    fi
  fi
fi

exit $fail
