#!/usr/bin/env python3
"""Who is "this session", for model routing. ONE definition, imported by both sides.

The routing ledger records a decision against a session, and the PreToolUse hook looks for a
decision from THIS session inside the freshness window. The decision is written by one process
(model_route.py) and read by another (the hook), so the two must compute the same answer from
different processes — and when they disagree, the hook blocks every subagent spawn and routing
becomes unusable rather than merely advisory.

It was defined twice, identically, in both files. That is the defect the two capability detectors
already taught this suite: a rule with two implementations has two behaviours eventually.

Resolution order:

  1. a session variable the harness provides      exact, and what actually happens in practice
  2. the POSIX session id                         stable across SIBLING processes — the parent pid
                                                  is NOT, which was the bug: `model_route --record`
                                                  and the hook run as separate children of the same
                                                  shell, so their parent pids differ and no record
                                                  ever matched
  3. "unscoped"                                   nothing identifies the session, so freshness is
                                                  bounded by the TTL alone

Step 3 is weaker on purpose, and the weakening is worth naming: with no session identity, a
decision recorded by a different agent in the same repository within the TTL will satisfy the hook.
The alternative — refusing to match anything — makes the hook block every spawn forever, which is
worse than a bounded window. On a machine where the harness provides a session variable, which is
the normal case, step 1 applies and the question does not arise.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os

SESSION_VARS = ("CLAUDE_SESSION_ID", "INTENT_AGENT_ID", "CLAUDE_CODE_CHILD_SESSION")


def session_id():
    for v in SESSION_VARS:
        val = os.environ.get(v)
        if val:
            return f"{v}={val}"
    try:
        return f"sid={os.getsid(0)}"
    except (AttributeError, OSError):          # not POSIX, or not permitted
        return "unscoped"


def _self_test():
    ok = fail = 0

    def chk(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL  {label}")

    saved = {v: os.environ.get(v) for v in SESSION_VARS}
    try:
        for v in SESSION_VARS:
            os.environ.pop(v, None)
        base = session_id()
        chk("a fallback identity is produced", bool(base))
        chk("the fallback is not the parent pid, which differs between siblings",
            "ppid=" not in base)

        # the property that matters: two sibling processes must agree
        import subprocess, sys
        here = os.path.dirname(os.path.abspath(__file__))
        code = ("import sys; sys.path.insert(0, %r); import routing_session as R; "
                "print(R.session_id())" % here)
        env = dict(os.environ)
        for v in SESSION_VARS:
            env.pop(v, None)
        a = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        b = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        chk("two sibling processes agree on the session identity "
            f"({a.stdout.strip()!r} vs {b.stdout.strip()!r})",
            a.stdout.strip() == b.stdout.strip() and bool(a.stdout.strip()))

        os.environ["CLAUDE_SESSION_ID"] = "abc123"
        chk("a harness session variable wins", session_id() == "CLAUDE_SESSION_ID=abc123")
        os.environ.pop("CLAUDE_SESSION_ID")
        os.environ["INTENT_AGENT_ID"] = "agent-7"
        chk("the next variable is used when the first is absent",
            session_id() == "INTENT_AGENT_ID=agent-7")
        os.environ["CLAUDE_SESSION_ID"] = "wins"
        chk("resolution order is respected", session_id() == "CLAUDE_SESSION_ID=wins")
        os.environ.pop("CLAUDE_SESSION_ID")
        os.environ["INTENT_AGENT_ID"] = ""
        chk("an empty variable is not an identity", session_id() != "INTENT_AGENT_ID=")
    finally:
        for v, val in saved.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val
    print(f"[routing-session] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(session_id())
