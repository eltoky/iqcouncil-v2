#!/usr/bin/env python3
"""Compare what the repository CLAIMS is enforced with what the forge actually enforces.

    check_enforcement.py [--config PATH] [--branch main] [--json] [--autonomous]
    check_enforcement.py --self-test

`merge.enforcement` has been a DECLARED setting since the suite was written: a record of what you
believe branch protection does, so that "we thought it was enforced" is a written claim rather
than an assumption. Nothing compared it with reality. That was the largest remaining gap in
ENFORCEMENT_MAP.md, and this closes it.

THE DANGEROUS DIRECTION IS ASYMMETRIC, so the verdicts are too:

  VERIFIED      declared `required`, and the forge lists intent/acceptance among the checks it
                blocks on. The claim is true.
  DRIFT         declared `required`, and the forge does NOT block on it. Every merge the team
                believes was gated could have gone through red. This is a FINDING: the whole point
                of writing the claim down was to make this discoverable.
  STRICTER      declared `advisory`, and the forge blocks anyway. Reality is safer than the
                claim, so this is a note rather than a finding — but the declaration is stale and
                somebody should fix it, because the next reader will trust the file.
  CONSISTENT    declared `advisory`, and the forge does not block. Nothing to say.
  UNVERIFIED    the forge cannot say — no protection concept, an adapter that does not answer the
                optional question, no credential, or a permissions error. NOT a pass and NOT a
                failure: it is the honest third state, and conflating it with either is how a
                check becomes theatre.

Why UNVERIFIED does not simply block: most forges the loop is meant to work on have no
protection API at all — the whole design is that only three questions are forge-specific — so
blocking on an unanswerable question would make the suite GitHub-only by the back door.

But it is NOT good enough for an autonomous merge. Under `--autonomous` (which the gate passes
when the resolved autonomy mode is hotl or hootl) UNVERIFIED becomes a finding, because a mode
that merges with no human looking has to know the gate it depends on is real.

Exit: 0 verified or consistent · 1 drift (or unverified under --autonomous) · 2 usage error
      3 unverified, and not asked to be strict about it.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, io, sys, json, argparse, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
GOV = "docs/intent/governance.yaml"
GATE_CHECK = "intent/acceptance"

VERIFIED, DRIFT, STRICTER, CONSISTENT, UNVERIFIED = (
    "VERIFIED", "DRIFT", "STRICTER", "CONSISTENT", "UNVERIFIED")


def _dict(node, key):
    if not isinstance(node, dict):
        return {}
    got = node.get(key)
    return got if isinstance(got, dict) else {}


def declared(cfg):
    """The claim, normalised. Anything unrecognised is treated as the stricter claim, so a typo
    makes the check MORE demanding rather than silently switching it off."""
    v = str(_dict(cfg, "merge").get("enforcement") or "").strip().lower()
    return v if v in ("required", "advisory") else "required"


def gate_in(protection, check=GATE_CHECK):
    """Is the acceptance gate among the checks the forge will actually block on?

    A forge may name the check with its own prefix (`ci/intent/acceptance`), so a suffix match
    counts — but a name that merely CONTAINS the words does not, or `intent/acceptance-dryrun`
    would read as the real gate.
    """
    if not isinstance(protection, dict):
        return None
    checks = protection.get("required_checks")
    if not isinstance(checks, list):
        return None
    for c in checks:
        c = str(c).strip()
        if c == check or c.endswith("/" + check) or c.endswith(":" + check):
            return True
    return False


def compare(cfg, protection, *, autonomous=False):
    """Pure. (verdict, findings, notes, facts)."""
    claim = declared(cfg)
    present = gate_in(protection)
    facts = {"declared": claim, "gate_check": GATE_CHECK,
             "required_checks": (protection or {}).get("required_checks")
             if isinstance(protection, dict) else None,
             "required_reviews": (protection or {}).get("required_reviews")
             if isinstance(protection, dict) else None,
             "branch": (protection or {}).get("branch") if isinstance(protection, dict) else None,
             "source": (protection or {}).get("source") if isinstance(protection, dict) else None}
    findings, notes = [], []

    if present is None:
        facts["verdict"] = UNVERIFIED
        msg = ("the forge could not say what it enforces, so "
               f"merge.enforcement: {claim} is still only a claim. Either this forge has no "
               "protection concept, its adapter does not answer the optional `protection` "
               "question, or the credential cannot read the setting.")
        if autonomous:
            findings.append(("ENFORCEMENT-UNVERIFIED",
                             msg + " An autonomous merge needs the gate it depends on to be "
                                   "known real, so this is refused rather than assumed."))
        else:
            notes.append(msg)
        return UNVERIFIED, findings, notes, facts

    if claim == "required" and not present:
        facts["verdict"] = DRIFT
        findings.append(("ENFORCEMENT-DRIFT",
                         f"governance declares enforcement: required, but the forge does not block "
                         f"on {GATE_CHECK}. Every merge the team believed was gated could have gone "
                         f"through red. Add it to the protected branch's required checks, or change "
                         f"the declaration to advisory and stop relying on it."))
        return DRIFT, findings, notes, facts

    if claim == "advisory" and present:
        facts["verdict"] = STRICTER
        notes.append(f"the forge blocks on {GATE_CHECK} although governance declares advisory. "
                     f"Reality is stricter than the claim, which is the safe direction — but the "
                     f"declaration is stale and the next person to read it will be misled.")
        return STRICTER, findings, notes, facts

    facts["verdict"] = VERIFIED if claim == "required" else CONSISTENT
    return facts["verdict"], findings, notes, facts


def exit_code(verdict, findings):
    if findings:
        return 1
    return 3 if verdict == UNVERIFIED else 0


def load_cfg(path=None):
    """(policy, trusted) through govcfg, the ONE reader. Untrusted is EMPTY, never the working
    tree — the working tree is the branch under review."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load(override=path)
    if not L.trusted:
        print(f"[enforcement] governance is UNTRUSTED — {L.why}", file=sys.stderr)
    return L.data, L.trusted


def main():
    ap = argparse.ArgumentParser(description="is the gate actually enforced, or only declared?")
    ap.add_argument("--config")
    ap.add_argument("--branch")
    ap.add_argument("--autonomous", action="store_true",
                    help="treat UNVERIFIED as a finding, as an autonomous merge must")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    try:
        cfg, _trusted = load_cfg(a.config)
    except Exception as e:                                  # noqa: BLE001
        print(f"[enforcement] the configuration could not be read: {e}", file=sys.stderr)
        return 2
    sys.path.insert(0, HERE)
    try:
        import forge as FG
        fg = FG.Forge.from_config(cfg)
        protection = fg.protection(a.branch)
    except Exception as e:                                  # noqa: BLE001
        print(f"[enforcement] the forge could not be asked: {e}", file=sys.stderr)
        protection = None

    verdict, findings, notes, facts = compare(cfg, protection, autonomous=a.autonomous)
    if a.json:
        print(json.dumps({"verdict": verdict, "findings": findings, "notes": notes,
                          "facts": facts}, indent=2, sort_keys=True))
    else:
        print(f"[enforcement] {verdict}   declared {facts['declared']}"
              + (f" · forge requires {facts['required_checks']}"
                 if facts["required_checks"] is not None else " · the forge did not say"))
        for _code, text in findings:
            print(f"  FINDING  {text}")
        for n in notes:
            print(f"  note     {n}")
    return exit_code(verdict, findings)


# ---------------------------------------------------------------------------- self-test

def self_test():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    def cfg(enf):
        return {"merge": {"enforcement": enf}}

    def prot(checks, **kw):
        d = {"branch": "main", "source": "test", "required_checks": checks}
        d.update(kw)
        return d

    # -- the four determinate verdicts
    v, f, n, _ = compare(cfg("required"), prot([GATE_CHECK]))
    check("required + enforced is VERIFIED", v == VERIFIED and not f and not n)
    v, f, _, _ = compare(cfg("required"), prot(["build", "test"]))
    check("required + NOT enforced is DRIFT", v == DRIFT)
    check("DRIFT is a finding", f and f[0][0] == "ENFORCEMENT-DRIFT")
    check("DRIFT exits 1", exit_code(v, f) == 1)
    v, f, n, _ = compare(cfg("advisory"), prot([GATE_CHECK]))
    check("advisory + enforced is STRICTER", v == STRICTER)
    check("STRICTER is only a note", not f and n)
    check("STRICTER exits 0", exit_code(v, f) == 0)
    v, f, n, _ = compare(cfg("advisory"), prot([]))
    check("advisory + not enforced is CONSISTENT", v == CONSISTENT and not f)

    # -- UNVERIFIED: the honest third state
    for label, protection in [("no answer at all", None),
                              ("not a mapping", "nonsense"),
                              ("no checks key", {"branch": "main"}),
                              ("checks not a list", {"required_checks": "intent/acceptance"}),
                              ("an empty answer", {})]:
        v, f, n, _ = compare(cfg("required"), protection)
        check(f"{label} is UNVERIFIED", v == UNVERIFIED, f"got {v}")
        check(f"{label} is not a finding by default", not f)
        check(f"{label} says so", bool(n))
        check(f"{label} exits 3", exit_code(v, f) == 3)
        # ...but an autonomous merge may not proceed on an unverified gate
        v2, f2, _, _ = compare(cfg("required"), protection, autonomous=True)
        check(f"{label} IS a finding for an autonomous merge",
              f2 and f2[0][0] == "ENFORCEMENT-UNVERIFIED")
        check(f"{label} exits 1 under --autonomous", exit_code(v2, f2) == 1)
    # an empty check list is a real answer, not an absence: the forge said "nothing is required"
    v, f, _, _ = compare(cfg("required"), prot([]))
    check("an empty required_checks list is DRIFT, not UNVERIFIED", v == DRIFT, f"got {v}")

    # -- how the check may be named
    for checks, want in [([GATE_CHECK], True),
                         (["ci/intent/acceptance"], True),
                         (["buildkite:intent/acceptance"], True),
                         (["intent/acceptance-dryrun"], False),
                         (["acceptance"], False),
                         (["Intent/Acceptance"], False),      # case matters to the forge
                         ([" intent/acceptance "], True),     # whitespace does not
                         ([], False)]:
        check(f"{checks} -> gate present {want}", gate_in(prot(checks)) is want,
              f"got {gate_in(prot(checks))}")

    # -- a typo in the claim makes the check STRICTER, never weaker
    for bad in ("Required", "REQUIRED", "requred", "", None, "off", "yes", 7, []):
        check(f"declared {bad!r} reads as required", declared({"merge": {"enforcement": bad}})
              == "required")
    check("advisory is honoured when spelled correctly",
          declared({"merge": {"enforcement": "advisory"}}) == "advisory")
    check("ADVISORY in caps still reads as advisory",
          declared({"merge": {"enforcement": "ADVISORY"}}) == "advisory")

    # -- garbage configuration never raises and never reports VERIFIED
    for junk in (None, {}, {"merge": "nonsense"}, {"merge": None}, [1, 2]):
        try:
            v, f, _, _ = compare(junk if isinstance(junk, dict) else {}, None)
            check(f"garbage {junk!r} is UNVERIFIED", v == UNVERIFIED, f"got {v}")
        except Exception as e:                              # noqa: BLE001
            check(f"garbage {junk!r} does not raise ({e})", False)
    v, f, _, _ = compare({"merge": "nonsense"}, prot([GATE_CHECK]))
    check("a malformed merge block still compares", v == VERIFIED, f"got {v}")

    # -- the facts carried into the record
    _, _, _, facts = compare(cfg("required"), prot([GATE_CHECK], required_reviews=2))
    for k in ("declared", "gate_check", "required_checks", "required_reviews", "verdict"):
        check(f"the record carries {k}", k in facts)
    check("the record carries the review count", facts["required_reviews"] == 2)

    print(f"[enforcement] self-test: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
