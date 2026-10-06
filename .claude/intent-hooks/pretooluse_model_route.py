#!/usr/bin/env python3
"""Claude Code PreToolUse hook — makes model routing MANDATORY rather than advisory.

Reads the tool call as JSON on stdin. Exit 2 blocks the call; stderr is fed back to the model.

Model routing used to be deterministic *once called* and called by nothing, which meant an agent
that routed by feel was never detected. This hook supplies the missing teeth: a subagent spawn is
blocked until a routing decision has been RECORDED for this session.

  blocked:  the Task tool (spawning a subagent) with no fresh record in the routing ledger
  blocked:  `claude --model X`, `--model X` on a spawn command, or an explicit model override in a
            shell command, when the recorded decision names a different model
  allowed:  everything else, always — this hook has one job

Governance (read from docs/intent/governance.yaml, working tree is fine here because a weaker
local policy cannot grant anything: the hook only ever blocks):
  model_routing.mandatory   true (default) enforces; false reverts to advisory
  model_routing.log         where decisions are recorded
  model_routing.record_ttl_minutes   how long a decision stays fresh (default 30)

What it does with each kind of bad input, precisely (an earlier version of this paragraph said it
"never blocks on an unreadable ledger", which stopped being true when the ledger stopped being
trusted — contradiction M1):
  * a tool call it cannot parse        → allowed: it is not a spawn the hook can recognise
  * no governance file at all          → allowed: not an intent-governed repository
  * an unreadable or malformed LEDGER  → "no fresh decision" → a subagent spawn is BLOCKED. The
    ledger is a file the agent writes; letting its corruption open the gate would let the agent
    open it (see fresh_record)
  * a malformed log path or TTL        → the same: no fresh decision, blocked
The commit-msg guard is the second line: it refuses an Assisted-by model that contradicts the
recorded decision.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, datetime

GOV = "docs/intent/governance.yaml"
DEFAULT_LOG = "docs/intent/governance/MODEL_ROUTING.log"
SPAWN_TOOLS = {"Task", "Agent"}


def cfg():
    """The routing policy, or None when this repository is not intent-governed.

    None and {} are different answers and conflating them was a real defect. This hook is installed
    once per machine, so it runs in EVERY directory the agent works in — including repositories that
    have nothing to do with the intent loop. Treating a missing governance file as "mandatory by
    default" blocked every subagent spawn in every unrelated repository, which contradicts this
    file's own promise never to block on a missing config and would make the machine unusable.

    So: no governance file -> None -> this is not our business, allow. A governance file that
    exists but cannot be read or parsed also allows, for the same reason the rest of this hook
    never fails closed on its own bugs.
    """
    if not os.path.exists(GOV):
        return None
    try:
        import yaml
        d = yaml.safe_load(open(GOV))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    mr = d.get("model_routing")
    if mr is None:
        return {}
    # `model_routing: yes` parses to a bool, `model_routing: some-string` to a str — and then every
    # `mr.get(...)` below raises AttributeError. A governance file that exists but whose routing
    # section is unusable is treated as an EMPTY section, which means mandatory-by-default: the
    # repository opted into the loop, so a malformed section must not read as an opt-out.
    return mr if isinstance(mr, dict) else {}


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


def unroutable(detail):
    """The hook cannot do its job. Say so once, on stderr, and ALLOW the call.

    This file's own docstring promises it never blocks on a missing config or its own bugs, and for
    one release it broke that promise in the loudest possible way: `routing_session.py` was not in
    the installer's SRC_LIST, so `from routing_session import ...` raised ModuleNotFoundError on
    EVERY Bash tool call in an armed repository. Claude Code reported "PreToolUse:Bash hook error"
    and ran the command anyway, so the gate was silently off while the transcript filled with
    tracebacks.

    Allowing is the right answer — routing is discipline, not safety, and a hook that bricks the
    repository when its own dependency is missing is worse than one that is off. But it is NOT a
    silent answer: a disarmed gate that says nothing is indistinguishable from a satisfied one.
    """
    print(f"[intent-loop/model] this hook is not functioning, so model routing is NOT being "
          f"enforced: {detail}", file=sys.stderr)
    print("  Re-arm it:  bash ~/.claude/skills/intent-init/scripts/install_runtime.sh", file=sys.stderr)
    sys.exit(0)


def fresh_record(mr):
    """The most recent decision for THIS session inside the TTL, or None.

    Every value read here is attacker-reachable and must be read defensively, because None is the
    answer that BLOCKS and an exception is the answer that allows. Two ways that went wrong:

      * the ledger is a file the agent is expected to append to. A line that is valid JSON but not
        an object — `0`, `[]`, `"x"` — made `r.get` raise AttributeError, which escaped to the
        top-level handler and became exit 0. One append to its own ledger turned the gate off for
        the session.
      * `log` and `record_ttl_minutes` come from the working-tree governance.yaml. A non-string log
        path or a non-numeric TTL raised TypeError/ValueError out of `open`/`int`, with the same
        result — which falsified this file's own claim that a weaker local policy cannot grant
        anything, since the hook only ever blocks.

    A malformed ledger or config now reads as "no fresh decision", which is what the OSError path
    has always done, so every failure in here agrees on blocking.
    """
    path = mr.get("log") or DEFAULT_LOG
    if not isinstance(path, str):
        return None                      # an unusable log path is not a fresh decision
    try:
        ttl = int(mr.get("record_ttl_minutes", 30) or 30)
    except (TypeError, ValueError):
        ttl = 30                         # an unreadable TTL falls back to the default, never to 0
    if ttl <= 0:
        ttl = 30                         # a zero or negative TTL would expire every decision
    now = datetime.datetime.now(datetime.timezone.utc)
    sid = session_id()
    best = None
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not isinstance(r, dict):
                    continue             # a JSON scalar is not a decision record
                if r.get("session") != sid:
                    continue
                try:
                    at = datetime.datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=datetime.timezone.utc)
                except Exception:
                    continue
                if (now - at).total_seconds() <= ttl * 60:
                    best = r
    except OSError:
        return None
    return best


def block(lines):
    for l in lines:
        print(l, file=sys.stderr)
    sys.exit(2)


def main():
    try:
        call = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    mr = cfg()
    if mr is None:                                  # not an intent-governed repository
        sys.exit(0)
    if not mr.get("mandatory", True) or not mr.get("enabled", True):
        sys.exit(0)

    tool = call.get("tool_name") or ""
    ti = call.get("tool_input") or {}
    rec = fresh_record(mr)

    # 1. spawning a subagent without a recorded decision
    if tool in SPAWN_TOOLS:
        if rec is None:
            block([
                "[intent-loop/model] BLOCKED: model routing is mandatory and no decision is recorded "
                "for this session.",
                "  Classify the work and record the decision first, then spawn:",
                "      python3 ~/.claude/skills/intent-init/scripts/model_route.py \\",
                "          --kind <kind> [--stage <stage>] [--lines N] [--files N] --record --announce",
                "  Print the returned line in the MAIN chat before forking, so the person can see "
                "which model is running and why.",
                "  Kinds and stage floors are in docs/intent/governance.yaml under model_routing.",
            ])
        # 2. the spawn must not contradict the recorded model
        want = rec.get("model")
        blob = json.dumps(ti)
        named = set(re.findall(r"\b(haiku|sonnet|opus|fable)\b", blob, re.I))
        named = {n.lower() for n in named}
        if named and want and want not in named:
            block([
                f"[intent-loop/model] BLOCKED: the recorded decision for this session is "
                f"'{want}', but this spawn names {sorted(named)}.",
                "  Routing is not advisory: re-run model_route.py for the work you are actually "
                "about to do, with --record, and spawn on what it returns.",
                f"  Recorded: {rec.get('announce','')}",
                "  Escalating a tier mid-task is allowed (--current <model>); silently taking a "
                "different one is not.",
            ])
        sys.exit(0)

    # 3. a shell command that spawns with an explicit model override
    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        m = re.search(r"--model[= ]\s*([A-Za-z0-9.\-]*?(haiku|sonnet|opus|fable)[A-Za-z0-9.\-]*)",
                      cmd, re.I)
        if m:
            asked = m.group(2).lower()
            if rec is None:
                block([
                    f"[intent-loop/model] BLOCKED: this command selects the '{asked}' model and no "
                    "routing decision is recorded for this session.",
                    "  Run model_route.py --kind <kind> --record --announce first.",
                ])
            if rec.get("model") != asked:
                block([
                    f"[intent-loop/model] BLOCKED: recorded decision is '{rec.get('model')}', the "
                    f"command asks for '{asked}'.",
                    f"  Recorded: {rec.get('announce','')}",
                    "  Re-route for the work you are actually doing, or escalate with --current.",
                ])
    sys.exit(0)


if __name__ == "__main__":
    # The outermost net. `block()` raises SystemExit, which must pass through untouched — catching
    # it here would turn every block into an allow, which is the one bug worse than the one this
    # guard exists to stop.
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:                                  # noqa: BLE001
        unroutable(f"{type(e).__name__}: {e}")
