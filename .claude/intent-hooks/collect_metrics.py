#!/usr/bin/env python3
"""Derive intent-loop indicators from git history + the artifact tree.
Usage: collect_metrics.py [REPO] [--out PATH]
Reports only what it can actually derive; everything else is listed NOT-MEASURED."""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py

import os, re, subprocess, argparse
from datetime import datetime, timezone

def git(repo, *a):
    try:
        r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""

def first_commit_ts(repo, path):
    out = git(repo, "log", "--diff-filter=A", "--format=%ct", "--", path).strip().splitlines()
    return int(out[-1]) if out else None

def commits_after(repo, path, ts):
    if ts is None: return 0
    out = git(repo, "log", "--format=%ct", "--", path).strip().splitlines()
    return sum(1 for c in out if c and int(c) > ts)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--out")
    a = ap.parse_args(); repo = a.repo
    changes_dir = os.path.join(repo, "docs/intent/changes")
    intents = []
    if os.path.isdir(changes_dir):
        for cid in sorted(os.listdir(changes_dir)):
            rel = f"docs/intent/changes/{cid}/intent.md"
            full = os.path.join(repo, rel)
            if not os.path.exists(full): continue
            txt = open(full).read()
            m = re.search(r"Status:\s*(\w+)", txt)
            status = (m.group(1) if m else "unknown").lower()
            planrel = f"docs/intent/changes/{cid}/plan.md"
            plan_first = first_commit_ts(repo, planrel) if os.path.exists(os.path.join(repo, planrel)) else None
            oq_body = txt.split("## Open questions")[-1] if "## Open questions" in txt else ""
            oq = len([l for l in oq_body.splitlines() if l.strip() and not l.startswith("#")])
            intents.append({"id": cid, "status": status,
                            "first": first_commit_ts(repo, rel), "plan_first": plan_first,
                            "churn": commits_after(repo, rel, plan_first) if plan_first else 0,
                            "oq": oq})
    n = len(intents)
    accepted = sum(1 for i in intents if i["status"] == "accepted")
    rejected = sum(1 for i in intents if i["status"] in ("rejected", "closed"))
    decided = accepted + rejected
    survival = (accepted / decided * 100) if decided else None

    reg = os.path.join(repo, "docs/intent/INTENT_REGISTER.md")
    neg = active = 0
    if os.path.exists(reg):
        rt = open(reg).read()
        neg = len(re.findall(r"^\| C-\d+", rt.split("## NEG")[-1], re.M)) if "## NEG" in rt else 0
        active = len(re.findall(r"^\| C-\d+ .*ACTIVE", rt, re.M))

    theater = []
    if survival is not None and decided >= 5:
        theater.append(("Survival rate ~100%", f"{survival:.0f}% of {decided} decided",
                        "THEATER-SUSPECTED" if survival >= 99 else "PASS"))
    elif survival is not None:
        theater.append(("Survival rate", f"{survival:.0f}% of {decided} decided (small sample)", "PASS"))
    if n:
        total_oq = sum(i["oq"] for i in intents)
        theater.append(("Open questions recorded", f"{total_oq} across {n} intents",
                        "THEATER-SUSPECTED" if total_oq == 0 else "PASS"))
    theater.append(("NEG claims in register", str(neg),
                    "THEATER-SUSPECTED" if (neg == 0 and active) else "PASS"))

    churn = sum(i["churn"] for i in intents)
    plans = sum(1 for i in intents if i["plan_first"])
    L = [f"# Intent Loop Metrics — {datetime.now(timezone.utc).date()}", "",
         "## Theater tests (read these first)", "| Test | Value | Verdict |", "|---|---|---|"]
    L += [f"| {t} | {v} | {r} |" for t, v, r in theater]
    L += ["", "## Derived indicators", "| Indicator | Value |", "|---|---|",
          f"| Change records (intent.md) | {n} |",
          f"| Accepted / rejected / undecided | {accepted} / {rejected} / {n - decided} |",
          f"| Survival rate | {f'{survival:.0f}%' if survival is not None else 'n/a — none decided'} |",
          f"| Post-plan intent churn (commits) | {churn} |",
          f"| Plans committed | {plans} of {n} |",
          f"| Register ACTIVE / NEG claims | {active} / {neg} |", "",
          "## NOT-MEASURED (where the number lives)",
          "- First-pass CI success rate → CI system",
          "- Review time per PR → PR metadata",
          "- Diff-vs-plan match rate → intent-pr-review reports",
          "- Gate wait times → OpenTelemetry export",
          "- Incident and regression counts → incident tracker", ""]
    if n < 10:
        L.append(f"> Sample is small (n={n}); ratios are not yet meaningful — read the raw counts.\n")
    out = a.out or os.path.join(repo, f"docs/intent/reports/METRICS_{datetime.now(timezone.utc).date()}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write("\n".join(L))
    flags = len([t for t in theater if t[2] != "PASS"])
    print(f"[metrics] wrote {out} — {n} change records, survival "
          f"{f'{survival:.0f}%' if survival is not None else 'n/a'}, {flags} theater flag(s)")

if __name__ == "__main__":
    main()
