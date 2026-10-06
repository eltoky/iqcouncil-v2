#!/usr/bin/env python3
"""Deterministic model routing for the intent loop.

The agent does not pick a model by feel. It describes the task in the vocabulary of
governance.yaml's model_routing block, and THIS script decides the tier, prints the
reason, and prints the one-line activation notice that must appear in the main chat
before the subagent is forked.

Order of resolution (first match wins, and floors win over everything):
  1. stage_floor        — a governance stage never runs below its floor.
  2. never_for          — a tier that forbids a kind of work cannot be chosen for it.
  3. use_for            — the lowest tier that lists one of the task's kinds.
  4. size caps          — changed lines / files push a tier up, never down.
  5. default            — nothing matched.
  escalate_only: a --current tier is never lowered; the result is max(current, resolved).

Usage
  model_route.py --kind bug_fix --lines 120 --files 3
  model_route.py --kind architecture_change --stage intent_review --announce
  model_route.py --kind cleanup --lines 900            # size pushes haiku -> sonnet
  model_route.py --kind cleanup --current opus         # escalate_only keeps opus
  model_route.py --self-test
  model_route.py --kind K --record         # append the decision to the routing ledger. REQUIRED when
                                           # model_routing.mandatory is true: the PreToolUse hook
                                           # blocks a subagent spawn until a decision is recorded.
  model_route.py --kind K --untrusted      # read governance.yaml from the working tree, NOT the
                                           # pinned trust ref — a locally edited routing table is
                                           # not trustworthy for stage floors; for tests only
Exit codes: 0 routed · 2 config/usage error · 3 self-test failure.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, json, argparse, subprocess, datetime

GOV = "docs/intent/governance.yaml"
# The trust ref is resolved in ONE place, govcfg.resolve_ref(). Two different refs were
# both called "the trust ref" before 2.0.0, so routing floors could lag merged policy.
ORDER = ["haiku", "sonnet", "opus", "fable"]          # cheapest -> strongest; index IS the rank


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def load_cfg(path=None, trusted=True):
    """(policy, source) through govcfg, the ONE reader and the ONE trust ref.

    Routing is the documented exception: when no trust ref carries the policy, the working-tree copy
    is used and ANNOUNCED as untrusted, because routing is enforced by a hook that can only block.
    A locally edited table could route intent review to a cheaper model, which is why it is shouted.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    if path:
        L = govcfg.load(override=path)
        return L.data, path
    if trusted:
        L = govcfg.load()
        if L.trusted:
            return L.data, L.ref
        print(f"[model-route] WARNING: {L.why}. Using the working tree copy — NOT trustworthy for "
              f"stage floors.", file=sys.stderr)
    W = govcfg.load(trusted=False)
    if not W.data and "does not exist" in W.why:
        print(f"[model-route] ERROR: no {GOV}. Run intent-init first.", file=sys.stderr)
        sys.exit(2)
    return W.data, "working-tree"


def rank(m):
    if m not in ORDER:
        print(f"[model-route] ERROR: unknown model '{m}' (expected one of {ORDER})", file=sys.stderr)
        sys.exit(2)
    return ORDER.index(m)


def _named_floors(floors, stage):
    """Floors keyed by STAGE NAME (stages.py), with the pre-2.0 numbered keys read as aliases —
    so `8_intent_review` and `intent_review` are one floor, whichever spelling either side used.
    A key that is not a stage (a kind such as governance_change) is kept as it is."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import stages
        canon = lambda k: stages.canonical(k) or k                  # noqa: E731
    except Exception:                                               # noqa: BLE001
        # Without the stage list a name cannot be matched to a numbered key. Rule 1: the unknown
        # is not "no floor" — an unmatched stage gets the HIGHEST floor configured.
        named = dict(floors)
        if stage and stage not in named and named:
            named[stage] = max(named.values(), key=rank)
        return named, stage
    named = {}
    for k, v in floors.items():
        c = canon(k)
        if c not in named or rank(v) > rank(named[c]):
            named[c] = v
    return named, (canon(stage) if stage else stage)


def route(mr, kinds, stage=None, lines=0, files=0, current=None):
    """Pure function: config + task descriptor -> (model, [reasons]). No I/O, so it is testable."""
    tiers = mr.get("tiers", {}) or {}
    reasons = []
    chosen = None

    # 1. stage floor — the hard one. A gate's floor is not a suggestion.
    floor = None
    floors, stage = _named_floors(mr.get("stage_floor", {}) or {}, stage)
    if stage and stage in floors:
        floor = floors[stage]
        reasons.append(f"stage_floor[{stage}]={floor}")
    # a kind can carry its own floor when it is also a floor key (e.g. governance_change)
    for k in kinds:
        if k in floors and (floor is None or rank(floors[k]) > rank(floor)):
            floor = floors[k]
            reasons.append(f"stage_floor[{k}]={floor}")

    # 2+3. lowest tier that lists one of the kinds and forbids none of them
    for name in ORDER:
        t = tiers.get(name) or {}
        never = set(t.get("never_for") or [])
        blocked = never & set(kinds)
        if blocked:
            reasons.append(f"{name}.never_for blocks {sorted(blocked)}")
            continue
        hit = sorted(set(t.get("use_for") or []) & set(kinds))
        if hit:
            chosen = name
            reasons.append(f"{name}.use_for matches {hit}")
            break

    if chosen is None:
        chosen = mr.get("default", "sonnet")
        # An unrecognised kind is an UNKNOWN, and the suite's first rule is that an unknown is not
        # a pass. It used to be recorded as a bare reason — `no use_for match; default=sonnet` —
        # so `language_port`, `android_to_ios`, `schema_migration` and anything else an agent
        # invented routed to the default tier with `human_in_loop` absent from the record. The
        # only thing that recognised a 40,000-line port as serious was its line count, which the
        # comment below correctly calls a blast-radius proxy rather than a difficulty measure.
        #
        # The model stays at the default, because routing an unknown to the top tier would make a
        # typo expensive. What changes is that an unknown kind now REQUIRES A HUMAN and says so.
        # The remedy already existed elsewhere in this codebase: `unattended_eligibility.py`
        # refuses a `never_for` name no fence implements, loudly. This is the same move.
        reasons.append(f"KIND-UNKNOWN: no tier declares {sorted(set(kinds))} in use_for, so this "
                       f"work is unclassified; routed to default={chosen} and a human is required. "
                       f"Add the kind to model_routing.tiers, or use one that exists.")

    # 4. size caps push UP only, and never past size_ceiling. Volume is a blast-radius proxy,
    #    not a difficulty measure: a 900-line rename is wide, not hard, so it must not be able
    #    to buy the top tier. Reaching fable requires a KIND, never a line count.
    ceiling = mr.get("size_ceiling", "opus")
    while True:
        t = tiers.get(chosen) or {}
        cap_l, cap_f = t.get("max_changed_lines"), t.get("max_files")
        over = []
        if cap_l is not None and lines > cap_l:
            over.append(f"lines {lines}>{cap_l}")
        if cap_f is not None and files > cap_f:
            over.append(f"files {files}>{cap_f}")
        if not over or rank(chosen) >= min(rank(ceiling), len(ORDER) - 1):
            if over:
                reasons.append(f"size over {chosen} caps ({', '.join(over)}) but size_ceiling={ceiling} reached")
            break
        nxt = ORDER[rank(chosen) + 1]
        reasons.append(f"{chosen} size cap exceeded ({', '.join(over)}) -> {nxt}")
        chosen = nxt

    # floors and escalate_only both raise, never lower
    if floor and rank(floor) > rank(chosen):
        reasons.append(f"raised to floor {floor}")
        chosen = floor
    if current and mr.get("escalate_only", True) and rank(current) > rank(chosen):
        reasons.append(f"escalate_only: keeping current {current} (would have been {chosen})")
        chosen = current
    return chosen, reasons


def known_kinds(mr):
    """Every kind any tier declares. The vocabulary, as configured."""
    out = set()
    for t in (mr.get("tiers") or {}).values():
        if isinstance(t, dict):
            out |= set(t.get("use_for") or [])
    return out


def unknown_kinds(mr, kinds):
    """The kinds in this request that no tier declares. Empty means the vocabulary covers it."""
    return sorted(set(kinds) - known_kinds(mr))


def needs_human(mr, kinds, model, gov=None):
    """Does this WORK require a human, regardless of which model does it?

    This used to be read off the chosen tier (`fable.requires_human_in_loop`), which meant the only
    way to demand a human was to route to the most expensive model — so a two-line governance.yaml
    edit went to Fable. Sensitivity and difficulty are different axes: this function answers the
    first, the tier answers the second. A tier flag is still honoured if a config sets one.

    An UNKNOWN kind also answers yes. Work nothing in the configuration recognises has not been
    classified, and unclassified work is exactly what a human should look at — otherwise inventing
    a kind is a way to route any work to the default tier with nobody watching.
    """
    # THE human-required list (policy.view) when the whole policy is available, so routing and the
    # merge gate cannot disagree about what needs a human. With only the routing block — the
    # self-test and older callers — its legacy human_in_loop_for is used, as before.
    # Given only the routing block, it is wrapped so policy.view applies the SAME rule: its legacy
    # human_in_loop_for if it has one, otherwise the defaults — never "nothing". Reading an absent
    # list as empty silently stopped requiring a human for anything once the list moved to risk:.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import policy
    hil = set(policy.view(gov if gov is not None else {"model_routing": mr}).human_required)
    if hil & set(kinds):
        return True
    if unknown_kinds(mr, kinds):
        return True
    return bool(((mr.get("tiers") or {}).get(model) or {}).get("requires_human_in_loop"))


LEDGER = "docs/intent/governance/MODEL_ROUTING.log"


def session_id():
    """Delegates to routing_session.py — ONE definition, shared with the other side of the ledger.

    This was duplicated here and in the hook. Identical code in two places is the defect that the
    two capability detectors already cost this suite a day over, and the duplicate fallback (the
    parent pid) was wrong in both copies: sibling processes have different parents, so a recorded
    decision never matched and the hook blocked every spawn.
    """
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from routing_session import session_id as _sid
    return _sid()


def record_decision(mr, model, kinds, stage, reasons, hitl, line):
    """Append-only. The ledger is what makes routing checkable after the fact: without a record,
    'we routed this correctly' is an assertion, and the PreToolUse hook has nothing to verify."""
    path = (mr.get("log") or LEDGER)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({
                "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "session": session_id(), "model": model, "kinds": list(kinds), "stage": stage,
                "human_in_loop": hitl, "why": reasons[-1] if reasons else "", "announce": line,
            }, sort_keys=True) + "\n")
    except OSError as e:
        print(f"[model-route] WARNING: could not write {path}: {e}", file=sys.stderr)


def announce_line(model, kinds, reasons, hitl):
    """The line a human reads in the main chat. It names the BINDING reason, not the last one:
    when a stage floor did the raising, the floor is what the reader needs to see."""
    why = reasons[-1] if reasons else "default"
    if why.startswith("raised to floor"):
        why = next((r for r in reasons if r.startswith("stage_floor[")), why)
    tail = " · HUMAN-IN-LOOP REQUIRED (no auto-merge)" if hitl else ""
    return f"[intent-loop/model] {model} · task: {','.join(kinds)} · why: {why}{tail}"


SELF_TESTS = [
    # (kinds, stage, lines, files, current, expected)
    (["cleanup"], None, 10, 2, None, "haiku"),
    (["typo_fix"], None, 1, 1, None, "haiku"),
    (["cleanup"], None, 300, 2, None, "sonnet"),            # one step up: over haiku, inside sonnet
    (["cleanup"], None, 900, 2, None, "opus"),              # a 900-line "cleanup" is not a cleanup
    (["cleanup"], None, 10, 60, None, "opus"),              # files blow through two caps
    (["cleanup"], None, 999999, 9999, None, "opus"),         # size_ceiling: volume never buys fable
    (["bug_fix"], None, 120, 3, None, "sonnet"),
    (["feature_implementation"], None, 350, 20, None, "sonnet"),
    (["feature_implementation"], None, 2000, 90, None, "opus"),
    (["high_impact_bug_fix"], None, 50, 2, None, "opus"),
    (["architecture_change"], None, 10, 1, None, "opus"),
    (["hub_nodes", "bug_fix"], None, 5, 1, None, "opus"),    # sonnet.never_for excludes hub_nodes
    (["critical_architecture_decision"], None, 0, 0, None, "fable"),
    (["hard_brainstorming"], None, 0, 0, None, "fable"),
    (["cleanup"], "8_intent_review", 5, 1, None, "opus"),    # floor beats a trivial-looking diff
    (["cleanup"], "escalation", 1, 1, None, "opus"),
    (["bug_fix"], "5_pr_review", 10, 1, None, "sonnet"),
    (["cleanup"], "intent_review", 5, 1, None, "opus"),      # the stage NAME carries the same floor
    (["cleanup"], "merge_gate", 5, 1, None, "opus"),
    (["cleanup"], None, 5, 1, "opus", "opus"),               # escalate_only never downgrades
    (["security_findings"], None, 5, 1, None, "opus"),       # haiku forbids it
    (["unlisted_new_kind"], None, 5, 1, None, "sonnet"),     # default, never haiku
    # --- the reported defect: sensitive-but-simple work must NOT reach fable ---
    (["governance_change"], None, 3, 1, None, "opus"),       # a rule EDIT: opus review floor, not fable
    (["config_edit"], None, 6, 1, None, "sonnet"),           # an ordinary config edit
    (["escalation_handling"], None, 2, 1, None, "opus"),     # a procedure, not a design decision
    (["irreversible_change"], None, 40, 2, None, "opus"),    # implementing one is opus
    (["security_critical"], None, 20, 1, None, "opus"),      # fixing is opus; deciding is fable
    (["arch_contract_changes"], None, 10, 1, None, "opus"),
    # --- fable is reserved for DECISIONS ---
    (["design_decision"], None, 0, 0, None, "fable"),
    (["spec_decision"], None, 0, 0, None, "fable"),
    (["strategic_planning"], None, 0, 0, None, "fable"),
    (["tradeoff_analysis"], None, 0, 0, None, "fable"),
    (["irreversible_decision"], None, 0, 0, None, "fable"),
    (["security_architecture_decision"], None, 0, 0, None, "fable"),
    (["critical_architecture_decision"], None, 0, 0, None, "fable"),
    (["hard_brainstorming"], None, 0, 0, None, "fable"),
]


# (kinds, expected_human_in_loop) — the gate must hold at EVERY tier, not only at fable
HITL_TESTS = [
    (["governance_change"], True),      # sensitive, cheap to write: human yes, fable no
    (["security_findings"], True),
    (["neg_claims"], True),
    (["register_writes"], True),
    (["arch_contract_changes"], True),
    (["escalation_handling"], True),
    (["irreversible_decision"], True),
    (["irreversible_change"], True),    # DOING it, not only deciding to — see governance.yaml
    (["cutover"], True),
    (["destructive_data_operation"], True),
    (["cleanup"], False),
    (["bug_fix"], False),
    (["feature_implementation"], False),
    (["hard_brainstorming"], False),    # a decision needs thought, not necessarily a merge gate
    # An UNKNOWN kind requires a human. Unclassified work is exactly what a person should look at,
    # and before this an invented string routed to the default tier with `human_in_loop` false —
    # which is how `language_port` and `schema_migration` were treated as routine work.
    (["unlisted_new_kind"], True),
    (["bug_fix", "unlisted_new_kind"], True),   # one unknown among knowns still counts
]


def self_test(mr, gov=None):
    bad = 0
    for kinds, stage, lines, files, current, want in SELF_TESTS:
        got, why = route(mr, kinds, stage, lines, files, current)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {kinds} stage={stage} lines={lines} files={files} "
              f"current={current} -> {got}" + ("" if ok else f"  (expected {want}; {why})"))
    for kinds, want in HITL_TESTS:
        model, _ = route(mr, kinds)
        got = needs_human(mr, kinds, model, gov=gov)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} human-in-loop {kinds} on {model} -> {got}"
              + ("" if ok else f"  (expected {want})"))
    total = len(SELF_TESTS) + len(HITL_TESTS)
    print(f"[model-route] self-test: {total - bad}/{total} passed")
    return 3 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", action="append", default=[],
                    help="task kind from model_routing.tiers[*].use_for; repeatable")
    ap.add_argument("--stage", help="loop stage name (stages.py), e.g. intent_review; numbered keys are aliases")
    ap.add_argument("--lines", type=int, default=0)
    ap.add_argument("--files", type=int, default=0)
    ap.add_argument("--current", help="model already running; escalate_only never lowers it")
    ap.add_argument("--config", help="explicit governance.yaml (tests, CI)")
    ap.add_argument("--untrusted", action="store_true", help="skip the pinned-ref read")
    ap.add_argument("--announce", action="store_true", help="print only the main-chat activation line")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--record", action="store_true",
                    help="append the decision to docs/intent/governance/MODEL_ROUTING.log")
    a = ap.parse_args()

    cfg, src = load_cfg(a.config, trusted=not a.untrusted)
    mr = cfg.get("model_routing") or {}
    if not mr:
        print("[model-route] ERROR: governance.yaml has no model_routing block.", file=sys.stderr)
        sys.exit(2)
    if a.self_test:
        sys.exit(self_test(mr, gov=cfg))
    if not mr.get("enabled", True):
        print("[model-route] routing disabled in governance.yaml; the session model stands.")
        sys.exit(0)
    if not a.kind:
        print("[model-route] ERROR: at least one --kind is required.", file=sys.stderr)
        sys.exit(2)

    model, reasons = route(mr, a.kind, a.stage, a.lines, a.files, a.current)
    hitl = needs_human(mr, a.kind, model, gov=cfg)
    line = announce_line(model, a.kind, reasons, hitl)

    if a.record:
        record_decision(mr, model, a.kind, a.stage, reasons, hitl, line)

    if a.announce:
        print(line)
    elif a.json:
        print(json.dumps({"model": model, "reasons": reasons, "announce": line,
                          "requires_human_in_loop": hitl, "config_source": src}, indent=1))
    else:
        print(line)
        for r in reasons:
            print(f"  - {r}")
        print(f"  config: {src}")
        if hitl:
            print("  NOTE: this tier requires a human in the loop; it never auto-merges.")
    sys.exit(0)


if __name__ == "__main__":
    main()
