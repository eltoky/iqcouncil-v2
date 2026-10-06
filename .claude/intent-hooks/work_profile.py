#!/usr/bin/env python3
"""Turn "what kind of work does this repository do?" into routing kinds and path fences.

    work_profile.py --list                         the profiles, one line each
    work_profile.py --profile NAME [NAME ...]      kinds, fences, evidence, what stays manual
    work_profile.py --profile NAME --yaml          the governance fragment to merge
    work_profile.py --profile NAME --json          the same, machine-readable
    work_profile.py --detect --root .              ADVISORY suggestion from the tree's own files
    work_profile.py --self-test

Why this exists: `intent-init` wrote the shipped `model_routing` block wholesale and asked nothing
about the work. Two consequences, and they pull in opposite directions.

The first is that the kinds are generic. `docs/WORK_TYPE_PROFILES.md` tells a reader which `--kind`
names carry their scenario, and until this script existed nothing connected that document to the
file the router reads — so a repository doing modernization was routed by a taxonomy chosen for
feature work, and an agent describing its own work accurately got `KIND-UNKNOWN`.

The second is worse, because it fails silently rather than loudly: the protected paths
(`risk.paths`, before 2.0.0 `arch_contract_paths` / `irreversible_paths`) are the fences that decide
whether a change can merge unattended, and the
shipped defaults are *generic globs*. A repository whose architectural boundaries are not in that
list has an unfenced path to anywhere — and every individual change looks small and correct. The
fence has to be the repository's real layout, which means somebody has to be asked.

So the profile is DERIVED from an answer a person can give, and it carries three things no
generated config usually does:

  * the kinds, so the router recognises the work instead of defaulting
  * the fence globs the profile needs ON TOP of the shipped defaults, as a CHECKLIST to confirm
    against the real tree — never as a claim that the fences are now correct
  * what STAYS MANUAL, printed every time, because a profile that hides its gaps is the failure
    the whole work-type assessment exists to prevent

`--detect` is advisory and says so. It reads extensions and nothing else, so it cannot tell a
greenfield service from a rebuild of one — that is the difference between a property claim and a
relation claim, and only a person knows which they are making.

EXIT: 0 derived · 1 nothing derivable · 2 usage, or a profile that is out of scope.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, json, argparse

# Each profile mirrors a section of docs/WORK_TYPE_PROFILES.md. `manual` is mandatory: the
# self-test refuses a profile without it, for the same reason invariant BB refuses a documented
# profile with no "Stays manual" row.
PROFILES = {
    "greenfield": {
        "title": "Greenfield development",
        "kinds": ["greenfield_feature", "feature_implementation", "test_authoring", "spec_delta"],
        "claims": "FUNC, CONSTRAINT, NEG",
        "evidence": "T2 for behaviour, T4 for any stated number, a tripwire for every NEG claim",
        "relation": False,
        "fences": [],
        "manual": ["claims are VERBAL until something realizes them — correct, not a gap"],
    },
    "brownfield": {
        "title": "Brownfield feature work",
        "kinds": ["brownfield_change", "bug_fix", "high_impact_bug_fix", "hard_implementation"],
        "claims": "FUNC, CONSTRAINT, ARCH",
        "evidence": "T2/T3 in the suite the repository already has — never introduce a second",
        "relation": False,
        "fences": [],
        "manual": ["the orphan sweep: scope the first trace map to the modules you are changing, "
                   "or a large repo yields thousands of UNMAPPED entries with no accepted baseline"],
    },
    "modernization": {
        "title": "Modernization",
        "kinds": ["modernization", "architecture_change", "cross_service_refactor"],
        "claims": "EQUIV (the contract), ARCH (the target shape), NEG (what you are not carrying)",
        "evidence": "T5 DIFFERENTIAL — recorded-traffic replay, or a golden corpus for a library",
        "relation": True,
        "fences": ["infra/**", "**/routes*", "config/flags*"],
        "manual": ["capturing the baseline: recording traffic or building a golden corpus is the "
                   "precondition for everything else and the loop does not do it for you"],
    },
    "legacy-escape": {
        "title": "Legacy escape (strangler fig)",
        "kinds": ["legacy_escape", "cutover", "irreversible_change"],
        "claims": "EQUIV almost exclusively, plus NEG for behaviours dropped on purpose",
        "evidence": "T5 against the captured baseline, per diverted behaviour",
        "relation": True,
        "fences": ["**/routes*", "config/flags*", "**/feature_flags*", "infra/**"],
        "manual": ["deciding which bugs to preserve: a faithfully reproduced bug passes a T5 test "
                   "and only a person can say whether that was the intention"],
    },
    "rebuild": {
        "title": "Rebuild alongside the running system",
        "kinds": ["rebuild_parity", "equivalence_claim"],
        "claims": "EQUIV with repo: on the baseline path, NEG for deliberate scope cuts",
        "evidence": "T5 per claim; role: BASELINE on the old path so the orphan sweep ignores it",
        "relation": True,
        "fences": ["**/routes*", "infra/**"],
        "manual": ["cross-repo verification: the gate reads ONE checkout, so a repo: key is a "
                   "reviewable assertion and not a verified one (ledger F-021)"],
    },
    "new-language-service": {
        "title": "New service in another language",
        "kinds": ["new_language_service", "language_port", "mechanical_port"],
        "claims": "FUNC, CONSTRAINT, ARCH — a new service is property work, not relation work",
        "evidence": "T2 in the new language's own framework",
        "relation": False,
        "fences": ["**/Dockerfile", "**/*.lock", "**/go.mod", "**/Cargo.toml", "**/pom.xml"],
        "manual": ["complexity: radon parses Python only, so the per-function ceiling does not "
                   "apply — wire your language's own tool into CI and say so in coverage.yaml"],
    },
    "refactoring": {
        "title": "Refactoring and re-platforming",
        "kinds": ["refactor", "cross_service_refactor", "framework_migration", "runtime_upgrade",
                  "dependency_upgrade", "platform_change", "mechanical_port",
                  "build_signing_change"],
        "claims": "the EXISTING claims, untouched. EQUIV only at an observable boundary",
        "evidence": "the existing test suite run UNCHANGED — a refactor that needed its tests "
                    "rewritten changed behaviour. T5 where there is a boundary",
        "relation": None,          # depends on the scope; the ladder decides
        "fences": ["**/*.lock", "**/package.json", "**/requirements*.txt", "**/Dockerfile",
                   "**/*.pbxproj", "**/*.entitlements", "fastlane/**", "**/build.gradle*"],
        "manual": ["the target's build: every shipped CI asset is ubuntu-latest and the gate "
                   "compiles nothing, so a build needing another OS or a device cannot be a "
                   "required check from here",
                   "complexity in any language but Python — the one check that would notice a "
                   "refactor making things worse"],
    },
    "data-pipeline": {
        "title": "Data pipeline development and modernization",
        "kinds": ["pipeline_authoring", "pipeline_modernization", "data_model_change",
                  "schema_migration", "destructive_data_operation"],
        "claims": "EQUIV for every migrated mapping, CONSTRAINT (freshness, cost, residency), NEG",
        "evidence": "T5 WITH A TOLERANCE, and record observed: — PASS alone cannot tell 99.98% "
                    "from exact",
        "relation": True,
        "fences": ["**/migrations/**", "**/*.ddl", "**/models/**", "**/dbt_project.yml"],
        "manual": ["lineage: nothing here knows about downstream consumer contracts, and for "
                   "pipeline work that IS the blast radius (ledger F-022)"],
    },
}

# Asked for by name and refused by name, with what a sibling would need. Pretending the loop
# covers this would be the most expensive kind of false assurance in the suite.
OUT_OF_SCOPE = {
    "dataops": (
        "Operating pipelines in production is OUT OF SCOPE. This loop governs the MERGE. If the "
        "chokepoint you need is the EXECUTION of a statement — DROP COLUMN, a backfill, a "
        "retention deletion, terraform apply — both PreToolUse hooks return 0 for anything that "
        "is not a git commit/push or a `gh pr` command, so those run entirely unobserved.\n"
        "    What a DataOps sibling would need that this has none of: a PRE-EXECUTION gate and "
        "interval verdicts. What would transfer: the register-of-intent-as-product idea, the four "
        "enforcement kinds, every-unknown-is-a-NO, the trust-ref rule, and the H1-H5 provenance "
        "shape with a data-provenance field set."),
}

# Extension -> profiles it is WEAK evidence for. Advisory only: a file extension cannot tell a new
# service from a rebuild of one, which is exactly the property/relation distinction that matters.
HINTS = {
    ".sql": ["data-pipeline"], ".R": ["data-pipeline"], ".ipynb": ["data-pipeline"],
    ".sas": ["data-pipeline", "modernization"], ".dtsx": ["data-pipeline", "modernization"],
    ".swift": ["refactoring", "new-language-service"],
    ".kt": ["refactoring", "new-language-service"],
    ".pbxproj": ["refactoring"], ".entitlements": ["refactoring"],
    ".go": ["new-language-service"], ".rs": ["new-language-service"],
    ".cob": ["legacy-escape", "modernization"], ".cbl": ["legacy-escape", "modernization"],
    ".jsp": ["modernization"], ".vb": ["modernization"],
}


def names():
    return sorted(PROFILES)


def derive(chosen):
    """(rc, result) for one or more profile names. Union of kinds, union of fences, ALL manual items.

    The manual items are unioned rather than summarised: two profiles in one repository means both
    sets of gaps apply, and dropping either is how a gap becomes invisible.
    """
    bad = [c for c in chosen if c not in PROFILES and c not in OUT_OF_SCOPE]
    if bad:
        return 2, {"error": f"unknown profile(s) {bad}; known: {names()}"}
    refused = [c for c in chosen if c in OUT_OF_SCOPE]
    if refused:
        return 2, {"error": "out of scope", "refusals": {c: OUT_OF_SCOPE[c] for c in refused}}
    kinds, fences, manual, titles, rel = [], [], [], [], []
    for c in chosen:
        p = PROFILES[c]
        titles.append(p["title"])
        kinds += [k for k in p["kinds"] if k not in kinds]
        fences += [f for f in p["fences"] if f not in fences]
        manual += [f"[{c}] {m}" for m in p["manual"]]
        rel.append(p["relation"])
    relation = True if any(r is True for r in rel) else (None if None in rel else False)
    return 0, {
        "profiles": list(chosen), "titles": titles, "kinds": kinds,
        "fences_to_confirm": fences, "manual": manual, "relation_work": relation,
        "claims": [PROFILES[c]["claims"] for c in chosen],
        "evidence": [PROFILES[c]["evidence"] for c in chosen],
    }


def detect(root="."):
    """Advisory profile suggestions from tracked file extensions. Never applied automatically."""
    import subprocess
    r = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    seen, score = set(), {}
    for line in r.stdout.splitlines():
        e = os.path.splitext(line)[1]
        if e in seen:
            continue
        seen.add(e)
        for p in HINTS.get(e, []):
            score[p] = score.get(p, 0) + 1
    return [p for p, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))]


def yaml_fragment(res):
    """The governance fragment, as text to MERGE rather than a file to overwrite.

    Printed, never written: the same discipline as merge_profile.py. A generator that edits
    governance.yaml in place is a generator that can quietly widen a fence.
    """
    L = ["# derived by work_profile.py — MERGE these, do not replace the file.",
         "# The kinds go in model_routing.tiers under the tier that suits each one; the router",
         "# refuses an unrecognised kind, so a kind named here must land in a tier.",
         "model_routing:",
         "  # confirm the tier for each: ordinary work at sonnet, work where the DIFFICULTY is",
         "  # deciding what 'the same' means at opus.",
         "  _kinds_this_repo_uses:"]
    L += [f"    - {k}" for k in res["kinds"]]
    L += ["risk:",
          "  # A CHECKLIST, not a claim. These are the surfaces this profile cares about; replace",
          "  # each glob with the path it actually has in THIS tree, then move it into risk.paths",
          "  # with its classes. A boundary that is not in risk.paths is unfenced, and every change",
          "  # through it looks small. (The key below is deliberately not `paths`: a generated list",
          "  # that reads like an answer retires the question.)",
          "  paths_to_confirm:"]
    L += [f"    \"{f}\": [arch_contract]" for f in res["fences_to_confirm"]] \
        or ["    # none beyond the shipped defaults"]
    return "\n".join(L)


def render(res):
    L = [f"[work-profile] {', '.join(res['titles'])}"]
    L.append(f"  kinds ({len(res['kinds'])}): {', '.join(res['kinds'])}")
    for c, cl in zip(res["profiles"], res["claims"]):
        L.append(f"  claims [{c}]: {cl}")
    for c, ev in zip(res["profiles"], res["evidence"]):
        L.append(f"  evidence [{c}]: {ev}")
    if res["relation_work"] is True:
        L.append("  THE CONTRACT IS A RELATION: EQUIV claims with a baseline, and T5 DIFFERENTIAL "
                 "evidence per claim. A property-only register cannot hold this work.")
    elif res["relation_work"] is None:
        L.append("  RELATION OR PROPERTY DEPENDS ON SCOPE: inside one module the existing tests "
                 "are the whole contract; at a boundary a consumer can observe, 'it still behaves "
                 "the same' is a RELATION and needs EQUIV + T5 however mechanical the diff looks.")
    if res["fences_to_confirm"]:
        L.append("  fences to CONFIRM against the real tree (a checklist, not a claim):")
        L += [f"      {f}" for f in res["fences_to_confirm"]]
    L.append("  STAYS MANUAL — printed every run, because a profile that hides its gaps misleads:")
    L += [f"      - {m}" for m in res["manual"]]
    return "\n".join(L)


def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    check("nine profiles are documented", len(PROFILES) == 8 and len(OUT_OF_SCOPE) == 1)
    # Every profile must name what stays manual, for the same reason invariant BB requires the row.
    for n, p in PROFILES.items():
        check(f"{n} names what stays manual", bool(p.get("manual")))
        check(f"{n} names at least one kind", bool(p.get("kinds")))
    rc, res = derive(["refactoring"])
    check("refactoring derives", rc == 0 and "refactor" in res["kinds"])
    check("refactoring is scope-dependent", res["relation_work"] is None)
    check("refactoring fences the build surface",
          any("pbxproj" in f or "lock" in f for f in res["fences_to_confirm"]))
    rc, res = derive(["modernization", "data-pipeline"])
    check("two profiles union their kinds",
          rc == 0 and "modernization" in res["kinds"] and "schema_migration" in res["kinds"])
    check("two profiles union their MANUAL items", len(res["manual"]) >= 2)
    check("either profile being relation work makes the result relation work",
          res["relation_work"] is True)
    rc, res = derive(["greenfield"])
    check("greenfield is property work", res["relation_work"] is False)
    # NEGATIVE CONTROLS.
    rc, res = derive(["dataops"])
    check("dataops is REFUSED, not derived", rc == 2 and "refusals" in res)
    check("the refusal says what a sibling would need",
          "PRE-EXECUTION" in res["refusals"]["dataops"])
    rc, res = derive(["not_a_profile"])
    check("an unknown profile is refused", rc == 2 and "unknown profile" in res["error"])
    rc, res = derive(["refactoring"])
    frag = yaml_fragment(res)
    check("the fragment says MERGE, not replace", "MERGE these, do not replace" in frag)
    check("the fragment marks the fences as to-confirm", "paths_to_confirm:" in frag)
    check("the fragment never emits a bare risk.paths key", "\n  paths:" not in frag)
    out = render(res)
    check("the rendering always prints the manual items", "STAYS MANUAL" in out)
    check("detect returns a list", isinstance(detect("."), list))
    print(f"[work-profile] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="print the profiles, one line each")
    ap.add_argument("--profile", nargs="+", help="one or more profile names")
    ap.add_argument("--detect", action="store_true", help="ADVISORY suggestion from the tree")
    ap.add_argument("--root", default=".")
    ap.add_argument("--yaml", action="store_true", help="print the governance fragment to merge")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.list:
        for n in names():
            print(f"  {n:22s} {PROFILES[n]['title']}")
        for n, why in OUT_OF_SCOPE.items():
            print(f"  {n:22s} OUT OF SCOPE — {why.splitlines()[0][:70]}...")
        return 0
    if a.detect:
        sug = detect(a.root)
        print("[work-profile] ADVISORY, from file extensions only: "
              + (", ".join(sug) if sug else "nothing suggested"))
        print("    A file extension cannot tell a NEW service from a REBUILD of one, which is the "
              "property-versus-relation distinction that decides the whole profile. Ask.")
        return 0 if sug else 1
    if not a.profile:
        ap.print_help()
        return 2
    rc, res = derive(a.profile)
    if rc:
        for r in (res.get("refusals") or {}).values():
            print(f"[work-profile] REFUSED: {r}", file=sys.stderr)
        if "error" in res and not res.get("refusals"):
            print(f"[work-profile] {res['error']}", file=sys.stderr)
        return rc
    if a.json:
        print(json.dumps(res, indent=2))
    elif a.yaml:
        print(yaml_fragment(res))
    else:
        print(render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
