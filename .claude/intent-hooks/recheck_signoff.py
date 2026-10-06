#!/usr/bin/env python3
"""Re-check a human sign-off: does the decision stand up?

Runs AFTER a human approves or signs off, on the exact commit that would merge. Every check is
deterministic — the verdict is computed, never argued — so the agent can neither invent a block
nor be talked out of one. It may still raise an escalation on its own judgment, by invoking intent_escalate.py's
request subcommand, which routes the question to a manager; that is not a veto.

Usage:
  recheck_signoff.py --change ID --pr N                 # reads PR state via `gh pr view`
  recheck_signoff.py --change ID --pr-json state.json   # offline / tests
      [--ledger docs/intent/reports/FIX_LEDGER_<change-id>.md] [--report docs/intent/reports/PR_INTENT_<change-id>.md]

PR state JSON (the subset of `gh pr view --json` used):
  {"headRefOid": sha, "additions": n, "deletions": n,
   "commits": [{"oid": sha, "committedDate": iso}],
   "reviews": [{"author": {"login": x}, "state": "APPROVED", "submittedAt": iso, "commit": {"oid": sha}}],
   "statusCheckRollup": [{"name": x, "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": iso}]}

  recheck_signoff.py --change ID --pr N --unattended    # no approval expected (see below)
  recheck_signoff.py --change ID --pr N --no-escalate   # fail without raising an escalation
  recheck_signoff.py --change ID --pr N --no-override   # ignore local overrides (fork PRs)

UNATTENDED (`--unattended`): run only when unattended_eligibility.py has already said ELIGIBLE,
because this flag is what removes the approval requirement. It drops ONLY the three
approval-specific checks, which are meaningless with no approval to judge — stale approval, review
time versus diff size, approved-before-checks — and keeps every other one: failing checks on the
head commit, unresolved ledger rows, and provenance (H1-H4). The accountable human is the policy
owner recorded in governance.yaml, who certified the policy rather than this change.

Exit: 0 decision HOLDS, 3 decision FAILS (blocked or escalated), 4 AWAITING SIGN-OFF (no approval yet), 1 error.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
# The identity the coverage module must carry. Must equal intent_coverage.MODULE_MARKER;
# invariant 7d checks that they agree.
COVERAGE_MARKER = "intent-loop/coverage-denominator/1"
import os, re, sys, json, argparse, subprocess
from datetime import datetime

GOV = "docs/intent/governance.yaml"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # forge.py — the adapter — lives beside this script
GOVBASE = "docs/intent/governance"


def git(*a):
    r = subprocess.run(["git", *a], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def effective(sha="HEAD"):
    """The CODE commit: latest commit at or before `sha` touching anything outside the governance
    records. CI pushes escalation records onto the PR branch; those commits must neither count as
    'new code after the approval' nor move the commit a decision is bound to."""
    return git("log", "-1", "--format=%H", sha, "--", ".", f":(exclude){GOVBASE}") or ""


SELF_STATUS = "intent/acceptance"                  # the gate's own verdict
SELF_WORKFLOW = "intent-acceptance-trigger"        # the unprivileged trigger


def rollup(st):
    """Normalise GitHub's statusCheckRollup, which mixes two shapes: CheckRun (name, status,
    conclusion, completedAt) and StatusContext (context, state, startedAt). Exclude the gate's own
    earlier verdict and the trigger's runs — a review event starts a trigger run that always finishes
    after the review, and a previous failure is not evidence about this commit."""
    out = []
    for c in st.get("statusCheckRollup", []) or []:
        if c.get("__typename") == "StatusContext" or ("context" in c and "name" not in c):
            state = (c.get("state") or "").upper()
            done = state in ("SUCCESS", "FAILURE", "ERROR")
            n = {"name": c.get("context", "?"), "status": "COMPLETED" if done else "IN_PROGRESS",
                 "conclusion": state if done else None, "completedAt": c.get("startedAt") or c.get("createdAt")}
        else:
            n = {"name": c.get("name", "?"), "status": c.get("status"), "conclusion": c.get("conclusion"),
                 "completedAt": c.get("completedAt"), "workflow": c.get("workflowName", "")}
        if n["name"] == SELF_STATUS or n.get("workflow") == SELF_WORKFLOW:
            continue
        out.append(n)
    return out


def touches_code(sha):
    files = git("diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()
    return any(f and not f.startswith(GOVBASE + "/") for f in files) if files else True


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def autonomy_of(cfg):
    """The autonomy mode, failing closed to hitl — ONE implementation, in policy.py."""
    sys.path.insert(0, HERE)
    import policy
    return policy.autonomy_or_hitl(cfg)


def unattended_refusals(cfg, cfg_trusted, mode, policy_owner, aut):
    """Every reason --unattended must not be honoured, as lines, or [] when it may be.

    Pulled out of main() deliberately. main() measured F(96) on cyclomatic complexity, and the
    `signoff_recheck.enabled: false` bypass survived review because one early return among ninety
    branches reads as one more guard. These four preconditions are the ones that decide whether a
    human can be skipped at all, so they are worth being able to read in one screen — and worth
    being testable without constructing a whole pull request.

    The flag NEVER grants what the policy does not: it only declines to wait for an approval that
    the policy has already said is not required.
    """
    out = []
    if not cfg_trusted:
        out.append("--unattended refused: the governance file could not be read from the trust "
                   "ref, so the policy allowing it is the one the branch under review supplies. "
                   "An unknown policy grants nothing.")
    if mode != "unattended":
        out.append(f"--unattended refused: governance.yaml on the trust ref says "
                   f"auto_merge='{mode}'. The flag cannot grant what the policy does not.")
    if not policy_owner:
        out.append("--unattended refused: no merge.unattended.policy_owner — an unattended merge "
                   "with nobody accountable is not a merge we make.")
    # The named autonomy mode, from the same trusted configuration. auto_merge says HOW a merge
    # happens; the mode says where the human stands, and it carries the expiry, the accountable
    # owner and — for hotl — the notification channel without which "somebody is watching" is an
    # empty claim. Both must permit this.
    if not aut["autonomous_merge_permitted"]:
        out.append(f"--unattended refused: the autonomy mode resolves to '{aut['effective']}'"
                   + (f" (declared '{aut['declared']}')" if aut.get("declared") else "")
                   + ", under which a human decides this change.")
        out += [f"  {code}: {text}" for code, text in aut["findings"]]
    return out


def notify_send(cfg, *, change, commit, pr):
    """Deliver the human-on-the-loop notice. (code, report) — 0 delivered, 1 not, 3 not required.

    Fails closed if the notifier module itself cannot be reached, for the same reason the autonomy
    resolver does: deleting a file must not be a way to stop needing to tell anybody."""
    sys.path.insert(0, HERE)
    try:
        import notify as NT
        return NT.send(cfg, change=change, commit=commit, pr=pr)
    except Exception as e:                                  # noqa: BLE001
        return 1, {"detail": f"the notifier could not be run at all ({e})"}


def facts_file(a):
    """The facts document to read the forge through, when one was supplied and really exists.

    A path that was passed but is not there must read as "no facts file" rather than as a file,
    or the forge backend resolves to `file` and then finds nothing — which looks like a forge that
    answered with silence instead of one that was never asked.
    """
    p = getattr(a, "pr_json", None)
    return p if (p and os.path.exists(p)) else None


def enforcement_gate(cfg, aut, findings, facts_file=None):
    """Is the gate this verdict depends on actually enforced, or only declared?

    `merge.enforcement` was a DECLARED setting for the whole life of the suite — a written claim
    with nothing comparing it to reality. The dangerous direction is asymmetric, so the handling is:
    a claim of `required` that the forge does not honour is a FINDING, because every merge the team
    believed was gated could have gone through red. A forge that cannot answer is UNVERIFIED, which
    is a note here and a finding under an autonomous mode — a merge with no human looking has to
    know its gate is real.

    Fails closed on its own absence, like the other gates: a missing checker is not a pass.
    """
    sys.path.insert(0, HERE)
    autonomous = aut["effective"] in ("hotl", "hootl")
    try:
        import check_enforcement as CE
        import forge as FG
        # Same backend the rest of this script reads facts through, facts file included — asking
        # the forge a different way here would mean checking a different repository's protection
        # from the one whose request is being judged.
        protection = FG.Forge.from_config(cfg, facts_file=facts_file).protection()
        verdict, found, notes, facts = CE.compare(cfg, protection, autonomous=autonomous)
    except Exception as e:                                  # noqa: BLE001
        findings.append(("ENFORCEMENT-UNCHECKED",
                         f"whether the acceptance gate is actually enforced could not be "
                         f"determined at all ({e}); a claim that cannot be checked is not a claim"))
        return {"verdict": "UNCHECKED", "notes": []}
    findings += found
    return {"verdict": verdict, "notes": notes, "facts": facts}


def _coverage_module():
    """The coverage module, or raise. Identity checked, not assumed.

    It was named `coverage.py` until 1.22.0, which shadows the PyPI `coverage` package — a
    dependency of pytest-cov, so present on a great many machines. `import coverage` then resolved
    to that package and the first call into it raised AttributeError, which this gate reported as
    COVERAGE-UNCHECKED: fail-closed, but blaming the installed runtime for somebody else's
    package. Separated out so the ratchet does not charge coverage_gate for the check.
    """
    try:
        import intent_coverage as m
    except Exception as exc:                                     # noqa: BLE001
        # Guarded AT THE IMPORT NODE, which is what invariant Z2 requires and what rule 20 is
        # about: a caller wrapping the CALL is not the same as guarding the import, and the
        # strict reading is the one that survives the next refactor.
        raise ImportError(f"intent_coverage is not importable here: {exc}") from exc
    # Checked from OUR side, against a constant the caller owns. The first version asked the
    # module `m.is_ours(m)` — but you cannot ask a stranger whether it is yours: an impostor has
    # no `is_ours`, so the check raised AttributeError instead of refusing. UAT-L12 found it.
    if getattr(m, "MODULE_MARKER", None) != COVERAGE_MARKER:
        raise ImportError(f"intent_coverage resolved to {getattr(m, '__file__', '?')}, which is "
                          f"not this suite's module")
    return m


def coverage_gate(aut, findings, changed_files, root="."):
    """Did any tool actually measure the files in this change?

    The fifth and last of the silent passes. Every measuring gate used to report an empty
    measurement as a clean one — `0 functions on the security path`, `NOT-RUNNABLE-HERE` with exit
    0, "none detected mechanically" — so a change in a language nothing claimed sailed through
    every one of them looking green.

    `intent_coverage.py` ships in intent-smoke-tests and this is a CROSS-SKILL import, declared in
    invariant Z2 and guarded for the same reason `init_gate`'s is: the runtime is flat, so the
    import resolves in `.claude/intent-hooks/` and nowhere else.
    """
    sys.path.insert(0, HERE)
    autonomous = aut["effective"] in ("hotl", "hootl")
    if changed_files is None:
        # An unknown file list IS an unknown, but the severity follows the same asymmetry as the
        # rest of the suite rather than blocking every caller that does not pass --changed-files.
        # Under `hitl` a human is approving and can see the diff; under `hotl`/`hootl` nobody is,
        # and merging a change where nothing is known to have measured it is exactly what to stop.
        msg = ("the changed-file list was not supplied, so whether any tool measured this change "
               "cannot be determined — pass --changed-files")
        if autonomous:
            findings.append(("COVERAGE-UNCHECKED", msg))
        return {"verdict": "UNCHECKED", "notes": [("COVERAGE-UNCHECKED", msg)]}
    try:
        CV = _coverage_module()
    except Exception as e:                                  # noqa: BLE001
        installed = os.path.exists(os.path.join(root, ".claude/intent-hooks/intent_coverage.py"))
        runtime = os.path.isdir(os.path.join(root, ".claude/intent-hooks"))
        msg = (f"which tools claim the files in this change could not be determined ({e}); an "
               f"unmeasured change is not a measured one")
        if runtime and not installed:
            findings.append(("COVERAGE-UNCHECKED", msg + " — intent_coverage.py is missing from the "
                                                         "installed runtime"))
            return {"verdict": "UNCHECKED", "notes": []}
        return {"verdict": "UNCHECKED", "notes": [("COVERAGE-UNCHECKED", msg)]}
    cfg, source = CV.load(root)
    probs = CV.validate(cfg)
    if probs:
        # A malformed declaration IS blocking in every mode: unlike a missing file list, somebody
        # wrote this file and it does not parse, so no gate can rely on it and the repository
        # believes it has a denominator that it does not have.
        findings.append(("COVERAGE-MALFORMED",
                         f"docs/intent/coverage.yaml ({source}) is not conformant, so no gate can "
                         f"rely on it: {probs[0]}"))
        return {"verdict": "MALFORMED", "notes": []}
    found, notes = CV.assess(changed_files, cfg, autonomous=autonomous)
    findings += [tuple(f) for f in found]
    c = CV.classify(changed_files, cfg)
    return {"verdict": "UNCLAIMED" if found else "CLAIMED", "source": source,
            "languages": sorted(c["by_language"]), "unclaimed": c["unclaimed"],
            "notes": [tuple(n) for n in notes]}


def coverage_summary(cov):
    """One line for the record: what measured this change, and what did not."""
    if cov.get("verdict") in ("UNCHECKED", "MALFORMED"):
        return f"Coverage: **{cov['verdict']}**"
    langs = ", ".join(cov.get("languages") or []) or "nothing"
    un = cov.get("unclaimed") or []
    tail = f" · {len(un)} file(s) claimed by no declared language" if un else ""
    return f"Coverage: **{cov.get('verdict')}** · languages in this change: {langs}{tail}"


def changed_files_of(a):
    """The changed paths, or None when they were not supplied. None is an unknown, not an empty
    change — the distinction decides whether the coverage gate can say anything at all."""
    path = getattr(a, "changed_files", None)
    if not path:
        return None
    try:
        return [l.strip() for l in open(path) if l.strip()]
    except OSError:
        return None


def init_gate(aut, findings, root="."):
    """Is the loop this verdict is produced BY actually armed, or only declared?

    The same question as `enforcement_gate`, one level up. That one asks whether the acceptance
    check is really required; this asks whether the loop that produced the check exists at all —
    whether the trust ref the stage floors are read from was ever created, whether the register
    the claims are checked against carries any, whether CLAUDE.md declares a loop the repository
    does not have.

    Severity follows the same asymmetry, for the same reason: a human merging can see an
    unfinished onboarding, so it is a note. Nothing human in the loop means nobody can, and a
    merge judged against an UNINITIALIZED register is judged against nothing — so under `hotl` or
    `hootl` it is a finding. `intent_status.py` applies that rule; this function only passes the
    mode in and keeps the results.

    Fails closed on its own absence, like every other gate here: if the status checker cannot be
    reached, that is reported rather than assumed to mean the loop is fine.
    """
    sys.path.insert(0, HERE)
    autonomous = aut["effective"] in ("hotl", "hootl")
    try:
        import intent_status as IS
    except Exception as e:                                  # noqa: BLE001
        # `intent_status.py` ships in intent-init but the RUNTIME is flat — the installer copies
        # every script into one `.claude/intent-hooks/` directory, so `import intent_status` from
        # here resolves there and nowhere else. That makes this the suite's only real cross-skill
        # import, and it means absence has two quite different meanings:
        #
        #   * this repository HAS an installed runtime, and the module is missing from it — a real
        #     gap, exactly the 1.16.0 class of defect, so a finding.
        #   * this repository has no runtime at all (a development checkout, or the gate run
        #     straight out of the skills tree) — then `_runtime` below already reports
        #     RUNTIME-ABSENT as a finding, and repeating it here as a second finding would only
        #     punish running the gate from source.
        #
        # Never silently pass in either case: the note still names what could not be checked.
        installed = os.path.exists(os.path.join(root, ".claude/intent-hooks/intent_status.py"))
        runtime = os.path.isdir(os.path.join(root, ".claude/intent-hooks"))
        msg = (f"whether the intent loop is actually armed could not be determined ({e}); "
               f"a loop that cannot be checked is not a loop")
        if runtime and not installed:
            findings.append(("LOOP-UNCHECKED",
                             msg + " — intent_status.py is missing from the installed runtime, "
                                   "so re-run install_runtime.sh"))
            return {"verdict": "UNCHECKED", "notes": [], "line": "loop state unchecked"}
        return {"verdict": "UNCHECKED", "line": "loop state not checked (no installed runtime)",
                "notes": [("LOOP-UNCHECKED", msg + " — this repository has no installed runtime")]}
    verdict, aspects, found, notes = IS.assess(IS.collect(root), autonomous=autonomous)
    findings += [tuple(f) for f in found]
    return {"verdict": verdict, "notes": [tuple(n) for n in notes],
            "line": IS.summary_line(verdict, aspects)}


def enforcement_summary(enf):
    """One line for the record: was the gate this verdict rests on actually enforced."""
    checks = (enf.get("facts") or {}).get("required_checks")
    if checks is None:
        return f"Gate enforcement: **{enf['verdict']}** · the forge did not say what it enforces"
    return f"Gate enforcement: **{enf['verdict']}** · the forge requires {checks}"


def notice_gate(cfg, aut, findings, *, change, commit, pr):
    """Tell the human on the loop, BEFORE the merge, and make a failure to tell them a finding.

    Last of the gates deliberately: it is the only one with a side effect outside the repository,
    so it runs only once every other reason to stop has been ruled out. Sending a notice and then
    blocking on a failed check would train the reader to ignore the channel.

    Delivery is a PRECONDITION of the merge, not a consequence of it. A notice after the merge
    gives nobody the chance to intervene, which is the whole difference between a human on the loop
    and a human out of it. The cost is stated plainly in the handbook: when the notification path
    is down, hotl repositories stop merging. A repository that would rather keep merging unwatched
    is describing hootl, and should declare hootl.
    """
    if findings or aut["effective"] != "hotl":
        return None
    rc, notice = notify_send(cfg, change=change, commit=commit, pr=pr)
    if rc == 1:
        findings.append(("NOTICE-UNDELIVERED",
                         f"the human-on-the-loop notice could not be delivered, so nobody has been "
                         f"told this is about to merge: {notice.get('detail')}"))
    return notice


def mode_summary(aut, notice=None):
    """One line for the decision record: the mode that produced this verdict, and the facts that
    make it that mode rather than a claim about itself."""
    bits = [f"Autonomy mode: **{aut['effective']}**"]
    if aut.get("declared") and aut["declared"] != aut["effective"]:
        bits.append(f"declared `{aut['declared']}`, degraded")
    if aut.get("review_by"):
        bits.append(f"grant reviewed by {aut['review_by']}")
    if aut.get("notify_channel"):
        bits.append(f"notified at {aut['notify_channel']}")
    if notice and notice.get("token"):
        bits.append(f"notice delivered, token `{notice['token']}`")
    elif notice and notice.get("detail"):
        bits.append("NOTICE NOT DELIVERED")
    return " · ".join(bits)


def load_cfg():
    """(policy, trusted) — through govcfg, the ONE reader. Untrusted means EMPTY, never the
    working tree: the working tree is the branch under review, and every privileged decision below
    is gated on this file. Empty means the strict defaults; callers refuse to grant anything."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load()
    if not L.trusted:
        print(f"[recheck] WARNING: governance is UNTRUSTED here — {L.why}. Strict defaults apply "
              f"and nothing is granted.", file=sys.stderr)
    return L.data, L.trusted


def pr_state(a):
    """PR facts, via the forge adapter — the one place the loop talks to a hosting service.

    The checks below are written against the GitHub-native field names they were first proven
    against, so the adapter renders the neutral contract back into that shape. Nothing here knows
    which forge answered. `--pr-json` still accepts either shape, so existing fixtures keep working.
    """
    import forge as FG
    if a.pr_json:
        doc = json.load(open(a.pr_json))
        if FG.detect_shape(doc) == "neutral":
            probs = FG.validate(doc)
            if probs:
                raise SystemExit("[recheck] facts document is not conformant:\n   "
                                 + "\n   ".join(probs))
            return FG.to_github(doc)
        # A GitHub-shaped document goes through the SAME normalisation as a live query — only
        # standing approvals from trusted reviewers survive it. Returning it raw counted any
        # APPROVED review ever posted.
        return FG.to_github(FG.from_github(doc, change=a.change))
    # The ADAPTER is a trust decision, not a preference: whatever it names gets EXECUTED, and its
    # output becomes the approvals and checks this script then believes. Reading it from the working
    # tree let a pull request ship its own `forge.adapter`, have the trusted step run it, and
    # fabricate its own approval. It comes from the pinned ref, like every other privileged setting.
    gov, trusted = load_cfg()
    if not trusted:
        gov = {}                      # untrusted tree: no adapter, fall back to the built-in backend
    fg = FG.Forge.from_config(gov)
    facts = fg.facts(pr=int(a.pr), change=a.change)
    if facts is None:
        raise SystemExit(f"[recheck] cannot read PR {a.pr}: no forge backend could answer "
                         f"(backend: {fg.backend()}). Every forge-derived fact is unknown, so this "
                         f"is a BLOCK, not a pass.")
    probs = FG.validate(facts)
    if probs:
        raise SystemExit("[recheck] the forge returned a non-conformant facts document:\n   "
                         + "\n   ".join(probs))
    return FG.to_github(facts)


def ledger_open(path):
    """Ledger rows whose status is not terminal, as (id, severity, status) — through verdicts.py.

    A verbatim twin of this parser lived in unattended_eligibility.py; both now read the ledger
    through the ONE reader, which takes the structured intent-report/1 block when the ledger has
    one and the markdown table otherwise. A line that LOOKS like a row but does not parse is still
    REPORTED, not skipped: skipping silently removed a BLOCKER.

    Returns (rows, unparseable), or None when there is no ledger at all.
    """
    sys.path.insert(0, HERE)
    import verdicts
    text = verdicts.read_file(path)
    if text is None:
        return None
    rec = verdicts.facts(None, text)
    if rec["open_findings"] is None:
        return None
    bad = list(rec["unparseable_findings"]) + [p for p in rec["problems"]]
    return (rec["open_findings"], bad)


def approval_binding(last, rs, head):
    """Check 2: is the approval bound to the commit that would actually merge."""
    out = []
    if not rs.get("approved_stale_commit", True):
        return out
    approved = (last.get("commit") or {}).get("oid")
    approved_code = effective(approved) if approved and git("cat-file", "-t", approved) else approved
    if not approved:
        # An approval that names no commit cannot be shown to be current. Skipping the check
        # treated it as fresh; the contract validator refuses such a document for exactly this
        # reason, but the legacy GitHub shape never reaches the validator.
        out.append(("UNBOUND-APPROVAL",
                    "the approval is not bound to a commit, so it cannot be shown to "
                    "apply to the head — an approval that cannot be checked is not one"))
    elif approved_code != head:
        out.append(("STALE-APPROVAL", f"approved {last['commit']['oid'][:10]}, but the head is {head[:10]} — "
                    "commits landed after the approval and nobody approved them"))
    return out


def check_review_time(at, rs, st, lines):
    """Check 3: was the review unhurried, measured against the size of what it reviewed.

    `at` is the approval time and is never None here — the rule compares against it, and comparing
    a datetime with None is the TypeError that used to crash the gate.
    """
    commit_times = [ts(c["committedDate"]) for c in st.get("commits", [])
                    if c.get("committedDate") and touches_code(c.get("oid", ""))]
    # A commit with an unreadable date is dropped rather than compared, for the same reason.
    commit_times = [t for t in commit_times if t is not None]
    pushed = max((t for t in commit_times if t <= at), default=None)
    if lines is None:
        return [("SIZE-UNKNOWN",
                 "the forge did not report the diff size, so the review-time check "
                 "cannot be applied — an unknown is not a pass")]
    if not (pushed and lines):
        return []
    need = rs.get("min_seconds_per_100_lines", 60) * max(1, lines / 100)
    took = (at - pushed).total_seconds()
    if took < need:
        return [("RUBBER-STAMP", f"{lines} changed lines approved {int(took)}s after the last push "
                 f"(expected at least {int(need)}s for a genuine review)")]
    return []


def check_approved_before_checks(at, rs, checks):
    """Check 4: did the approval precede the evidence it was supposedly based on."""
    if not rs.get("approved_before_checks_finished", True):
        return []
    done = [(c["name"], ts(c["completedAt"])) for c in checks if c.get("completedAt")]
    late = [n for n, t in done if t is not None and t > at]
    pending = [c["name"] for c in checks if c.get("status") != "COMPLETED"]
    if late or pending:
        return [("APPROVED-BEFORE-CHECKS", "approved before these checks finished: "
                 + ", ".join(late + pending))]
    return []


def approval_findings(last, rs, head, st, lines, checks):
    """Everything wrong with the approval itself — checks 2, 3 and 4 — as a list of findings.

    Extracted from main(), which the complexity ratchet caught growing from F(89) to F(92) when the
    untimed-approval guard was added. Raising the baseline would have been the wrong move: main()
    is the gate's own decision path, it is the one function this project names as must-not-grow,
    and one early return among ninety branches is how the `signoff_recheck.enabled` bypass survived
    review. So the block moved out instead — a down payment on ledger item F-002 — and judging an
    approval is now testable without constructing a whole pull request.

    Split three ways because the first extraction landed at D(29) and new code on the security path
    starts at C or better. The ceiling applies to the person who wrote the ceiling.
    """
    out = []
    at = ts(last["submittedAt"])
    if at is None:
        # The contract now requires submitted_at, but the LEGACY GitHub shape never reaches the
        # validator, so this is still arrivable. It used to fall through to `t <= at` against None
        # and raise TypeError — a traceback where rule 1 asks for a finding. Fail-closed by
        # accident is not fail-closed.
        out.append(("UNTIMED-APPROVAL",
                    "the approval carries no readable submission time, so neither its age nor the "
                    "review time can be judged — an approval that cannot be checked is not one"))
    out += approval_binding(last, rs, head)     # independent of the timestamp
    if at is not None:
        out += check_review_time(at, rs, st, lines)
        out += check_approved_before_checks(at, rs, checks)
    return out


def _is_off(v):
    """Rule 5: a scrutiny switch is OFF only when it plainly says so. A typo is ON."""
    return v is False or (isinstance(v, str) and v.strip().lower() in ("false", "no", "off", "0"))


def ledger_findings(parsed, last, rs, disabled):
    """Check 6 — the review's corrections were not made — plus the rubber-stamp signal it carries.

    OPEN-FINDINGS is ALWAYS a finding. `approved_with_open_blockers` used to switch that check off,
    so one scrutiny flag waived the gate on unresolved findings (CLAUDE.md rule 4; UAT-E09). It is
    now what governance.yaml has always called it: a RUBBER-STAMP SIGNAL. On (the default), an
    approval given while a BLOCKER is still open is an additional finding about the approval
    itself — an approver who signs over an open blocker did not read the ledger. Off, only that
    signal goes; the open findings still block. Like checks 2-4 it judges an approval, so it is
    skipped with no approval and with approval scrutiny switched off.
    """
    if parsed is None:
        return [("NO-LEDGER", "no fix ledger found — cannot show the review's findings were resolved")]
    out = []
    open_rows, unparseable = parsed
    if unparseable:
        out.append(("LEDGER-UNPARSEABLE",
                    f"{len(unparseable)} line(s) look like findings but do not parse, so "
                    f"they cannot be shown resolved: {unparseable[0]}"))
    blockers = [r for r in open_rows if r[1] == "BLOCKER"]
    if open_rows:
        out.append(("OPEN-FINDINGS", f"{len(open_rows)} ledger item(s) not resolved"
                    + (f", including {len(blockers)} BLOCKER" if blockers else "") + ": "
                    + ", ".join(f"{i} {s} {status}" for i, s, status in open_rows[:6])))
    if blockers and last and not disabled and not _is_off(rs.get("approved_with_open_blockers", True)):
        who = (last.get("author") or {}).get("login", "?")
        out.append(("RUBBER-STAMP", f"approved by {who} while {len(blockers)} BLOCKER(s) were still "
                    f"open in the fix ledger: " + ", ".join(i for i, _, _ in blockers[:6])))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--change", required=True); ap.add_argument("--pr")
    ap.add_argument("--pr-json"); ap.add_argument("--ledger"); ap.add_argument("--report")
    ap.add_argument("--changed-files",
                    help="file with one changed path per line. Without it the coverage gate "
                         "reports UNCHECKED rather than assuming nothing changed")
    ap.add_argument("--no-escalate", action="store_true")
    ap.add_argument("--comments-json"); ap.add_argument("--no-override", action="store_true",
                    help="ignore local overrides (fork PRs: an override cannot be attributed to a team member)")
    ap.add_argument("--unattended", action="store_true",
                    help="no approval is expected; requires auto_merge=unattended AND a prior ELIGIBLE verdict")
    a = ap.parse_args()
    cfg, cfg_trusted = load_cfg()
    rc = cfg.get("signoff_recheck") or {}
    rs = rc.get("rubber_stamp") or {}
    disabled = not rc.get("enabled", True)
    # Policy BEFORE the forge. These four preconditions decide whether a human may be skipped at
    # all, and none of them need a single fact about the request — so checking them first means a
    # repository that may not merge unattended never queries the forge, and the refusal says
    # "your policy does not allow this" instead of "the forge did not answer".
    policy_owner = (((cfg.get("merge") or {}).get("unattended") or {}).get("policy_owner") or "").strip()
    aut = autonomy_of(cfg)
    # The mechanism the MODE implies (policy.implied_merge, via autonomy.resolve): `autonomy.mode`
    # is the single statement, so hotl with no auto_merge written means unattended here exactly as
    # it does in the workflow. A contradiction resolves to hitl inside `aut`, which refuses below.
    mode = aut.get("auto_merge") or (cfg.get("merge") or {}).get("auto_merge") or "after_signoff"
    if a.unattended:
        refusals = unattended_refusals(cfg, cfg_trusted, mode, policy_owner, aut)
        if refusals:
            for line in refusals:
                print(f"[recheck] {line}", file=sys.stderr)
            return 1

    st = pr_state(a)
    checks = rollup(st)
    # GitHub's head is the latest commit of ANY kind; judge the latest CODE commit instead.
    head = effective() or st.get("headRefOid", "")
    # Size is OPTIONAL in the forge contract. Absent means unknown, and unknown is not zero: as zero
    # it silently switched the rubber-stamp fence off, because `if pushed and lines` is then false.
    def _int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None
    add, dele = _int(st.get("additions")), _int(st.get("deletions"))
    lines = None if add is None or dele is None else add + dele
    approvals = [r for r in st.get("reviews", []) if r.get("state") == "APPROVED"]
    findings = []

    # 1. is there a human sign-off at all? If not, the PR is waiting for review — that is a normal
    #    state, not a failed decision, so nothing is escalated. Exit 4 = pending.
    #    --unattended removes that wait, but only when the protected config really says unattended:
    #    the flag alone must never be able to skip a human, or passing it would BE the bypass.
    if not approvals and a.unattended:
        approvals = []          # explicit: there is no approval and none is expected
    elif not approvals:
        # NOTE: this runs even when signoff_recheck.enabled is false. Disabling the re-check turns
        # off SCRUTINY of a sign-off; it does not waive the sign-off. Returning 0 here would make a
        # config flag into a bypass with no eligibility fences and no accountable owner — strictly
        # worse than `unattended`, and undocumented. Only --unattended may waive an approval.
        out = f"docs/intent/reports/RECHECK_{a.change}.md"
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").write(f"# Sign-off re-check — {a.change}\n\n**Verdict: AWAITING SIGN-OFF** \u2014 no human approval yet.\n")
        print(f"[recheck] AWAITING SIGN-OFF for {a.change} \u2014 no human approval yet; nothing to re-check")
        return 4
    last = max(approvals, key=lambda r: ts(r["submittedAt"])) if approvals else None

    # `signoff_recheck.enabled: false` switches off SCRUTINY OF THE APPROVAL — checks 2, 3 and 4,
    # the ones that judge whether a human really reviewed. It is not a merge bypass. Returning here
    # skipped the failing-check, unfinished-check, open-ledger and provenance checks as well, so one
    # config flag waived every gate at once — strictly worse than `unattended`, with no fences and
    # nobody accountable. Same shape as the 1.4.1 fix, one layer deeper.
    if disabled:
        who0 = (last or {}).get("author", {}).get("login", "?")
        print(f"[recheck] approval scrutiny is switched off in governance.yaml — the sign-off by "
              f"{who0} is taken at face value. The merge gates below still apply.")

    # Checks 2-4 judge an approval. With no approval they are not "passed", they are inapplicable —
    # recorded as such in the report so an unattended merge never reads as a fully-reviewed one.
    if last and not disabled:
        findings += approval_findings(last, rs, head, st, lines, checks)

    # 5. checks failing on the signed commit
    failing = [c["name"] for c in checks
               if c.get("status") == "COMPLETED" and c.get("conclusion") not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
    if failing:
        findings.append(("CHECKS-FAILING", "failing on the head commit: " + ", ".join(failing)))

    # 5b. checks that have not FINISHED on the commit being merged.
    #
    # In the attended path an unfinished check is already caught by APPROVED-BEFORE-CHECKS above:
    # the approval necessarily predates the result. Unattended merging has no approval, so that
    # check is correctly dropped — and nothing replaced it, which left a real hole: an eligible
    # change could merge with its tests still running. An unfinished check is an UNKNOWN result,
    # and the whole unattended design rests on every unknown being a NO.
    #
    # Deliberately not applied to the attended path, where the earlier finding covers it and the
    # merging human can see the request's own check list.
    if a.unattended:
        # No check list at all is indistinguishable from "everything passed" unless it is named.
        if not checks:
            findings.append(("CHECKS-UNKNOWN",
                             "the forge reported no checks whatsoever. That is not the same as "
                             "everything passing, and an unattended merge cannot tell the two "
                             "apart — so it refuses"))
        unfinished = [c["name"] for c in checks if c.get("status") != "COMPLETED"]
        if unfinished:
            findings.append(("CHECKS-UNFINISHED",
                             "these checks have not finished on the head commit, so their result is "
                             "unknown — an unattended merge needs every check to have PASSED, not "
                             "merely to have been started: " + ", ".join(unfinished)))

    # 6. the review's corrections were not made
    findings += ledger_findings(ledger_open(a.ledger), last, rs, disabled)

    # 7. provenance (hard rules H1-H4)
    if a.report:
        # --range as well as --report: without it only the PR's Provenance SECTION was checked, so
        # H1 (every agent commit carries Assisted-by) and H5 (no agent-added Signed-off-by) never
        # ran at the gate at all, while the output claimed "provenance (H1-H4)".
        import govcfg
        base = govcfg.resolve_ref() or "refs/remotes/origin/main"
        cmd = [sys.executable, os.path.join(HERE, "check_provenance.py"), "--report", a.report]
        if git("rev-parse", "--verify", "--quiet", base):
            cmd += ["--range", f"{base}..HEAD"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            findings.append(("PROVENANCE", p.stdout.strip().splitlines()[0] if p.stdout.strip() else "provenance check failed"))

    # 8. the human on the loop is TOLD, before anything merges.
    #
    # This is the last gate deliberately: it is the only one with a side effect outside the
    # repository, so it runs once every other reason to stop has been ruled out. Sending a notice
    # and then blocking on a failed check would train the reader to ignore the channel.
    #
    # Delivery is a PRECONDITION of the merge, not a consequence of it. A notice after the merge
    # gives nobody the chance to intervene, which is the entire difference between a human on the
    # loop and a human out of it — so if the notice cannot be delivered, this becomes a finding
    # and the verdict does not stand. The cost is stated plainly in the handbook: when the
    # notification path is down, hotl repositories stop merging. A repository that would rather
    # keep merging unwatched is describing hootl and should declare hootl.
    enf = enforcement_gate(cfg, aut, findings, facts_file=facts_file(a))
    notice = notice_gate(cfg, aut, findings, change=a.change, commit=head, pr=a.pr)
    loop = init_gate(aut, findings)
    cov = coverage_gate(aut, findings, changed_files_of(a))

    out = f"docs/intent/reports/RECHECK_{a.change}.md"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    who = (last or {}).get("author", {}).get("login", "?")
    # The autonomy mode goes in the record, not just in the refusal. Six months on, "why did this
    # merge without me" is answered by the artifact rather than by reconstructing what
    # governance.yaml happened to say that day.
    mode_line = mode_summary(aut, notice)
    enf_line = enforcement_summary(enf)
    # The loop's own armed state, in the record. Six months on, "the gate passed but the register
    # was empty" should be answerable from the artifact rather than from guesswork.
    loop_line = f"Loop state: **{loop.get('verdict', '?')}** \u00b7 {loop.get('line', '')}"
    cov_line = coverage_summary(cov)
    if a.unattended and not last:
        L = [f"# Unattended acceptance re-check — {a.change}", "",
             f"Head commit: `{head[:12]}` \u00b7 changed lines: {lines}", "",
             mode_line, "",
             enf_line, "",
             loop_line, "",
             cov_line, "",
             f"**No human approved this change.** It merged under `auto_merge: unattended`, and the",
             f"accountable human is the policy owner **{policy_owner}**, who certified the policy",
             "rather than this change. Hard rule H5 is deliberately relaxed here.", "",
             "Not applicable without an approval: stale-approval, review-time-vs-size, "
             "approved-before-checks.", "",
             f"**Verdict: {'ELIGIBLE AND VALIDATION HOLDS' if not findings else 'BLOCKED'}**", ""]
        L += [f"- **{k}** \u2014 {v}" for k, v in findings] or \
             ["- checks green on the head commit, ledger clear, provenance clean"]
    else:
        L = [f"# Sign-off re-check — {a.change}", "",
             f"Head commit: `{head[:12]}` \u00b7 changed lines: {lines} \u00b7 approved by: {who}", "",
             mode_line, "",
             enf_line, "",
             loop_line, "",
             cov_line, "",
             f"**Verdict: {'DECISION HOLDS' if not findings else 'DECISION DOES NOT STAND'}**", ""]
        L += [f"- **{k}** \u2014 {v}" for k, v in findings] or ["- every check passed on the signed commit"]
    open(out, "w").write("\n".join(L) + "\n")

    if not findings:
        if a.unattended and not last:
            print(f"[recheck] UNATTENDED ACCEPTANCE for {a.change} at {head[:10]} \u2014 no human approval; "
                  f"accountable policy owner: {policy_owner} \u2192 {out}")
        else:
            print(f"[recheck] DECISION HOLDS for {a.change} at {head[:10]} (approved by {who}) \u2192 {out}")
        return 0
    # A manager may already have released this exact commit. The release is re-verified, never assumed.
    chk = [sys.executable, os.path.join(HERE, "intent_escalate.py"), "check", "--change", a.change, "--commit", head]
    if a.comments_json: chk += ["--comments-json", a.comments_json]
    elif a.pr: chk += ["--pr", str(a.pr)]
    if a.no_override: chk += ["--no-override"]
    rel = subprocess.run(chk, capture_output=True, text=True)
    if rel.returncode == 0:
        msg = rel.stdout.strip().splitlines()[-1]
        with open(out, "a") as f:
            f.write(f"\n**Released by escalation:** {msg}\n")
        print(f"[recheck] DECISION HOLDS BY ESCALATION for {a.change} \u2014 {len(findings)} finding(s) were reviewed "
              f"by an approver and released: {msg}")
        return 0
    print(f"[recheck] DECISION DOES NOT STAND for {a.change} \u2014 {len(findings)} finding(s) \u2192 {out}")
    for k, v in findings:
        print(f"  {k}: {v}")
    if rc.get("on_fail", "escalate") == "escalate" and not a.no_escalate:
        reason = "; ".join(k for k, _ in findings) + f" (approved by {who})"
        subprocess.run([sys.executable, os.path.join(HERE, "intent_escalate.py"), "request",
                        "--change", a.change, "--commit", head, "--reason", reason, "--trigger", "recheck"])
    return 3


if __name__ == "__main__":
    sys.exit(main())
