#!/usr/bin/env python3
"""The loop's stages — named, ordered, defined ONCE.

Before 2.0.0 a stage's identity was its row number in a prose table in CLAUDE.md (11, 12 or 13
rows depending on the copy, with no row for the plan gate), and `model_routing.stage_floor` keyed
its floors on those numbers (`8_intent_review`). Nothing could drive the loop, because nothing
could name where a change was.

This module is the list. The loop driver (intent_loop.py) walks it; model routing reads its
floors by these names; the CLAUDE.md table is CHECKED against it (names and order, by an
invariant) — it is written by hand, so its prose can say more than this list does. The numbered keys are still
accepted, as aliases, so a governance file written before 2.0.0 keeps its floors.

    stages.py            print the list
    stages.py --self-test
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import sys
from collections import namedtuple

# name       the stage's identity everywhere
# owner      the skill(s) that do the work
# actor      who acts: agent (a worker may claim it), human (the queue waits), ci (the forge acts)
# kind       the model-routing kind for the work, when an agent does it
# bound      True when "done" is bound to a commit: if the change's head moves, it is done no more
# on_fail    the stage the loop returns to when this stage's check FAILS (not merely "not yet")
# proves     what the driver checks before it records the stage done — the evidence, not a promise
Stage = namedtuple("Stage", "name owner actor kind bound on_fail proves")

STAGES = (
    Stage("intent", "intent-change-record (+ kickoff / register-builder)", "human", "spec_decision",
          False, None, "docs/intent/changes/<id>/intent.md says `Status: accepted` ON THE PROTECTED "
          "BRANCH — acceptance is a human act, recorded as a merge"),
    Stage("spec", "intent-spec-gate", "agent", "spec_delta", False, None,
          "SPEC_GATE_<id>.md carries an intent-report/1 block with gate: PASS or PASS-WITH-UPDATES"),
    Stage("plan", "intent-plan-gate", "agent", "spec_delta", False, None,
          "docs/intent/changes/<id>/plan.md is committed"),
    Stage("implement", "the implementing agent", "agent", "feature_implementation", False, None,
          "the change branch carries a commit, beyond the protected branch, that touches something "
          "outside docs/intent/"),
    Stage("test", "intent-smoke-tests", "agent", "test_authoring", True, "implement",
          "TESTS_<id>.md carries gate: PASS for the change's head commit"),
    Stage("review", "intent-pr-review + review lanes + intent-review-consolidator", "agent",
          "lane_consolidation", True, None,
          "a FIX_LEDGER for the change carries an intent-report/1 findings block"),
    Stage("fix", "systematic debugging | intent-input-feedback", "agent", "fix_ledger_work", True,
          None, "the fix ledger has no open finding"),
    Stage("intent_review", "intent-pr-review", "agent", "intent_review", True, "fix",
          "PR_INTENT for the change records verdict APPROVE or APPROVE-WITH-FOLLOW-UPS"),
    Stage("pr", "the forge", "agent", "config_edit", True, None,
          "a pull request number is recorded for the change"),
    Stage("merge_gate", "intent-merge-gate (the acceptance workflow then enforces it on the PR)",
          "agent", "intent_review", True, "fix",
          "MERGE_GATE_<id>.md records decision AUTO-MERGE or HUMAN-REVIEW (BLOCK sends it to fix)"),
    Stage("merged", "the forge (a human or the unattended path)", "ci", None, False, None,
          "the merge commit is reachable from the protected branch"),
    Stage("archive", "intent-archive-sync (after the post-merge actor)", "agent",
          "manifest_regen", False, None,
          "the fragment is assembled and INTENT_COMPLIANCE.md was regenerated after the merge"),
)
NAMES = tuple(s.name for s in STAGES)
TERMINAL = "archived"
BY_NAME = {s.name: s for s in STAGES}

# model_routing.stage_floor keys written before 2.0.0, by the prose-table row number.
FLOOR_ALIASES = {"5_pr_review": "review", "8_intent_review": "intent_review",
                 "10_hitl_merge_gate": "merge_gate"}


def canonical(name):
    """A stage name, accepting the pre-2.0 numbered floor keys. None for anything else."""
    n = FLOOR_ALIASES.get(name, name)
    return n if n in BY_NAME else None


def after(name):
    """The stage that follows `name`, or TERMINAL after the last."""
    i = NAMES.index(name)
    return NAMES[i + 1] if i + 1 < len(NAMES) else TERMINAL


def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        if not cond:
            print(f"  FAIL {label}")
    check("names are unique", len(set(NAMES)) == len(NAMES))
    check("every on_fail names an EARLIER stage",
          all(s.on_fail is None or NAMES.index(s.on_fail) < NAMES.index(s.name) for s in STAGES))
    check("every actor is known", all(s.actor in ("agent", "human", "ci") for s in STAGES))
    check("the plan gate is a stage", "plan" in NAMES and NAMES.index("plan") < NAMES.index("implement"))
    check("aliases resolve", canonical("8_intent_review") == "intent_review"
          and canonical("review") == "review" and canonical("nope") is None)
    check("after the last stage is terminal", after(NAMES[-1]) == TERMINAL)
    check("every stage says what proves it", all(s.proves for s in STAGES))
    print(f"[stages] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    import argparse
    _a = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    _a.add_argument("--self-test", action="store_true", help="check the list's own consistency")
    if _a.parse_args().self_test:
        sys.exit(_self_test())
    for i, s in enumerate(STAGES, 1):
        print(f"{i:>2}. {s.name:<14} {s.actor:<6} {s.owner}\n    done when: {s.proves}")
