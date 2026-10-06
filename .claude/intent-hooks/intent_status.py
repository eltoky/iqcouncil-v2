#!/usr/bin/env python3
"""What is actually ARMED in this repository, versus what CLAUDE.md DECLARES.

Why this exists
---------------
`intent-init` finishes by printing an ordered next-actions list: populate the register, build the
trace map, run the smoke tests, flip TRACE_REQUIRED, create the trust ref. That list is correct and
it is **Declared** enforcement — the weakest of the suite's four kinds — because the only thing
holding it is a sentence in a transcript the operator later closes.

The state that results is the suite's own worst failure mode, one level up from the install defect
of 1.16.0. CLAUDE.md announces an `## Intent-Governed Engineering Loop`, so every later agent
session reads that block and believes the loop is in force, while in fact:

  * `refs/intent-trust/governance` does not exist, so `model_route.py` reads the stage floors from
    the WORKING TREE — the branch under review supplies its own floors. Nothing in the suite ever
    created that ref, so this was true of every install by construction.
  * `INTENT_REGISTER.md` is still `UNINITIALIZED`, so no claim can be checked against anything.
  * `INTENT_TRACE.yaml` is absent and `TRACE_REQUIRED=false`, so the trace gate is off.
  * the code graph is absent, so blast-radius review is manual while the block may imply it is not.

A loop that reports armed while being off is worse than no loop, because the claim is what later
sessions act on. So this module converts that from Declared to Mechanical: one computation, three
consumers — the SessionStart hook, the CLAUDE.md capability line, and the merge gate.

Division of labour, which is the point of the design
----------------------------------------------------
**Tooling is initialized; content is authored.** `init_tools.py` arms everything that can be
derived mechanically — the runtime, the git hooks, the trust ref, the third-party tool state. It
never writes a claim. The register and the trace map come from a skill the operator chooses
(`intent-kickoff`, `intent-register-builder`), because inventing claims to look complete is an
anti-pattern the suite already refuses. This module is what makes the gap between the two visible
instead of forgotten.

Severity follows the house rule already used for `merge.enforcement`: a **note** where a human
decides, a **finding** where nothing human is in the loop. Under `hitl` an unfinished init is a
normal state during onboarding and the operator can see it. Under `hotl`/`hootl` nobody is looking,
so merging against an empty register or working-tree floors is not something to mention — it is
something to stop.

  python3 intent_status.py                      human-readable, exit 0
  python3 intent_status.py --json               machine-readable for the gate and the hook
  python3 intent_status.py --autonomous         assess as an unattended mode would
  python3 intent_status.py --quiet              print nothing when everything is armed
  python3 intent_status.py --decisions          the setup questions never asked (init_decisions)
  python3 intent_status.py --self-test

EXIT: 0 nothing blocking · 1 at least one blocking finding · 2 not a repository.
`--quiet` is for the session hook: a fully armed repo should say nothing at all, because a status
line that prints on every session when there is nothing wrong is a line people stop reading.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

GOV = "docs/intent/governance.yaml"
import govcfg                                        # same skill; installed alongside
TRUST_REF = govcfg.PINNED_REF                        # the last-resort candidate, spelled once
REGISTER = "docs/intent/INTENT_REGISTER.md"
TRACE = "docs/intent/INTENT_TRACE.yaml"
GUARD_CFG = ".claude/intent-hooks/intent-guard.config"
SETTINGS = ".claude/settings.json"
HOOKS_DIR = ".claude/intent-hooks"

# The aspects, in the order a reader wants them: the ones that decide whether a gate can be trusted
# come before the ones that decide how much it can see.
ASPECTS = ("runtime", "git_hooks", "trust_ref", "governance", "register", "trace_map",
           "spec_tool", "code_graph", "declaration", "decisions")

# The questions only a PERSON can answer, as ONE list (CLAUDE.md rule 24): intent-init asks them,
# `init_decisions` in governance.yaml records the answers, and this module reports any that were
# never asked. 2.1 had no record, so a re-run saw an existing governance.yaml and skipped the
# work-profile and notification questions — new in that release — without anyone deciding to.
DECISIONS = (
    ("source_of_truth", "Step 3b", "Which system is the source of truth for each artifact — this "
     "repository, a legacy system (Jira, ServiceNow…), or linkage between the two?"),
    ("merge_policy", "Step 4e.1", "How does this team work: who approves a merge, and which "
     "checks must pass before it?"),
    ("autonomy", "Step 4e.1b", "Where does the human stand: in the loop (hitl), on it (hotl), or "
     "out of it (hootl)?"),
    ("notices", "Step 4e2b", "Where should people be told when the loop is waiting on them: "
     "Slack, Teams, both, or nowhere yet?"),
    ("work_profile", "Step 4e3", "What kind of work will this repository do — greenfield, "
     "brownfield, modernization, legacy escape, rebuild, polyglot service, refactoring, data "
     "pipeline? More than one is normal."),
)


def sh(*a, cwd=None):
    return subprocess.run(a, capture_output=True, text=True, cwd=cwd)


# ---------------------------------------------------------------- collection (all the I/O)

def _settings_commands(root):
    """Hook commands actually registered with Claude Code, or None if unreadable.

    None and set() are different answers: an unreadable settings.json means we cannot tell whether
    the hooks will run, which is not the same as knowing they will not.
    """
    p = os.path.join(root, SETTINGS)
    if not os.path.exists(p):
        return set()
    try:
        d = json.load(open(p))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    out = set()
    for groups in (d.get("hooks") or {}).values():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if isinstance(g, dict):
                for h in g.get("hooks") or []:
                    if isinstance(h, dict) and h.get("command"):
                        out.add(h["command"])
    return out


def _guard_cfg(root):
    """Parse the shell-sourced guard config without sourcing it.

    Never `source` a repo-controlled file — the suite's own standing rule. A config is data.
    """
    out = {}
    p = os.path.join(root, GUARD_CFG)
    try:
        for line in open(p):
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def _register_stage(root):
    """UNINITIALIZED, ACTIVE-ish, or None when the file is absent.

    The scaffolded placeholder carries `stage: UNINITIALIZED`, which is honest and is exactly the
    state that must not be mistaken for a populated register.
    """
    p = os.path.join(root, REGISTER)
    if not os.path.exists(p):
        return None
    try:
        head = open(p).read(4000)
    except OSError:
        return None
    if "UNINITIALIZED" in head:
        return "UNINITIALIZED"
    return "POPULATED"


def _d(v):
    return v if isinstance(v, dict) else {}


def _shown(v):
    """A setting as the person wrote it: YAML reads an unquoted `off` as False."""
    return {True: "on", False: "off"}.get(v, v) if isinstance(v, bool) else v


def _answer(v):
    """The recorded answer as text, or None when nothing real was recorded (blank, or a list of
    blanks such as `[""]` or `[null]` left from a template)."""
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        items = [str(x).strip() for x in v if x is not None and str(x).strip()]
        return ", ".join(items) or None
    return None


def _diverges(key, ans, gov):
    """Where the recorded answer and the live configuration disagree, say how — rule 24 says one
    decision written in two places is two decisions, so the two are compared, never trusted apart."""
    word = ans.split()[0].lower().strip(",.;:()") if ans else ""
    mode = str(_d(gov.get("autonomy")).get("mode") or "")
    if key == "autonomy" and word in ("hitl", "hotl", "hootl") and mode and word != mode:
        return f"recorded {word}, but autonomy.mode is {mode}"
    on = str(_shown(_d(gov.get("loop_notices")).get("enabled"))).lower() in ("on", "true", "yes")
    if key == "notices" and word.startswith("none") and on:
        return f"recorded {ans}, but loop_notices is switched on"
    return None


def _inferred(key, gov):
    """What existing configuration already implies for a question never recorded — shown to be
    CONFIRMED, never taken as the answer (it may be a template default nobody chose)."""
    if key == "autonomy" and _d(gov.get("autonomy")).get("mode"):
        return f"autonomy.mode is {_d(gov.get('autonomy')).get('mode')}"
    if key == "merge_policy" and _d(gov.get("merge")).get("auto_merge") not in (None, ""):
        return f"merge.auto_merge is {_shown(_d(gov.get('merge')).get('auto_merge'))}"
    ln = _d(gov.get("loop_notices"))
    if key == "notices" and str(ln.get("enabled")).lower() in ("true", "yes", "on"):
        return "loop_notices is on: " + ", ".join(map(str, _d(ln.get("channels"))))
    return None


def decision_state(gov):
    """{key: (state, detail)} — answered | diverged (recorded, but the configuration now says
    otherwise: ask again) | inferred (to confirm) | pending (never asked)."""
    rec = _d(gov.get("init_decisions"))
    out = {}
    for key, step, _q in DECISIONS:
        ans = _answer(rec.get(key))
        if ans and _diverges(key, ans, gov):
            out[key] = ("diverged", _diverges(key, ans, gov))
        elif ans:
            out[key] = ("answered", ans)
        elif _inferred(key, gov):
            out[key] = ("inferred", _inferred(key, gov))
        else:
            out[key] = ("pending", step)
    return out


def _collect_decisions(root):
    """decision_state() for the working-tree governance file; None when there is no file (the
    governance aspect reports that), "UNREADABLE" when it does not parse."""
    if not os.path.exists(os.path.join(root, GOV)):
        return None
    g = govcfg.load(GOV, root=root, trusted=False)
    if "does not parse" in (g.why or ""):
        return "UNREADABLE"
    return decision_state(g.data)


def collect(root=".", detect=None):
    """Everything `assess` needs, as plain data. All I/O lives here so assess() stays pure."""
    if detect is None:
        try:
            import intent_detect as detect
        except Exception:
            detect = None

    def have_cli(n):
        return bool(detect and detect.cli_present(n))

    def have_tool(n):
        return bool(detect and (detect.have(n, repo=root) or detect.mcp_present(n)))

    cmds = _settings_commands(root)
    cfg = _guard_cfg(root)
    hooks = os.path.join(root, HOOKS_DIR)
    gitdir = sh("git", "rev-parse", "--git-path", "hooks", cwd=root)
    hooks_path = os.path.join(root, gitdir.stdout.strip()) if gitdir.returncode == 0 else ""
    claude_md = os.path.join(root, "CLAUDE.md")
    try:
        declared = "INTENT-LOOP:START" in open(claude_md).read()
    except OSError:
        declared = False
    return {
        "root": os.path.abspath(root),
        "runtime_scripts": sorted(f for f in os.listdir(hooks)) if os.path.isdir(hooks) else [],
        "registered_commands": None if cmds is None else sorted(cmds),
        "git_hooks": [h for h in ("pre-commit", "commit-msg")
                      if hooks_path and os.path.exists(os.path.join(hooks_path, h))],
        **_trust_state(root),
        "governance_worktree": os.path.exists(os.path.join(root, GOV)),
        "register": _register_stage(root),
        "trace_map": os.path.exists(os.path.join(root, TRACE)),
        "trace_required": (cfg.get("TRACE_REQUIRED") or "").lower() == "true",
        "enforce_level": cfg.get("ENFORCE_LEVEL") or "",
        "openspec_cli": have_cli("openspec"),
        "openspec_dir": os.path.isdir(os.path.join(root, "openspec")),
        "graph_tool": have_tool("graphify") or have_tool("code-review-graph") or have_cli("graphify"),
        "graph_built": os.path.isdir(os.path.join(root, "graphify-out"))
                       or os.path.isdir(os.path.join(root, ".code-review-graph")),
        "declared": declared,
        "decisions": _collect_decisions(root),
    }


# ---------------------------------------------------------------- assessment (pure)

def _runtime(s, add):
    scripts = s.get("runtime_scripts") or []
    if not scripts:
        add("runtime", "ABSENT", "no runtime in .claude/intent-hooks/ — nothing is installed",
            blocking=True)
        return
    cmds = s.get("registered_commands")
    if cmds is None:
        # Unreadable settings is an UNKNOWN, and an unknown is not a pass: we cannot say the hooks
        # will run. This is the state that shipped zero registered hooks and exited 0.
        add("runtime", "UNVERIFIED",
            f"{SETTINGS} could not be read, so whether the hooks are registered with Claude Code "
            f"cannot be determined", blocking=True)
        return
    if not any("intent-hooks" in c for c in cmds):
        add("runtime", "DEGRADED",
            "the runtime scripts are installed but NOT registered in .claude/settings.json, so "
            "Claude Code will never call them — re-run install_runtime.sh", blocking=True)
        return
    add("runtime", "ARMED", f"{len(scripts)} runtime scripts installed and registered")


def _git_hooks(s, add):
    have = s.get("git_hooks") or []
    if len(have) == 2:
        add("git_hooks", "ARMED", "pre-commit and commit-msg installed")
    elif have:
        add("git_hooks", "DEGRADED", f"only {', '.join(have)} installed", blocking=False)
    else:
        add("git_hooks", "ABSENT",
            "no git hooks — the commit guard and provenance trailer are unenforced locally "
            "(hooks are not committed, so every clone needs the installer run)", blocking=False)


def _trusted_ref(root):
    """The ref govcfg would read policy from here, or None. The ONE resolver, not a second one."""
    return govcfg.resolve_ref(GOV, root=root)


def _trust_state(root):
    """Three facts, one resolution. `trust_ref` is whether ANY candidate ref exists at all, so a
    ref that exists without carrying the governance file is told apart from no ref — the first is
    DEGRADED (fix the ref), the second ABSENT (create one)."""
    ref = _trusted_ref(root)
    exists = ref is not None or any(
        sh("git", "rev-parse", "--verify", "--quiet", c, cwd=root).returncode == 0
        for c in govcfg.candidates())
    return {"trust_ref": exists, "governance_on_trust": ref is not None,
            "trust_ref_name": ref or TRUST_REF}


def _trust_ref(s, add):
    """The one that is a live bypass rather than a missing nicety."""
    if s.get("trust_ref") and s.get("governance_on_trust"):
        add("trust_ref", "ARMED", f"{s.get('trust_ref_name', TRUST_REF)} carries {GOV}")
        return
    if s.get("trust_ref"):
        add("trust_ref", "DEGRADED",
            f"a trust ref exists but none carries {GOV}; every privileged reader sees an EMPTY "
            f"policy (model routing alone falls back to the working tree)",
            blocking=False, autonomous_blocking=True)
        return
    # A note while a human approves, a finding once nothing human is in the loop — the same
    # asymmetry as merge.enforcement, and for the same reason. Under hitl the approver is the
    # control and the floors only decide which model reviews; under hotl/hootl the floors ARE the
    # control, and reading them from the branch under review means the branch sets its own.
    add("trust_ref", "ABSENT",
        f"no trusted ref (origin/main, origin/master or {TRUST_REF}) carries {GOV}, so every "
        f"privileged reader sees an EMPTY policy and model_route.py falls back to the WORKING TREE "
        f"— a branch supplies its own floors. Push {GOV} to the protected branch, or for a "
        f"repository with no remote pin one: init_tools.py --trust-ref",
        blocking=False, autonomous_blocking=True)


def _governance(s, add):
    if s.get("governance_worktree"):
        add("governance", "ARMED", f"{GOV} present")
    else:
        add("governance", "ABSENT", f"no {GOV} — run intent-init", blocking=True)


def _register(s, add):
    st = s.get("register")
    if st == "POPULATED":
        add("register", "ARMED", "the register carries claims")
    elif st == "UNINITIALIZED":
        # Expected immediately after init: content is authored, not generated. A note while a
        # human decides; a finding when nothing human is in the loop, because an autonomous merge
        # judged against an empty register is judged against nothing.
        add("register", "DEGRADED",
            "the register is UNINITIALIZED — no claim can be checked. Choose the skill that "
            "authors it (intent-kickoff, or intent-register-builder from existing docs); "
            "initialization deliberately does not invent claims", blocking=False,
            autonomous_blocking=True)
    else:
        # Absent rather than UNINITIALIZED means the scaffold never ran, so the advice differs —
        # but the severity does not. One rule across every content gap: a note while a human
        # approves, a finding once nothing human is in the loop. Only the aspects that make the
        # gate itself untrustworthy or self-deadlocked block unconditionally (runtime, governance,
        # and a TRACE_REQUIRED that no map can satisfy).
        add("register", "ABSENT",
            f"no {REGISTER} at all — intent-init has not scaffolded this repository, so there is "
            f"nothing for a claim to be checked against", blocking=False,
            autonomous_blocking=True)


def _trace_map(s, add):
    if s.get("trace_map") and s.get("trace_required"):
        add("trace_map", "ARMED", "the trace map exists and TRACE_REQUIRED=true")
    elif s.get("trace_map"):
        add("trace_map", "DEGRADED",
            "the trace map exists but TRACE_REQUIRED=false, so nothing enforces it — flip it in "
            f"{GUARD_CFG}", blocking=False, autonomous_blocking=True)
    elif s.get("trace_required"):
        # Backwards of the usual gap and worse: the gate is armed against an artifact that is not
        # there, so it fails for a reason that reads as the author's fault.
        add("trace_map", "MISMATCH",
            f"TRACE_REQUIRED=true but {TRACE} does not exist — the gate will refuse every change "
            f"until the map is built (intent-trace-map)", blocking=True)
    else:
        add("trace_map", "ABSENT",
            "no trace map and TRACE_REQUIRED=false — claim-to-code traceability is off; build it "
            "with intent-trace-map, then flip the flag", blocking=False,
            autonomous_blocking=True)


def _spec_tool(s, add):
    if s.get("openspec_dir"):
        add("spec_tool", "ARMED", "openspec/ present")
    elif s.get("openspec_cli"):
        add("spec_tool", "DEGRADED",
            "the openspec binary is installed but this repo has no openspec/ — the spec gate is "
            "manual. Initialize it: init_tools.py --openspec", blocking=False)
    else:
        add("spec_tool", "ABSENT",
            "no OpenSpec — the spec stage is manual, as the CLAUDE.md block should say",
            blocking=False)


def _code_graph(s, add):
    if s.get("graph_built"):
        add("code_graph", "ARMED", "a code graph is built")
    elif s.get("graph_tool"):
        add("code_graph", "DEGRADED",
            "a graph tool is installed but no graph is built — blast-radius review is MANUAL. "
            "Build it: init_tools.py --graph", blocking=False)
    else:
        add("code_graph", "ABSENT", "no code graph tool — blast-radius review is manual",
            blocking=False)


def _declaration(s, add, armed_core):
    """The false-claim check, and the reason this module exists.

    CLAUDE.md is injected into every session deterministically. A block that announces the governed
    loop while the core is not armed does not merely fail to help — it actively misinforms every
    later session, including this one.
    """
    if not s.get("declared"):
        add("declaration", "ABSENT",
            "CLAUDE.md carries no INTENT-LOOP block, so no session is told to follow the loop",
            blocking=False)
        return
    if armed_core:
        add("declaration", "ARMED", "CLAUDE.md declares the loop and the core is armed")
        return
    add("declaration", "MISMATCH",
        "CLAUDE.md declares the governed loop but the core is NOT armed — every session reads "
        "that block and believes the loop is in force. A loop that reports armed while it is off "
        "is worse than no loop", blocking=False, autonomous_blocking=True)


def _decisions(s, add):
    """Unasked questions are a NOTE, never blocking: the loop runs on the configuration it has. But
    the note prints at every session start until someone answers, which is the point."""
    d = s.get("decisions")
    if d is None:
        return
    if d == "UNREADABLE":
        add("decisions", "UNKNOWN", f"{GOV} does not parse, so which setup questions were asked "
            f"is unknown")
        return
    pending = [f"{k} ({v[1]})" for k, v in d.items() if v[0] == "pending"]
    confirm = [f"{k} ({v[1]})" for k, v in d.items() if v[0] in ("inferred", "diverged")]
    if not pending and not confirm:
        add("decisions", "ARMED", "every setup question is answered in init_decisions")
        return
    add("decisions", "PENDING", "re-run intent-init to ask: "
        + "; ".join(filter(None, ["never asked: " + ", ".join(pending) if pending else "",
                                  "to confirm: " + ", ".join(confirm) if confirm else ""]))
        + "  (intent_status.py --decisions lists the questions)")


# The aspects whose absence means the loop cannot be trusted at all, as opposed to cannot see much.
CORE = ("runtime", "trust_ref", "governance")


def assess(state, autonomous=False):
    """(verdict, aspects, findings, notes) — pure, so it is testable without a repository."""
    aspects, findings, notes = {}, [], []

    def add(name, verdict, detail, blocking=False, autonomous_blocking=False):
        aspects[name] = {"verdict": verdict, "detail": detail}
        hard = blocking or (autonomous and autonomous_blocking)
        if verdict in ("ARMED",):
            return
        (findings if hard else notes).append([name.upper().replace("_", "-") + "-" + verdict,
                                              detail])

    _runtime(state, add)
    _git_hooks(state, add)
    _trust_ref(state, add)
    _governance(state, add)
    _register(state, add)
    _trace_map(state, add)
    _spec_tool(state, add)
    _code_graph(state, add)
    armed_core = all(aspects.get(c, {}).get("verdict") == "ARMED" for c in CORE)
    _declaration(state, add, armed_core)
    _decisions(state, add)

    if findings:
        verdict = "NOT-ARMED" if not armed_core else "DEGRADED"
    elif notes:
        verdict = "PARTIAL"
    else:
        verdict = "ARMED"
    return verdict, aspects, findings, notes


def summary_line(verdict, aspects):
    """One line for the CLAUDE.md capability block and the session hook."""
    bits = [f"{k}={aspects[k]['verdict'].lower()}" for k in ASPECTS if k in aspects]
    return f"intent-loop: {verdict} · " + " ".join(bits)


# ---------------------------------------------------------------- self-test

def _self_test_decisions(check, armed):
    # A re-run on a repo set up before init_decisions existed: nothing recorded, autonomy set.
    st = decision_state({"autonomy": {"mode": "hitl"}, "merge": {"auto_merge": "after_signoff"}})
    check("an unrecorded question is pending", st["work_profile"][0] == "pending"
          and st["notices"][0] == "pending")
    check("configuration already present is shown to CONFIRM, not taken as answered",
          st["autonomy"] == ("inferred", "autonomy.mode is hitl"))
    v2, _a2, f2, n2 = assess(dict(armed, decisions=st))
    check("unasked questions make the repo PARTIAL with a note, never blocking",
          v2 == "PARTIAL" and not f2 and any(c == "DECISIONS-PENDING" and "work_profile" in d
                                             for c, d in n2))
    st = decision_state({"init_decisions": {"source_of_truth": "repo", "merge_policy": "solo",
                                            "autonomy": "hitl", "notices": "none-yet",
                                            "work_profile": ["brownfield"]}})
    check("recorded answers are answered", all(v[0] == "answered" for v in st.values()))
    check("a list of blanks is not an answer",
          decision_state({"init_decisions": {"work_profile": ["", None]}})["work_profile"][0] == "pending")
    st = decision_state({"init_decisions": {"autonomy": "hitl (Frank)", "notices": "none-yet"},
                         "autonomy": {"mode": "hootl"}, "loop_notices": {"enabled": True}})
    check("a recorded answer the configuration now contradicts is asked again",
          st["autonomy"][0] == "diverged" and st["notices"][0] == "diverged")
    check("an unquoted `off` is shown as off, not False",
          decision_state({"merge": {"auto_merge": False}})["merge_policy"][1] == "merge.auto_merge is off")
    check("an empty list is not an answer",
          decision_state({"init_decisions": {"work_profile": []}})["work_profile"][0] == "pending")


def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    armed = {"runtime_scripts": ["a.py"], "registered_commands": [".claude/intent-hooks/x.py"],
             "git_hooks": ["pre-commit", "commit-msg"], "trust_ref": True,
             "governance_on_trust": True, "governance_worktree": True, "register": "POPULATED",
             "trace_map": True, "trace_required": True, "openspec_dir": True,
             "graph_built": True, "declared": True,
             "decisions": {k: ("answered", "x") for k, _s, _q in DECISIONS}}
    v, a, f, n = assess(armed)
    check("a fully armed repo is ARMED", v == "ARMED")
    check("a fully armed repo has no findings and no notes", not f and not n)
    _self_test_decisions(check, armed)

    # The state Frank's repo was in: declared, nothing armed.
    bare = dict(armed, trust_ref=False, governance_on_trust=False, register="UNINITIALIZED",
                trace_map=False, trace_required=False, openspec_dir=False, graph_built=False)
    v, a, f, n = assess(bare)
    v_aut, a_aut, f_aut, n_aut = assess(bare, autonomous=True)
    notes = {c for c, _ in n}
    aut_findings = {c for c, _ in f_aut}
    check("a declared-but-unarmed repo is not ARMED", v != "ARMED")
    check("the missing trust ref is a NOTE under hitl", "TRUST-REF-ABSENT" in notes)
    check("the missing trust ref is a FINDING under autonomy",
          "TRUST-REF-ABSENT" in aut_findings)
    check("the false declaration is a NOTE under hitl", "DECLARATION-MISMATCH" in notes)
    check("the false declaration is a FINDING under autonomy",
          "DECLARATION-MISMATCH" in aut_findings)
    check("a declared-but-unarmed repo is NOT-ARMED under autonomy", v_aut == "NOT-ARMED")

    # Severity follows the mode, exactly as merge.enforcement does.
    half = dict(armed, register="UNINITIALIZED", trace_map=False, trace_required=False)
    v1, _, f1, n1 = assess(half, autonomous=False)
    v2, _, f2, n2 = assess(half, autonomous=True)
    nc1 = {c for c, _ in n1}
    fc2 = {c for c, _ in f2}
    check("UNINITIALIZED register is a NOTE under hitl", "REGISTER-DEGRADED" in nc1)
    check("UNINITIALIZED register is a FINDING under autonomy", "REGISTER-DEGRADED" in fc2)
    check("an absent trace map is a note under hitl", "TRACE-MAP-ABSENT" in nc1)
    check("an absent trace map is a finding under autonomy",
          "TRACE-MAP-ABSENT" in fc2)
    check("hitl verdict is PARTIAL, not NOT-ARMED", v1 == "PARTIAL")
    check("autonomous verdict is DEGRADED once the core is armed", v2 == "DEGRADED")

    # A missing register is a content gap too: note under hitl, finding under autonomy.
    noreg = dict(armed, register=None)
    check("an absent register is a note under hitl",
          "REGISTER-ABSENT" in {c for c, _ in assess(noreg)[3]})
    check("an absent register is a finding under autonomy",
          "REGISTER-ABSENT" in {c for c, _ in assess(noreg, autonomous=True)[2]})
    # The three that block whatever the mode: the gate cannot function, or deadlocks itself.
    for st, aspect in ((dict(armed, runtime_scripts=[]), "RUNTIME-ABSENT"),
                       (dict(armed, governance_worktree=False), "GOVERNANCE-ABSENT"),
                       (dict(armed, trace_map=False, trace_required=True), "TRACE-MAP-MISMATCH")):
        check(f"{aspect} blocks even under hitl", aspect in {c for c, _ in assess(st)[2]})

    # An unreadable settings.json is an UNKNOWN, and an unknown is not a pass.
    v, a, f, n = assess(dict(armed, registered_commands=None))
    check("unreadable settings is UNVERIFIED", a["runtime"]["verdict"] == "UNVERIFIED")
    check("unreadable settings blocks", "RUNTIME-UNVERIFIED" in {c for c, _ in f})

    # Scripts present but not registered: the 1.16.0 settings bug, seen from the other side.
    v, a, f, n = assess(dict(armed, registered_commands=["/usr/bin/other"]))
    check("installed-but-unregistered hooks are DEGRADED",
          a["runtime"]["verdict"] == "DEGRADED")
    check("installed-but-unregistered hooks block", "RUNTIME-DEGRADED" in {c for c, _ in f})

    # TRACE_REQUIRED=true with no map is worse than both off.
    v, a, f, n = assess(dict(armed, trace_map=False, trace_required=True))
    check("TRACE_REQUIRED with no map is a MISMATCH", a["trace_map"]["verdict"] == "MISMATCH")
    check("TRACE_REQUIRED with no map blocks", "TRACE-MAP-MISMATCH" in {c for c, _ in f})

    # Not declared and not armed is honest, not a mismatch.
    v, a, f, n = assess(dict(bare, declared=False))
    check("an undeclared repo is not a false claim",
          a["declaration"]["verdict"] == "ABSENT")
    check("an undeclared unarmed repo still reports the trust ref",
          "TRUST-REF-ABSENT" in {c for c, _ in n})

    # A trust ref without governance on it is not an armed trust ref.
    v, a, f, n = assess(dict(armed, governance_on_trust=False))
    check("a trust ref lacking governance.yaml is DEGRADED",
          a["trust_ref"]["verdict"] == "DEGRADED")
    check("a half-pinned trust ref is a finding under autonomy",
          "TRUST-REF-DEGRADED" in {c for c, _ in
                                   assess(dict(armed, governance_on_trust=False),
                                          autonomous=True)[2]})

    # Partial git hooks degrade without blocking: they can only block, so a missing one grants
    # nothing it should not have.
    v, a, f, n = assess(dict(armed, git_hooks=["pre-commit"]))
    check("one git hook is DEGRADED but not blocking",
          a["git_hooks"]["verdict"] == "DEGRADED" and "GIT-HOOKS-DEGRADED" in {c for c, _ in n})

    # The summary line names every aspect it assessed.
    v, a, f, n = assess(armed)
    line = summary_line(v, a)
    check("the summary line carries every aspect", all(k in line for k in ASPECTS))

    # The config parser must not be fooled by comments, and must never source the file.
    import tempfile
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".claude/intent-hooks"), exist_ok=True)
    open(os.path.join(d, GUARD_CFG), "w").write(
        '# a comment with TRACE_REQUIRED="true" in it\n'
        'TRACE_REQUIRED="false"   # set true once the map exists\n'
        'ENFORCE_LEVEL="block"\n')
    cfg = _guard_cfg(d)
    check("a commented-out value is not read", cfg.get("TRACE_REQUIRED") == "false")
    check("an inline comment is stripped", cfg.get("ENFORCE_LEVEL") == "block")
    check("a missing config is empty, not an error", _guard_cfg(tempfile.mkdtemp()) == {})

    # Unreadable settings.json -> None (unknown), absent -> set() (known empty).
    check("absent settings is a known empty set", _settings_commands(tempfile.mkdtemp()) == set())
    d2 = tempfile.mkdtemp(); os.makedirs(os.path.join(d2, ".claude"), exist_ok=True)
    open(os.path.join(d2, SETTINGS), "w").write("{ not json")
    check("corrupt settings is None, not empty", _settings_commands(d2) is None)
    open(os.path.join(d2, SETTINGS), "w").write('["a list"]')
    check("a non-object settings is None", _settings_commands(d2) is None)

    print(f"[intent-status] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


def print_decisions(d):
    """The questions intent-init still has to ask, verbatim, with where each answer goes."""
    if d is None or d == "UNREADABLE":
        print(f"[intent-status] {GOV} is {'absent' if d is None else 'unreadable'} — run intent-init")
        return 1
    open_ = [(k, s, q, d[k]) for k, s, q in DECISIONS if d[k][0] != "answered"]
    if not open_:
        print("[intent-status] every setup question is answered (init_decisions)")
        return 0
    print(f"[intent-status] {len(open_)} setup question(s) to ask — record each answer under "
          f"init_decisions in {GOV}:")
    for k, step, q, (state, detail) in open_:
        tag = f"CONFIRM — {detail}" if state in ("inferred", "diverged") else "NEVER ASKED"
        print(f"  {k:<16} {step:<11} {tag}\n      {q}")
    return 1                           # questions open: a caller can check by exit code (rule 13)


def _report(a, verdict, aspects, findings, notes):
    if a.json:
        print(json.dumps({"verdict": verdict, "aspects": aspects, "findings": findings,
                          "notes": notes, "autonomous": a.autonomous}, indent=2))
        return 1 if findings else 0
    if a.line:
        print(summary_line(verdict, aspects))
        return 1 if findings else 0
    if a.quiet and verdict == "ARMED":
        return 0
    print(f"[intent-status] {summary_line(verdict, aspects)}")
    for code, detail in findings:
        print(f"  BLOCKING  {code}: {detail}")
    for code, detail in notes:
        print(f"  note      {code}: {detail}")
    if findings and not a.autonomous:
        print("  (an autonomous mode would treat the notes above as blocking too)")
    return 1 if findings else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--autonomous", action="store_true",
                    help="assess as an unattended mode would: notes become findings")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when everything is armed (for the session hook)")
    ap.add_argument("--line", action="store_true", help="print only the one-line summary")
    ap.add_argument("--decisions", action="store_true",
                    help="list the setup questions not yet answered in init_decisions")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.decisions:                    # before the git check: it reads one file and nothing else
        return print_decisions(_collect_decisions(a.root))
    if sh("git", "rev-parse", "--git-dir", cwd=a.root).returncode != 0:
        print(f"[intent-status] not a git repository: {a.root}", file=sys.stderr)
        return 2
    state = collect(a.root)
    verdict, aspects, findings, notes = assess(state, autonomous=a.autonomous)
    return _report(a, verdict, aspects, findings, notes)


if __name__ == "__main__":
    sys.exit(main())
