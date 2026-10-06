#!/usr/bin/env python3
"""CI coverage gate: every ACTIVE claim that must be tested has a test that actually exists.

Fails when: an ACTIVE MUST TESTABLE claim has no manifest entry; an ACTIVE NEG claim has no
tripwire; an ACTIVE EQUIV claim has no DIFFERENTIAL test; a manifest entry names a test file that
is not there; or the manifest's `register_version` is stale.

Usage: check_test_coverage.py [--register docs/intent/INTENT_REGISTER.md]
                              [--manifest docs/intent/tests/INTENT_TESTS.yaml] [--root .]

WHAT CHANGED
------------
This gate matched claim IDs against manifest **keys** and never looked at the test path, so a
manifest entry pointing at a file that does not exist satisfied it. Executed against the shipped
build: with both test files deleted it printed `OK: 2 tests, 0 gaps` and exited 0. A manifest is an
assertion that a test exists; the gate now checks the assertion.

Two additions for parity work:

  * **EQUIV claims need a DIFFERENTIAL test.** An `EQUIV` claim asserts that a new path behaves
    like a named baseline. That is only ever evidenced by running both and comparing, so the link
    is mechanical here in the same way a NEG claim's tripwire is — and with no `uncovered:` escape
    hatch, for the same reason.
  * **The `uncovered:` escape hatch is narrowed.** It excused a MUST claim on free text, so for
    hard-to-test work the honest path and the gate-satisfying path were the same path. It now
    requires a non-empty reason, it cannot excuse a NEG or EQUIV claim at all, and the count is
    reported on every run so "how many claims are we not testing" is a visible number rather than
    something you would have to go and count.

Requires pyyaml.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import argparse, os, re, sys

CATEGORIES = ("FUNC", "CONSTRAINT", "AUDIENCE", "BIZ", "ARCH", "NEG", "EQUIV")
# Categories whose evidence is non-negotiable: no free-text excuse can stand in for the test.
MANDATORY = {"NEG": ("TRIPWIRE",), "EQUIV": ("T5", "DIFFERENTIAL")}


def claims(register_text):
    """[(cid, category, line)] for every row in the register's category tables."""
    out, cat = [], None
    for line in register_text.splitlines():
        h = re.match(r"## (%s)\b" % "|".join(CATEGORIES), line)
        if h:
            cat = h.group(1)
            continue
        row = re.match(r"\| *(C-\d+) *\|", line)
        if row and cat is not None:
            out.append((row.group(1), cat, line))
    return out


def excused(manifest):
    """{cid: reason} for entries under `uncovered:` that carry a reason of substance."""
    out = {}
    for u in (manifest.get("uncovered") or []):
        if not isinstance(u, dict):
            continue
        cid, why = u.get("claim"), str(u.get("reason") or "").strip()
        if cid and len(why) >= 10:
            out[cid] = why
    return out


def check(register_text, manifest, root="."):
    """(errors, notes, stats) — pure apart from the one os.path.exists per entry."""
    errs, notes = [], []
    tests = {k: v for k, v in (manifest.get("tests") or {}).items() if isinstance(v, dict)}
    exc = excused(manifest)

    rv = re.search(r"Version: *(\d+)", register_text)
    if rv and str(manifest.get("register_version")) != rv.group(1):
        errs.append(f"manifest register_version={manifest.get('register_version')} but register "
                    f"v{rv.group(1)} — regenerate intent-smoke-tests")

    # A manifest entry is an assertion that a test exists. Check it.
    missing_files = []
    for cid, e in sorted(tests.items()):
        p = e.get("test")
        if not p:
            errs.append(f"{cid} has a manifest entry with no test path")
        elif not os.path.exists(os.path.join(root, p)):
            missing_files.append(f"{cid} -> {p}")
    for mf in missing_files:
        errs.append(f"the manifest names a test file that does not exist: {mf}")

    for cid, cat, line in claims(register_text):
        if "SUPERSEDED" in line or "NON-TESTABLE" in line or cat == "BIZ":
            continue
        want = MANDATORY.get(cat)
        if want:
            e = tests.get(cid)
            tier = (e or {}).get("tier", "")
            if not e or str(tier).upper() not in want:
                errs.append(f"{cid} ({cat}) has no {want[0]} test — this category cannot be "
                            f"excused with an uncovered: entry")
            elif cid in exc:
                errs.append(f"{cid} ({cat}) has an uncovered: entry, which this category does "
                            f"not permit")
            continue
        if "MUST" in line and cid not in tests:
            if cid in exc:
                notes.append(f"{cid} (MUST, {cat}) is deliberately untested: {exc[cid]}")
            else:
                errs.append(f"{cid} (MUST, {cat}) has no test and no uncovered-entry")

    stats = {"tests": len(tests), "excused": len(exc), "missing_files": len(missing_files)}
    return errs, notes, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--register", default="docs/intent/INTENT_REGISTER.md")
    ap.add_argument("--manifest", default="docs/intent/tests/INTENT_TESTS.yaml")
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    try:
        import yaml
    except ImportError:
        print("[intent-tests] pyyaml is required", file=sys.stderr)
        return 2
    try:
        reg = open(a.register).read()
        man = yaml.safe_load(open(a.manifest)) or {}
    except Exception as e:
        print(f"[intent-tests] cannot read the register or manifest: {e}", file=sys.stderr)
        return 2
    errs, notes, stats = check(reg, man, a.root)
    for e in errs:
        print("[intent-tests] FAIL:", e)
    for n in notes:
        print("[intent-tests] note:", n)
    print(f"[intent-tests] {'FAIL' if errs else 'OK'}: {stats['tests']} tests, "
          f"{stats['excused']} deliberately untested, {len(errs)} gaps")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
