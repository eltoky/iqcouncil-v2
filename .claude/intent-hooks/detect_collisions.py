#!/usr/bin/env python3
"""Layer 2 — detect overlap between in-flight changes. Advisory: reports, never blocks.

Usage:
  detect_collisions.py --change <id> [--stage intent|promotion|plan]
  detect_collisions.py --all
  detect_collisions.py --change <id> --json

Compares this change against every other change whose intent.md status is not
archived/rejected, on three axes: intent subject matter, claim sets, planned files.
Writes docs/intent/reports/COLLISION_<change-id>.md unless --json.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, argparse
from datetime import datetime, timezone

CHANGES = "docs/intent/changes"
REPORTS = "docs/intent/reports"
STOP = set("""the a an and or of to for in on with by from is are be that this it as at we our us
will shall can should must not no add new use using support allow enable make into when if then
user users system systems data feature change update fix""".split())


def read(p):
    try:
        return open(p).read()
    except FileNotFoundError:
        return ""


def status_of(txt):
    m = re.search(r"Status:\s*(\w+)", txt)
    return (m.group(1) if m else "unknown").lower()


def keywords(txt):
    body = txt
    for h in ("## Problem", "## Proposed outcome"):
        if h in body:
            body = body.split(h, 1)[1]
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{3,}", body.lower())
    return {w for w in words if w not in STOP}


def claims_of(cid):
    """Claims this change touches: from its register fragment, else cited in intent.md."""
    frag = f"docs/intent/register.d/{cid}.md"
    txt = read(frag) or read(f"{CHANGES}/{cid}/intent.md") + read(f"{CHANGES}/{cid}/plan.md")
    return set(re.findall(r"\bC-[\w-]*\d+\b", txt))


def planned_files(cid):
    txt = read(f"{CHANGES}/{cid}/plan.md")
    if "## Files that change" not in txt:
        return set()
    body = txt.split("## Files that change", 1)[1].split("\n## ", 1)[0]
    return {m.strip() for m in re.findall(r"[\w./-]+\.[A-Za-z0-9]+", body)}


def load_changes():
    out = {}
    if not os.path.isdir(CHANGES):
        return out
    for cid in sorted(os.listdir(CHANGES)):
        ip = f"{CHANGES}/{cid}/intent.md"
        if not os.path.exists(ip):
            continue
        txt = read(ip)
        st = status_of(txt)
        if st in ("archived", "rejected", "closed"):
            continue
        out[cid] = {"status": st, "kw": keywords(txt), "claims": claims_of(cid),
                    "files": planned_files(cid),
                    "holder": (re.search(r"Author:\s*([^.\n]+)", txt) or [None, "?"])[1]
                              if re.search(r"Author:\s*([^.\n]+)", txt) else "?"}
    return out


def overlaps(me, other):
    kw = me["kw"] & other["kw"]
    sim = len(kw) / max(1, min(len(me["kw"]), len(other["kw"]))) if me["kw"] and other["kw"] else 0
    return {
        "subject_similarity": round(sim, 2),
        "shared_keywords": sorted(list(kw))[:12],
        "shared_claims": sorted(me["claims"] & other["claims"]),
        "shared_files": sorted(me["files"] & other["files"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--change"); ap.add_argument("--all", action="store_true")
    ap.add_argument("--stage", default="all"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    ch = load_changes()
    if not ch:
        print("[collide] no in-flight changes found"); return 0
    targets = list(ch) if a.all else [a.change]
    findings = []
    for cid in targets:
        if cid not in ch:
            print(f"[collide] unknown change {cid}"); return 1
        for other, od in ch.items():
            if other == cid:
                continue
            ov = overlaps(ch[cid], od)
            hit = (ov["shared_claims"] or ov["shared_files"]
                   or (a.stage in ("intent", "all") and ov["subject_similarity"] >= 0.30))
            if hit:
                findings.append({"change": cid, "other": other, "other_status": od["status"],
                                 "other_holder": od["holder"], **ov})
    if a.json:
        print(json.dumps(findings, indent=1)); return 0
    if not findings:
        print(f"[collide] no overlap detected for {', '.join(targets)} "
              f"(compared against {len(ch)-1} other in-flight change(s))")
        return 0
    lines = [f"# Collision report — {datetime.now(timezone.utc).date()}", "",
             "Advisory only: this report names who to talk to. It does not arbitrate — "
             "whether two changes can proceed is a judgment for the two humans.", ""]
    for f in findings:
        lines += [f"## {f['change']}  \u2194  {f['other']}  ({f['other_status']}, {f['other_holder']})", ""]
        if f["shared_claims"]:
            lines.append(f"- **Shared claims** (register conflict risk): {', '.join(f['shared_claims'])}")
        if f["shared_files"]:
            lines.append(f"- **Shared planned files** (merge conflict incoming): {', '.join(f['shared_files'])}")
        if f["subject_similarity"] >= 0.30:
            lines.append(f"- **Subject similarity {f['subject_similarity']}** — possible duplicate work. "
                         f"Shared terms: {', '.join(f['shared_keywords'])}")
        lines.append("")
    lines += ["## Not checked here", "",
              "- Blast-radius intersection: run CRG `get_impact_radius_tool` on each change's planned "
              "files and compare. Different files with intersecting radii collide semantically, and git "
              "will not warn you.", ""]
    os.makedirs(REPORTS, exist_ok=True)
    out = f"{REPORTS}/COLLISION_{targets[0] if not a.all else 'ALL'}.md"
    open(out, "w").write("\n".join(lines))
    print(f"[collide] {len(findings)} overlap(s) → {out}")
    for f in findings:
        bits = []
        if f["shared_claims"]: bits.append(f"claims {','.join(f['shared_claims'])}")
        if f["shared_files"]: bits.append(f"{len(f['shared_files'])} file(s)")
        if f["subject_similarity"] >= 0.30: bits.append(f"subject {f['subject_similarity']}")
        print(f"  {f['change']} ~ {f['other']} ({f['other_holder']}): " + "; ".join(bits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
