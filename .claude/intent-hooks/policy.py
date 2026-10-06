#!/usr/bin/env python3
"""ONE map of what needs a human, ONE map of protected paths — and everything else derived.

    policy.py --show [--config FILE]      the derived view: human-required, fences, path classes
    policy.py --self-test

Why this module exists
----------------------
Before 2.0.0 the question "does this need a human?" was answered in FIVE places that drifted apart:
`model_routing.human_in_loop_for`, `merge.unattended.never_for`, each tier's `never_for`,
`autonomy.DEFAULT_HUMAN_CLASSES` and `unattended_eligibility.DEFAULT_NEVER`. Worse, the gate read
only two of them: `human_in_loop_for` said a governance change "never auto-merges, whatever
merge.auto_merge says", and nothing at merge time read it. And two path lists —
`arch_contract_paths` and `irreversible_paths` — shared 11 of their 13 globs, while the code's own
defaults differed from the template's, and the template won.

So there is one block, `risk:`, in governance.yaml:

    risk:
      human_required: [governance_change, neg_claims, ...]   # the ONE list
      unattended_only: [first_pr_of_change, hub_nodes]        # fence autonomy, need no human alone
      paths:                                                  # each glob ONCE, with its classes
        "infra/**": [arch_contract, irreversible]

and these rules:

  * A human-required class the gate can DETECT is automatically an unattended fence. Declaring that
    something needs a human and then merging it with nobody looking is no longer expressible.
  * A human-required class the gate CANNOT detect (a decision, not a diff) is reported as such —
    "declared, not checkable at merge" — never silently treated as enforced.
  * Model tiers keep their own `never_for`: that is the DIFFICULTY axis (which model may do the
    work), not the human axis, and the two must not be conflated again.

Legacy keys are still read when no `risk:` block exists, so a policy written before 2.0.0 keeps
working; when both exist, `risk:` wins and the legacy keys are reported as ignored.

This file is the single source; tools/sync_shared.py copies it into each skill that ships it.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import sys, json, argparse

MODULE_MARKER = "intent-loop/policy/1"

# ---------------------------------------------------------------- the defaults, in ONE place

DEFAULT_HUMAN_REQUIRED = [
    "governance_change", "register_writes", "security_findings", "neg_claims",
    "arch_contract_changes", "irreversible_change", "cutover", "destructive_data_operation",
    "schema_migration", "build_signing_change", "escalation_handling", "irreversible_decision",
]
DEFAULT_UNATTENDED_ONLY = ["first_pr_of_change", "hub_nodes"]

# Each glob ONCE, with every path class it belongs to.
DEFAULT_PATHS = {
    # design intent
    "docs/architecture/**": ["arch_contract"], "*.drawio": ["arch_contract"],
    "openspec/specs/**": ["arch_contract"], "docs/adr/**": ["arch_contract"],
    # deployment and traffic — the cut-over surface; a revert restores the file, not the traffic
    "infra/**": ["arch_contract", "irreversible"], "deploy/**": ["arch_contract"],
    "helm/**": ["arch_contract"], "k8s/**": ["arch_contract"],
    "terraform/**": ["arch_contract", "irreversible"], "*.tf": ["arch_contract", "irreversible"],
    "*.tfvars": ["arch_contract", "irreversible"],
    "**/Dockerfile": ["arch_contract"], "docker-compose*.yml": ["arch_contract"],
    "**/nginx*.conf": ["arch_contract", "irreversible"], "**/routes*": ["arch_contract", "irreversible"],
    "**/ingress*": ["arch_contract", "irreversible"],
    # feature flags: a one-line flip is a cut-over
    "config/flags*": ["arch_contract", "irreversible"],
    "**/feature_flags*": ["arch_contract", "irreversible"],
    "**/*.flags.yml": ["arch_contract", "irreversible"],
    # schema and data shape — not revertible by git
    "**/migrations/**": ["arch_contract", "irreversible"], "**/migrate/**": ["arch_contract", "irreversible"],
    "**/*.ddl": ["arch_contract", "irreversible"], "db/**": ["arch_contract"],
    "**/retention*": ["irreversible"], "**/backfill*": ["irreversible"],
    # platform and signing
    "**/*.entitlements": ["arch_contract", "signing"], "**/*.plist": ["arch_contract"],
    "**/*.pbxproj": ["arch_contract", "signing"], "**/*.mobileprovision": ["arch_contract", "signing"],
    "fastlane/**": ["arch_contract", "signing"], "**/keystore*": ["arch_contract", "signing"],
}

# How the GATE detects each class. A class absent from this map is a decision, not a diff: it
# shapes routing through --kind and cannot be checked at merge.
DETECTORS = {
    "governance_change": "self_protected",          # built in, not configurable (eligibility)
    "register_writes": "self_protected",
    "neg_claims": "register+report",
    "security_findings": "ledger",
    "arch_contract_changes": "paths:arch_contract",
    "irreversible_change": "paths:irreversible",
    "cutover": "paths:irreversible",
    "destructive_data_operation": "paths:irreversible",
    "schema_migration": "paths:irreversible",
    "build_signing_change": "paths:signing",
    "hub_nodes": "graph",
    "first_pr_of_change": "history",
}

# The fence names the eligibility gate implements, keyed by the detector that answers them. Several
# classes share a detector; each detector runs once.
FENCE_OF = {
    "register+report": "neg_claims", "ledger": "security_findings", "graph": "hub_nodes",
    "history": "first_pr_of_change", "paths:arch_contract": "arch_contract_changes",
    "paths:irreversible": "irreversible_change", "paths:signing": "build_signing_change",
}


def _d(node, key):
    got = node.get(key) if isinstance(node, dict) else None
    return got if isinstance(got, dict) else {}


def _l(v):
    return [str(x) for x in v] if isinstance(v, list) else None


class View:
    """The derived policy. Every consumer reads THIS, not the raw keys."""

    def __init__(self, human_required, unattended_only, paths, source, notes):
        self.human_required = human_required
        self.unattended_only = unattended_only
        self.paths = paths                        # {glob: [path_class, ...]}
        self.source = source                      # "risk" | "legacy" | "default"
        self.notes = notes

    def globs(self, path_class):
        return [g for g, cls in self.paths.items() if path_class in cls]

    def detectable(self):
        return [c for c in self.human_required + self.unattended_only if c in DETECTORS]

    def undetectable(self):
        return [c for c in self.human_required if c not in DETECTORS]

    def fences(self):
        """The unattended fences to apply: every detectable class, as the fence name the gate runs."""
        out = []
        for c in self.detectable():
            det = DETECTORS[c]
            name = FENCE_OF.get(det)
            if name and name not in out:
                out.append(name)
        return out

    def as_dict(self):
        return {"source": self.source, "human_required": self.human_required,
                "unattended_only": self.unattended_only, "fences": self.fences(),
                "undetectable_at_merge": self.undetectable(),
                "path_classes": {pc: self.globs(pc) for pc in ("arch_contract", "irreversible",
                                                               "signing")},
                "notes": self.notes}


def _legacy_paths(un):
    """{glob: classes} from the two pre-2.0 lists, or None if neither is set."""
    arch, irr = _l(un.get("arch_contract_paths")), _l(un.get("irreversible_paths"))
    if arch is None and irr is None:
        return None
    paths = {}
    for g in (arch if arch is not None else [g for g, c in DEFAULT_PATHS.items()
                                             if "arch_contract" in c]):
        paths.setdefault(g, []).append("arch_contract")
    for g in (irr if irr is not None else [g for g, c in DEFAULT_PATHS.items()
                                           if "irreversible" in c]):
        paths.setdefault(g, []).append("irreversible")
    for g, cls in DEFAULT_PATHS.items():                 # signing was never configurable before
        if "signing" in cls:
            paths.setdefault(g, []).append("signing")
    return paths


def _paths_from_risk(raw):
    if not isinstance(raw, dict):
        return None
    out = {}
    for g, cls in raw.items():
        c = _l(cls) if isinstance(cls, list) else ([str(cls)] if cls else [])
        out[str(g)] = c
    return out


def _or(v, default):
    return v if v is not None else list(default) if isinstance(default, (list, tuple)) else dict(default)


def _view_risk(risk, un, legacy_hil, legacy_never):
    """The 2.0.0 shape. Legacy keys beside it are ignored, and named so nobody thinks they apply."""
    legacy = (("model_routing.human_in_loop_for", legacy_hil),
              ("merge.unattended.never_for", legacy_never),
              ("merge.unattended.arch_contract_paths", un.get("arch_contract_paths")),
              ("merge.unattended.irreversible_paths", un.get("irreversible_paths")))
    ignored = [k for k, v in legacy if v is not None]
    notes = [f"risk: is set, so these legacy keys are IGNORED: {', '.join(ignored)}"] if ignored else []
    return View(_or(_l(risk.get("human_required")), DEFAULT_HUMAN_REQUIRED),
                _or(_l(risk.get("unattended_only")), DEFAULT_UNATTENDED_ONLY),
                _or(_paths_from_risk(risk.get("paths")), DEFAULT_PATHS), "risk", notes)


def _view_legacy(un, legacy_hil, legacy_never):
    """Before 2.0.0: the UNION of the two human lists, because either one meant "a human"."""
    hr = list(_or(legacy_hil, DEFAULT_HUMAN_REQUIRED))
    hr += [c for c in (legacy_never or []) if c not in DEFAULT_UNATTENDED_ONLY and c not in hr]
    # An explicit never_for may DROP the autonomy-only fences — they need no human by themselves.
    # It can no longer drop a human-required one: that is the list above.
    uo = list(DEFAULT_UNATTENDED_ONLY) if legacy_never is None else \
        [c for c in DEFAULT_UNATTENDED_ONLY if c in legacy_never]
    return View(hr, uo, _legacy_paths(un) or dict(DEFAULT_PATHS), "legacy",
                ["legacy policy keys in use; `risk:` is the single source from 2.0.0"])


def view(gov):
    """The derived policy for a governance document (any shape, including garbage)."""
    gov = gov if isinstance(gov, dict) else {}
    risk = _d(gov, "risk")
    un = _d(_d(gov, "merge"), "unattended")
    legacy_hil = _l(_d(gov, "model_routing").get("human_in_loop_for"))
    legacy_never = _l(un.get("never_for"))
    if risk:
        return _view_risk(risk, un, legacy_hil, legacy_never)
    if legacy_hil is not None or legacy_never is not None:
        return _view_legacy(un, legacy_hil, legacy_never)
    paths = _legacy_paths(un)
    return View(list(DEFAULT_HUMAN_REQUIRED), list(DEFAULT_UNATTENDED_ONLY),
                paths or dict(DEFAULT_PATHS), "legacy" if paths else "default", [])


# ---------------------------------------------------------------- merge mode, from ONE statement

PERMITTED = {"hitl": ("off", "after_signoff"), "hotl": ("unattended",), "hootl": ("unattended",)}


def implied_merge(mode, written):
    """(auto_merge, contradiction) — the mechanism the MODE implies, honouring an explicit choice.

    `autonomy.mode` is the single statement. Under hotl/hootl it implies `unattended`; under hitl it
    implies `after_signoff` unless `off` (a person presses merge) is written. Writing a mechanism
    the mode does not permit is two statements disagreeing — a contradiction the caller resolves
    DOWN to hitl. An unset `auto_merge` is no longer an error: the mode already said it.
    """
    w = "off" if written is False else str(written or "").strip()
    allowed = PERMITTED.get(mode)
    if allowed is None:
        return w or "", None
    if not w:
        return allowed[-1] if mode == "hitl" else allowed[0], None
    if w in allowed:
        return w, None
    return w, (f"autonomy.mode is {mode} but merge.auto_merge is {w!r}; {mode} permits "
               f"{' or '.join(allowed)}")


# ---------------------------------------------------------------- the autonomy mode, failing closed

def autonomy_or_hitl(cfg):
    """autonomy.resolve(cfg), or hitl with the reason when the resolver cannot be reached.

    A missing or broken autonomy.py must not read as "no mode restrictions apply" — that would make
    deleting a file a way to buy autonomy. Both gates used to carry their own copy of this; the two
    differed (one omitted `always_human_for`), which is how twins drift."""
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    try:
        import autonomy as AU
        return AU.resolve(cfg)
    except Exception as e:                                  # noqa: BLE001
        return {"declared": None, "effective": "hitl", "degraded": True,
                "autonomous_merge_permitted": False, "auto_merge": None,
                "always_human_for": list(DEFAULT_HUMAN_REQUIRED),
                "findings": [["AUTONOMY-UNREADABLE",
                              f"the autonomy mode could not be resolved ({e}); with no readable "
                              f"mode nothing merges without a human"]]}


# ---------------------------------------------------------------- self-test

def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    v = view({})
    check("defaults: governance needs a human", "governance_change" in v.human_required)
    check("defaults: detectable classes become fences",
          {"neg_claims", "security_findings", "arch_contract_changes", "irreversible_change",
           "build_signing_change", "hub_nodes", "first_pr_of_change"} <= set(v.fences()))
    check("decisions are reported as undetectable",
          set(v.undetectable()) == {"escalation_handling", "irreversible_decision"})
    check("each glob is listed once", len(v.paths) == len(set(v.paths)))
    check("migrations are both arch and irreversible",
          "**/migrations/**" in v.globs("arch_contract") and "**/migrations/**" in v.globs("irreversible"))

    # H3 · a human-required class is enforced at merge even if never_for omits it.
    v = view({"model_routing": {"human_in_loop_for": ["arch_contract_changes"]},
              "merge": {"unattended": {"never_for": ["neg_claims"]}}})
    check("legacy: human_in_loop_for becomes a fence", "arch_contract_changes" in v.fences())
    check("legacy: never_for is still honoured", "neg_claims" in v.fences())
    check("legacy: never_for may drop an autonomy-only fence", "first_pr_of_change" not in v.fences())
    check("legacy: never_for cannot drop a human-required fence",
          "irreversible_change" in view({"merge": {"unattended": {"never_for": []}}}).fences())

    # The risk block wins and says what it ignored.
    v = view({"risk": {"human_required": ["neg_claims"], "paths": {"x/**": ["arch_contract"]}},
              "merge": {"unattended": {"never_for": ["hub_nodes"]}}})
    check("risk: wins", v.source == "risk" and v.human_required == ["neg_claims"])
    check("risk: reports the ignored legacy keys", any("IGNORED" in n for n in v.notes))
    check("risk: paths are its own", v.globs("arch_contract") == ["x/**"])

    # Removing a class from THE list is the only way to stop needing a human for it.
    v = view({"risk": {"human_required": [], "unattended_only": []}})
    check("an empty human list fences nothing it does not list", v.fences() == [])

    # Garbage does not crash and does not loosen.
    for junk in (None, "x", {"risk": "nonsense"}, {"risk": {"human_required": "neg_claims"}}):
        v = view(junk)
        check(f"garbage {junk!r} keeps the default human list",
              "governance_change" in v.human_required)

    # Mode as the single statement.
    check("hotl implies unattended", implied_merge("hotl", None) == ("unattended", None))
    check("hitl implies after_signoff", implied_merge("hitl", None) == ("after_signoff", None))
    check("hitl honours an explicit off", implied_merge("hitl", "off") == ("off", None))
    check("YAML False is off", implied_merge("hitl", False) == ("off", None))
    check("a contradiction is reported", implied_merge("hitl", "unattended")[1] is not None)
    check("hotl with after_signoff is a contradiction", implied_merge("hotl", "after_signoff")[1])
    print(f"[policy] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--show", action="store_true", help="print the derived view")
    ap.add_argument("--config", help="read this file instead of the trust ref")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load(override=a.config)
    print(json.dumps({"trusted": L.trusted, "source": L.source, **view(L.data).as_dict()},
                     indent=2))
    return 0 if L.trusted else 1


if __name__ == "__main__":
    sys.exit(main())
