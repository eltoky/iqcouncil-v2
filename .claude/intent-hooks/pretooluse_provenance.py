#!/usr/bin/env python3
"""Claude Code PreToolUse hook — H5 inside the harness.
Reads the tool call as JSON on stdin. Exit 2 blocks the call; stderr is fed back to the model.
Blocks: git commit with -s/--signoff or a Signed-off-by trailer; the agent approving or merging a PR.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import json, re, sys

try:
    call = json.load(sys.stdin)
except Exception:
    sys.exit(0)                      # never block on a hook we cannot parse
if call.get("tool_name") != "Bash":
    sys.exit(0)
cmd = (call.get("tool_input") or {}).get("command", "") or ""

def block(msg):
    print(f"[provenance] BLOCKED (H5): {msg}", file=sys.stderr)
    print("[provenance] The agent prepares the change; a human certifies, approves and merges it. "
          "Correct the command and continue — this is a same-turn action item, not a stop.", file=sys.stderr)
    sys.exit(2)

if re.search(r"\bgit\b[^;&|]*\bcommit\b", cmd):
    if re.search(r"(^|\s)(-s|--signoff)(\s|$)", cmd) or re.search(r"Signed-off-by:", cmd, re.I):
        block("agents must never add Signed-off-by (do not use -s/--signoff). Only a human can certify.")
if re.search(r"\bgh\s+pr\s+merge\b", cmd):
    block("the agent must not merge a PR on its own authority. Hand the merge-gate report to a human.")
if re.search(r"\bgh\s+pr\s+review\b[^;&|]*--approve", cmd):
    block("the agent must not approve a PR. Approval is a human certification.")
m = re.search(r"intent_escalate\.py\s+(sign|keygen|override)\b", cmd)
if m:
    block(f"'intent_escalate.py {m.group(1)}' is for humans only \u2014 approvers sign and generate keys on "
          "their own machines, and an override is a human decision. The agent may run 'request' and 'verify'.")
sys.exit(0)
