#!/usr/bin/env python3
"""Claude Code PreToolUse hook for the intent commit guard.
Hook input arrives as JSON on stdin (NOT an env var). Exit 2 blocks the tool call and only
stderr reaches the model, so the guard's exit code is propagated unchanged and its output
is forwarded to stderr.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import json, os, re, subprocess, sys

try:
    call = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if call.get("tool_name") != "Bash":
    sys.exit(0)
cmd = (call.get("tool_input") or {}).get("command", "") or ""
if not re.search(r"\bgit\b[^;&|]*\b(commit|push)\b", cmd):
    sys.exit(0)

root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or "."
staged = subprocess.run(["git", "diff", "--cached", "--name-only"], capture_output=True, text=True, cwd=root).stdout
guard = os.path.join(root, ".claude/intent-hooks/commit_guard.sh")
if not os.path.exists(guard):
    sys.exit(0)
r = subprocess.run(["bash", guard, staged], capture_output=True, text=True, cwd=root)
if r.stdout: sys.stderr.write(r.stdout)
if r.stderr: sys.stderr.write(r.stderr)
sys.exit(2 if r.returncode == 2 else 0)
