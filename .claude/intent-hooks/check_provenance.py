#!/usr/bin/env python3
"""Validate provenance for a PR: commits and the PR's Provenance section.

Usage:
  check_provenance.py --range origin/main..HEAD [--report docs/intent/reports/PR_INTENT_<change-id>.md]
  check_provenance.py --report <file>            # section only

Checks:
  H1  every commit in range that is agent-assisted carries a well-formed Assisted-by trailer
  H5  no commit carries Signed-off-by *added by an agent* (Signed-off-by with no human counterpart
      is flagged; commits carrying Assisted-by AND Signed-off-by are fine only when a human signed)
  H2-H4  the Provenance section exists and has every required field, incl. a non-silent
         "Could not be done"
Exit 0 clean, 1 findings.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import re, sys, argparse, subprocess

TRAILER = re.compile(r"^Assisted-by: [A-Za-z0-9._-]+:[A-Za-z0-9._-]+( [A-Za-z0-9._-]+)*$", re.M)
REQUIRED = ["Tools used", "Inputs", "Generated portions", "Generated share", "Tested with", "Could not be done"]


def commits(rng):
    out = subprocess.run(["git", "log", "--format=%H%x00%B%x01", rng], capture_output=True, text=True).stdout
    for chunk in out.split("\x01"):
        chunk = chunk.strip()
        if not chunk:
            continue
        sha, _, body = chunk.partition("\x00")
        yield sha[:10], body


def check_commits(rng):
    f = []
    for sha, body in commits(rng):
        assisted = bool(TRAILER.search(body)) or "Assisted-by:" in body
        signed = re.search(r"^Signed-off-by:", body, re.M | re.I)
        if "Assisted-by:" in body and not TRAILER.search(body):
            f.append(f"{sha}: H1 malformed Assisted-by trailer (expected AGENT:MODEL [TOOLS])")
        if not assisted:
            f.append(f"{sha}: H1 no Assisted-by trailer — if a tool produced meaningful content, disclose it")
        if assisted and signed and not re.search(r"^Signed-off-by: .+<.+@.+>", body, re.M):
            f.append(f"{sha}: H5 Signed-off-by without a named human identity on an assisted commit")
    return f


def check_report(path):
    try:
        t = open(path).read()
    except FileNotFoundError:
        return [f"{path}: no report found"]
    if "## Provenance" not in t:
        return [f"{path}: H2-H4 no '## Provenance' section"]
    sec = t.split("## Provenance", 1)[1].split("\n## ", 1)[0]
    f = []
    for field in REQUIRED:
        m = re.search(rf"\*\*{re.escape(field)}\*\*\s*[:\u2014-]\s*(.+)", sec)
        if not m or not m.group(1).strip() or m.group(1).strip().upper() in ("TBD", "TODO"):
            f.append(f"{path}: missing or empty '{field}'")
    return f + share_findings(path, t, sec)


def share_findings(path, t, sec):
    """The generated share, read by the ONE reader of reports (verdicts.py), not a second regex —
    the eligibility gate's docstring claimed to share this parser and did not (residual F-009).
    A Provenance section and an intent-report block that disagree are a finding: one is wrong."""
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import verdicts
    prose = verdicts._prose_share(t)
    if "Generated share" in sec and prose is None:
        return [f"{path}: 'Generated share' must be a percentage"]
    doc, _ = verdicts.block(t)
    block_share = (doc or {}).get("generated_share")
    if block_share is not None and prose is not None and block_share != prose:
        return [f"{path}: the Provenance section says {prose}% generated but the intent-report "
                f"block says {block_share}% — one of them is wrong"]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range"); ap.add_argument("--report")
    a = ap.parse_args()
    findings = []
    if a.range:
        findings += check_commits(a.range)
    if a.report:
        findings += check_report(a.report)
    for x in findings:
        print("[provenance]", x)
    print(f"[provenance] {'CLEAN' if not findings else str(len(findings)) + ' finding(s)'}")
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
