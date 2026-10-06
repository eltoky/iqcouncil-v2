#!/usr/bin/env python3
"""Is this PR eligible to merge with NO human approval? Fail-closed by construction.

    unattended_eligibility.py --change ID --pr N [--pr-json f] [--report f] [--ledger f]
                              [--changed-files f] [--merged-prs N] [--graph yes|no] [--json]
    unattended_eligibility.py --self-test

Exit: 0 ELIGIBLE · 3 INELIGIBLE (reasons printed) · 2 configuration error.

This is the gate on a deliberate relaxation of hard rule H5. Every unknown is a NO. If the
generated share cannot be parsed, if the changed-line count is missing, if the code graph is absent
so blast radius cannot be judged, if any input file is unreadable — INELIGIBLE. A bypass that
guesses is worse than no bypass, because it merges the cases it understood least.

The accountable human is `merge.unattended.policy_owner`: they certified the POLICY, not this
change. That is a weaker guarantee than a per-change review, so the fences are narrow and the
reasons are always printed and logged.

Checks, in order (all must pass):
  1. unattended configured, with a policy_owner that looks like `Name <email>`
  2. generated share <= max_generated_share            (the report's generated_share field)
  3. changed lines <= max_changed_lines                (additions + deletions)
  4. no never_for class present:
       neg_claims            a NEG claim id appears in the PR intent report
       security_findings     the fix ledger mentions security
       first_pr_of_change    no previously merged PR for this change
       hub_nodes             the report names a hub node
       arch_contract_changes the diff touches an architecture-contract path
  5. the fix ledger exists and has no open rows
  6. the PR intent report records an APPROVE verdict
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, argparse, subprocess, fnmatch

HERE = os.path.dirname(os.path.abspath(__file__))
GOV = "docs/intent/governance.yaml"
OWNER_RE = re.compile(r"^[^<>@]{2,}\s<[^<>@\s]+@[^<>@\s]+>$")
NEG_RE = re.compile(r"\bNEG-[A-Za-z0-9][A-Za-z0-9-]*\b")
# Architecture contracts, AND the deployment surface. The defaults used to be documentation only,
# which inverted the fence on exactly the work it exists to catch: executed under a valid hootl
# grant, deleting 4,000 lines of dead legacy code was INELIGIBLE on max_changed_lines while a
# 3-line route change moving 100% of production traffic, a feature-flag cut-over, a DROP COLUMN
# migration and a widened dbt DELETE predicate were all ELIGIBLE and merged with no human.
#
# Risk is reach, not size. A diff that can move traffic, change schema, or alter what runs in
# production is fenced by WHAT IT TOUCHES; the line count never saw any of it.
DEFAULT_ARCH_PATHS = [
    # the original: design intent
    "docs/architecture/**", "*.drawio", "openspec/specs/**", "docs/adr/**",
    # deployment and traffic — the cut-over surface
    "infra/**", "deploy/**", "helm/**", "k8s/**", "terraform/**", "*.tf", "*.tfvars",
    "**/Dockerfile", "docker-compose*.yml", ".github/workflows/**",
    "**/nginx*.conf", "**/routes*", "**/ingress*",
    # feature flags: a one-line flip is a cut-over
    "config/flags*", "**/feature_flags*", "**/*.flags.yml",
    # schema and data shape — not revertible by git
    "**/migrations/**", "**/migrate/**", "**/*.ddl", "db/**",
    # platform and signing, which no other fence can see
    "**/*.entitlements", "**/*.plist", "**/*.pbxproj", "**/*.mobileprovision",
    "fastlane/**", "**/keystore*",
]
# Paths whose change cannot be undone by reverting the commit. `git revert` restores the DDL file,
# not the dropped column; it restores the flag file, not the traffic that already moved.
DEFAULT_IRREVERSIBLE_PATHS = [
    "**/migrations/**", "**/migrate/**", "**/*.ddl",
    "config/flags*", "**/feature_flags*",
    "**/nginx*.conf", "**/routes*", "**/ingress*",
    "*.tf", "*.tfvars", "terraform/**", "infra/**",
    "**/retention*", "**/backfill*",
]
# The vulnerability vocabulary now lives in verdicts.py, the ONE reader of review reports, and
# applies only to pre-2.0 prose ledgers; a structured ledger states `security:` per finding.

# The loop's OWN controls. Never eligible for an unattended merge, and deliberately NOT a
# `never_for` name or a configurable list: anything configurable here is configured by the very
# file an unattended PR would be changing. Before 2.0.0 none of these was fenced, so an eligible
# unattended PR could rewrite the policy, the hooks or the workflow that the NEXT run executes
# from main — and `human_in_loop_for: [governance_change, register_writes]` said the opposite,
# because nothing at merge time read it.
SELF_PROTECTED = [
    "docs/intent/governance.yaml", "docs/intent/coverage.yaml",
    "docs/intent/INTENT_REGISTER.md", "docs/intent/INTENT_TRACE.yaml",
    # The register's 2.0.0 WRITE PATH: a fragment is assembled into the register after the merge,
    # so leaving it unfenced let an unattended PR supersede a non-goal (reproduced in the 2.0.0
    # review). And the loop's other configuration files and its eval suite.
    "docs/intent/register.d/**", "docs/intent/concurrency.config",
    "docs/intent/provenance.config", "docs/intent/bands.yaml", "docs/intent/evals/**",
    # Accepting an intent is a human act recorded as a merge (the loop driver's `intent` stage
    # reads it there), so a merge that changes an intent.md is never unattended.
    "docs/intent/changes/*/intent.md",
    ".claude/**", ".github/**", "CODEOWNERS", "docs/CODEOWNERS",
    "shared/**", "install-loop.sh",
]

# The fence names this gate IMPLEMENTS. Which of them apply is policy.view(cfg).fences().
DEFAULT_NEVER = ["neg_claims", "security_findings", "first_pr_of_change",
                 "hub_nodes", "arch_contract_changes", "irreversible_change",
                 "build_signing_change"]


REGISTER = "docs/intent/INTENT_REGISTER.md"
_CLAIM_ROW = re.compile(r"^\|\s*(C-\d+)\s*\|")
_SECTION = re.compile(r"^#{2,3}\s+([A-Z]+)\b")


def neg_ids(register_text):
    """The ids of NEG claims, from the register's NEG section — or None if it cannot be read.

    The register issues `C-NNN` ids grouped under a `## NEG` heading (register-format.md). The fence
    used to search reports for `NEG-...`, an id form the register never emits, so on a real report
    it could not fire. A per-row category column is honoured too, for registers written that way.
    """
    if register_text is None:
        return None
    ids, section = set(), None
    for line in register_text.splitlines():
        m = _SECTION.match(line)
        if m:
            section = m.group(1)
            continue
        r = _CLAIM_ROW.match(line)
        if not r:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if section == "NEG" or "NEG" in cells[1:3]:
            ids.add(r.group(1))
    return ids


def trusted_register():
    """(text, known) — the register on the trust ref. known=False only when NO trusted ref exists:
    a trusted ref that simply has no register means there are no registered non-goals, which is a
    fact, not an unknown."""
    sys.path.insert(0, HERE)
    import govcfg
    reg, _ref = govcfg.read_trusted(REGISTER)
    if reg is not None:
        return reg, True
    return "", govcfg.resolve_ref() is not None


def neg_reasons(report_text, register=None, claims_touched=None):
    """Reason lines for the NEG fence. The register comes from the TRUST REF (or the caller): a
    branch that could delete its own NEG section would otherwise un-fence itself."""
    if report_text is None:
        return ["cannot read the PR intent report, so NEG claims cannot be ruled out"]
    reg, known = register if register is not None else trusted_register()
    ids = neg_ids(reg) if known else None
    legacy = set(NEG_RE.findall(report_text))
    if ids is None and not legacy:
        return ["no trusted ref exists, so which claims are NEG cannot be known and touching one "
                "cannot be ruled out"]
    # The UNION of what the report declares and what its text names. Trusting `claims_touched`
    # alone let a report name C-007 in prose while declaring `claims_touched: []` (2.0.0 review):
    # a structured field may add claims to the check, never remove them.
    named = {i for i in (ids or ()) if re.search(r"\b%s\b" % re.escape(i), report_text)}
    touched = set(claims_touched or ()) | named
    hits = sorted(legacy | (touched & set(ids or ())))
    return [f"touches registered non-goals: {', '.join(hits[:5])}"] if hits else []


def _matches_any(path, globs):
    """One glob semantics for every path fence, so two fences cannot disagree about `**`.

    fnmatch has no `**`, so each pattern is tried three ways: as written, with `/**` collapsed to
    `/*` for a single level, and as a prefix match for a whole subtree.
    """
    for g in globs:
        if (fnmatch.fnmatch(path, g)
                or fnmatch.fnmatch(path, g.replace("/**", "/*"))
                or (g.endswith("/**") and path.startswith(g[:-3] + "/"))
                or (g.startswith("**/") and fnmatch.fnmatch(os.path.basename(path), g[3:]))
                or (g.startswith("**/") and fnmatch.fnmatch(path, g[3:]))
                or (g.startswith("**/") and ("/" + path).find("/" + g[3:].split("*")[0]) >= 0
                    and fnmatch.fnmatch(path, "*/" + g[3:]))):
            return True
    return False


def _graph_present(root="docs/intent/graph"):
    """A code graph exists only if it has CONTENT. `mkdir -p docs/intent/graph` — an empty directory
    the branch under review can commit — flipped this to True and with it the hub-node fence, which
    is the one that judges blast radius. An empty directory is an absent graph."""
    if not os.path.isdir(root):
        return False
    for base, _dirs, files in os.walk(root):
        if any(not f.startswith(".") for f in files):
            return True
    return False


def load_cfg(path=None):
    """The policy, through govcfg. Untrusted is EMPTY — and an empty policy has no
    `merge.unattended` block, so nothing is eligible. A branch cannot make itself mergeable."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load(override=path)
    if not L.trusted:
        print(f"[eligibility] governance is UNTRUSTED — {L.why}. Nothing is eligible.",
              file=sys.stderr)
    return L.data


def autonomy_of(cfg):
    """The autonomy mode, failing closed to hitl — ONE implementation, in policy.py."""
    sys.path.insert(0, HERE)
    import policy
    return policy.autonomy_or_hitl(cfg)


def autonomy_gate(cfg, facts):
    """The autonomy mode's verdict as a list of refusal reasons, or [] when it permits autonomy.

    Records what it decided into `facts` either way, so an ELIGIBLE verdict still carries the mode
    that allowed it. The resolver fails closed in every direction — unknown mode, lapsed grant,
    mode and mechanism disagreeing, hotl with nowhere to send the notification — and every one of
    those lands on hitl, which means a human decides."""
    aut = autonomy_of(cfg)
    facts["autonomy_declared"] = aut["declared"]
    facts["autonomy_effective"] = aut["effective"]
    facts["autonomy_findings"] = [c for c, _ in aut["findings"]]
    if aut["autonomous_merge_permitted"]:
        return []
    head = (f"autonomy mode is '{aut['effective']}'"
            + (f" (declared '{aut['declared']}', degraded)" if aut["degraded"] else "")
            + " — a human decides this change")
    return [head] + [f"{code}: {text}" for code, text in aut["findings"]]


def read_lines(path):
    if not path or not os.path.exists(path):
        return None
    try:
        return [l.strip() for l in open(path) if l.strip()]
    except OSError:
        return None


def path_fences(never, un, changed_files, facts, cfg=None):
    """The two fences that judge a change by WHAT IT TOUCHES, as reason lines.

    Extracted from evaluate() because the ratchet caught it growing from F(58) to F(60) when the
    irreversibility fence was added. evaluate() is the eligibility decision itself and already
    F-rated, so it does not grow.

    These two are the answer to the inversion that executed on the shipped build: with the old
    documentation-only globs, deleting 4,000 lines of dead legacy code was INELIGIBLE on
    max_changed_lines while a 3-line route change moving 100% of production traffic, a
    feature-flag cut-over and a DROP COLUMN migration were all ELIGIBLE. Risk is reach.
    """
    if changed_files is None:
        # Unknown is a NO for every path fence at once — the self-protected one unconditionally.
        return ["the changed-file list is unavailable, so a change to the loop's own controls, an "
                "architecture contract, an irreversible change or a signing change cannot be "
                "ruled out"]
    reasons = []
    # Unconditional: not gated on `never`, and not read from configuration.
    own = [f for f in changed_files if _matches_any(f, SELF_PROTECTED)]
    facts["self_protected_files"] = own[:5]
    if own:
        reasons.append(f"changes the loop's own controls: {', '.join(own[:5])} — policy, hooks, "
                       f"workflows and the register are never merged without a human")
    import policy
    pv = policy.view(cfg)
    for cls, path_class, fact, say in PATH_FENCES:
        if cls not in never:
            continue
        hit = [f for f in changed_files if _matches_any(f, pv.globs(path_class))]
        facts[fact] = hit[:5]
        if hit:
            reasons.append(say % ", ".join(hit[:5]))
    return reasons


# (fence class in `never`, risk.paths class, facts key, reason). Irreversibility is a PATH FACT, not
# a self-declared adjective: `irreversible_change` existed only as a model-routing string the agent
# typed, and `irreversible_decision` was in human_in_loop_for while `irreversible_change` was not —
# so deciding to cut over required a human and performing the cut-over did not.
PATH_FENCES = (
    ("arch_contract_changes", "arch_contract", "arch_files",
     "touches architecture-contract paths: %s"),
    ("irreversible_change", "irreversible", "irreversible_files",
     "touches paths a revert cannot undo: %s — reverting the commit restores the file, not the "
     "migrated schema, the moved traffic or the deleted rows"),
    ("build_signing_change", "signing", "signing_files",
     "changes build signing or entitlements: %s"),
)


def report_fences(never, rec, report_text, register, merged_prs, graph_available, ledger_path,
                  facts):
    """The fences judged from the review reports, as reason lines — from ONE normalised record.

    Extracted from evaluate(), which is the eligibility decision itself and already F-rated. Every
    field is None when unknown, and an unknown can never be ruled out.
    """
    reasons = []
    if "neg_claims" in never:
        reasons += neg_reasons(report_text, register, rec.get("claims_touched"))
    if "security_findings" in never:
        if ledger_path is None or rec.get("open_findings") is None:
            reasons.append("cannot read the fix ledger, so security findings cannot be ruled out")
        else:
            if rec.get("unparseable_findings"):
                reasons.append(f"{len(rec['unparseable_findings'])} ledger line(s) look like "
                               f"findings but do not parse, so they cannot be ruled out: "
                               f"{rec['unparseable_findings'][0]}")
            if rec.get("security") is True:
                reasons.append("the fix ledger reports a security finding — one never merges "
                               "unattended")
            elif rec.get("security") is None:
                reasons.append("a ledger finding is not classified as security or not — an "
                               "unclassified finding cannot be ruled out")
    if "first_pr_of_change" in never:
        facts["merged_prs"] = merged_prs
        if merged_prs is None:
            reasons.append("cannot determine whether an earlier PR for this change merged — "
                           "unknown means no")
        elif merged_prs < 1:
            reasons.append("this is the first PR of the change; the first one is always reviewed")
    if "hub_nodes" in never:
        facts["graph_available"] = graph_available
        if not graph_available:
            reasons.append("no code graph, so blast radius cannot be judged — hub nodes cannot be "
                           "ruled out")
        elif rec.get("hub_nodes") is None:
            reasons.append("the PR intent report does not say which hub nodes it touches — "
                           "absent is not none")
        elif rec["hub_nodes"]:
            reasons.append(f"touches hub node(s) {', '.join(map(str, rec['hub_nodes'][:5]))} — "
                           f"wide blast radius never merges unattended")
    return reasons


def ledger_and_verdict(rec, report_text, facts):
    """The ledger must be readable and clean, and the intent review must have APPROVED."""
    reasons = []
    rows = rec.get("open_findings")
    if rows is None:
        reasons.append("no fix ledger — cannot show the review lanes' findings were resolved")
    else:
        if rec.get("unparseable_findings"):
            facts["unparseable_rows"] = len(rec["unparseable_findings"])
            reasons.append(f"{len(rec['unparseable_findings'])} ledger line(s) look like findings "
                           f"but do not parse, so they cannot be shown resolved: "
                           f"{rec['unparseable_findings'][0]}")
        if rows:
            facts["open_rows"] = len(rows)
            reasons.append(f"{len(rows)} unresolved ledger row(s): "
                           + ", ".join(f"{i} {s} {st}" for i, s, st in rows[:5]))
    if report_text is None:
        reasons.append("cannot read the PR intent report — no recorded intent verdict")
    elif rec.get("verdict") != "APPROVE":
        reasons.append(f"the PR intent report records {rec.get('verdict') or 'no verdict'}, not "
                       f"APPROVE")
    return reasons


def evaluate(cfg, *, change, pr_state, report_text, report_path, ledger_path,
             changed_files, merged_prs, graph_available, register=None):
    """Pure: inputs in, (eligible, reasons, facts) out. No I/O except the ledger/report helpers
    the caller already resolved."""
    reasons, facts = [], {}
    un = ((cfg.get("merge") or {}).get("unattended") or {})
    mode = (cfg.get("merge") or {}).get("auto_merge")
    facts["mode"] = mode

    # 0. autonomy mode — the named declaration of where the human stands, checked BEFORE the
    # mechanism: a repository whose mode does not permit autonomous merging has nothing to
    # evaluate, however permissive merge.auto_merge is.
    refusal = autonomy_gate(cfg, facts)
    if refusal:
        return False, refusal, facts
    # The mechanism the MODE implies (policy.implied_merge): hotl/hootl mean unattended even when
    # merge.auto_merge is not written. Testing the raw key made a correct hotl config ineligible.
    sys.path.insert(0, HERE)
    import policy
    mode = policy.implied_merge(facts.get("autonomy_effective") or "", mode)[0] or mode
    facts["mode"] = mode

    # 1. configuration
    if mode != "unattended":
        reasons.append(f"auto_merge is '{mode}', not 'unattended' — nothing to evaluate")
        return False, reasons, facts
    owner = (un.get("policy_owner") or "").strip()
    facts["policy_owner"] = owner
    if not owner or not OWNER_RE.match(owner) or "example.com" in owner:
        reasons.append("merge.unattended.policy_owner must be a real identity as 'Name <email>' "
                       "(the template placeholder does not count): nobody is accountable otherwise")

    # One normalised record for everything read out of the review reports (verdicts.py). Fields
    # when the reports carry an intent-report/1 block; prose otherwise, LABELLED — and with nobody
    # looking, wording is not evidence, so an unstructured report is ineligible here.
    import verdicts
    rec = verdicts.facts(report_text, verdicts.read_file(ledger_path))
    facts["reports_structured"] = rec["structured"]
    if report_text is not None and not rec["structured"]:
        reasons.append("the review reports carry no intent-report/1 block, so their verdicts would "
                       "be read from wording — not evidence when nobody is looking")
    reasons += [f"malformed review report: {x}" for x in rec["problems"]]

    # 2. generated share
    cap_share = un.get("max_generated_share")
    share = rec["generated_share"]
    facts["generated_share"] = share
    if cap_share is None:
        reasons.append("max_generated_share is not set")
    elif share is None:
        reasons.append("the PR report has no parseable 'Generated share' (H3) — unknown means no")
    elif share > cap_share:
        reasons.append(f"generated share {share}% exceeds max_generated_share {cap_share}%")

    # 3. size
    cap_lines = un.get("max_changed_lines")
    lines = None
    if pr_state is not None and "additions" in pr_state and "deletions" in pr_state:
        try:
            lines = int(pr_state["additions"]) + int(pr_state["deletions"])
        except (TypeError, ValueError):
            lines = None
    facts["changed_lines"] = lines
    if cap_lines is None:
        reasons.append("max_changed_lines is not set")
    elif lines is None:
        reasons.append("the changed-line count is unavailable — unknown means no")
    elif lines > cap_lines:
        reasons.append(f"{lines} changed lines exceeds max_changed_lines {cap_lines}")

    # 4. the fences — DERIVED from the one human-required list (policy.py). Before 2.0.0 this read
    #    `never_for` alone, so a class `human_in_loop_for` said "never auto-merges" was merged
    #    unattended whenever never_for left it out. Every human-required class the gate can detect
    #    is now a fence, and the only way to stop fencing it is to remove it from THE list.
    pv = policy.view(cfg)
    never = set(pv.fences())
    facts["never_for"] = sorted(never)
    facts["human_required_undetectable"] = pv.undetectable()
    # An unrecognised name in a LEGACY never_for removes nothing now, but it is still a typo the
    # author meant as a fence — say so rather than let it pass silently.
    known = set(DEFAULT_NEVER) | set(policy.DETECTORS) | set(policy.DEFAULT_HUMAN_REQUIRED)
    unknown_fences = sorted(set(un.get("never_for") or []) - known)
    if unknown_fences:
        reasons.append(f"merge.unattended.never_for names {unknown_fences}, which no fence "
                       f"implements — the intended fence is therefore not applied. Known fences: "
                       f"{sorted(DEFAULT_NEVER)}")
    reasons += report_fences(never, rec, report_text, register, merged_prs, graph_available,
                             ledger_path, facts)
    reasons += path_fences(never, un, changed_files, facts, cfg)

    # 5. the ledger must exist and be clean, and 6. the intent review must have approved
    reasons += ledger_and_verdict(rec, report_text, facts)

    return (not reasons), reasons, facts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--change")        # not needed for --self-test
    ap.add_argument("--pr")
    ap.add_argument("--pr-json")
    ap.add_argument("--report")
    ap.add_argument("--ledger")
    ap.add_argument("--changed-files", help="file with one changed path per line")
    ap.add_argument("--merged-prs", type=int, help="count of already-merged PRs for this change")
    ap.add_argument("--graph", choices=["yes", "no"], help="is a code graph available")
    ap.add_argument("--config")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if not a.change:
        print("[eligibility] --change is required", file=sys.stderr); sys.exit(2)

    cfg = load_cfg(a.config)
    sys.path.insert(0, HERE)
    import forge as FG
    fg = FG.Forge.from_config(cfg, facts_file=a.pr_json if (a.pr_json and os.path.exists(a.pr_json))
                              else None)

    # PR size, via the adapter. A failure here leaves pr_state as None, which evaluate() treats as
    # an unknown changed-line count — and an unknown is INELIGIBLE. Never substitute a zero.
    pr_state = None
    try:
        facts = fg.facts(pr=int(a.pr) if a.pr else None, change=a.change)
        if facts is not None:
            pr_state = FG.to_github(facts)
    except Exception:
        pr_state = None

    report_text = None
    if a.report and os.path.exists(a.report):
        try:
            report_text = open(a.report).read()
        except OSError:
            report_text = None

    # How many requests for this change already merged — the `first_pr_of_change` fence. Unknown
    # stays None, and evaluate() reads None as "cannot tell", which is INELIGIBLE.
    merged = a.merged_prs
    if merged is None and a.change:
        try:
            merged = fg.merged_pr_count(a.change)
        except Exception:
            merged = None

    graph = (a.graph == "yes") if a.graph else _graph_present()

    ok, reasons, facts = evaluate(
        cfg, change=a.change, pr_state=pr_state, report_text=report_text, report_path=a.report,
        ledger_path=a.ledger, changed_files=read_lines(a.changed_files), merged_prs=merged,
        graph_available=graph)

    if a.json:
        print(json.dumps({"eligible": ok, "reasons": reasons, "facts": facts}, indent=1))
    else:
        print(f"[eligibility] {'ELIGIBLE' if ok else 'INELIGIBLE'} — change {a.change}"
              + (f", PR {a.pr}" if a.pr else ""))
        for r in reasons:
            print("  - " + r)
        if ok:
            print(f"  policy owner accountable for this merge: {facts.get('policy_owner')}")
            print(f"  generated share {facts.get('generated_share')}% · {facts.get('changed_lines')} changed lines")
    sys.exit(0 if ok else 3)


# ----------------------------------------------------------------- tests
def _cfg(**over):
    un = {"policy_owner": "Ada Lovelace <ada@acme.io>", "max_generated_share": 40,
          "max_changed_lines": 200, "never_for": DEFAULT_NEVER}
    un.update(over.pop("unattended", {}))
    # An autonomy mode that PERMITS autonomous merging, because that is the precondition every
    # other case here is about. The fixture carries it explicitly rather than relying on a
    # default: `auto_merge: unattended` with no declared mode is now ineligible, which is the
    # point of the mode, and the cases below prove that separately.
    aut = {"mode": "hootl", "review_by": "2999-01-01",
           "acknowledged_by": "Grace Hopper <grace@acme.io>"}
    aut.update(over.pop("autonomy", {}))
    c = {"merge": {"auto_merge": "unattended", "unattended": un}, "autonomy": aut}
    c["merge"].update(over)
    return c


# Structured, as the review skills write them from 2.0.0. The prose forms are kept below only as
# the negative cases they now are on the unattended path.
GOOD_REPORT = ("---\nintent-report: 1\nverdict: APPROVE\ngenerated_share: 20\nclaims_touched: [C-012]\n"
               "hub_nodes: []\n---\n# PR intent\n")
PROSE_REPORT = "# PR intent\n\n**Verdict: APPROVE**\n\n## Provenance\n\n**Generated share** — 20%\n"


def _ledger(*rows):
    body = "".join(f"  - {{id: {i}, severity: {s}, status: {st}{sec}}}\n" for i, s, st, sec in rows)
    return "---\nintent-report: 1\nfindings:\n" + body + "---\n"


def self_test():
    import tempfile
    d = tempfile.mkdtemp()
    rep = os.path.join(d, "r.md"); open(rep, "w").write(GOOD_REPORT)
    led = os.path.join(d, "l.md"); open(led, "w").write(_ledger(("F-001", "MINOR", "VERIFIED", ", security: false")))
    base = dict(change="c1", pr_state={"additions": 50, "deletions": 20}, report_text=GOOD_REPORT,
                report_path=rep, ledger_path=led, changed_files=["src/a.py"], merged_prs=2,
                graph_available=True, register=("", True))
    cases = []

    def case(name, want, cfg=None, **over):
        kw = dict(base); kw.update(over)
        cases.append((name, want, cfg or _cfg(), kw))

    case("happy path", True)
    case("wrong mode", False, {"merge": {"auto_merge": "after_signoff"}})
    case("placeholder policy owner", False, _cfg(unattended={"policy_owner": "Name <email@example.com>"}))
    case("missing policy owner", False, _cfg(unattended={"policy_owner": ""}))
    case("malformed policy owner", False, _cfg(unattended={"policy_owner": "ada"}))
    case("share over cap", False, _cfg(unattended={"max_generated_share": 10}))
    case("share unparseable", False, report_text="---\nintent-report: 1\nverdict: APPROVE\nhub_nodes: []\n---\n",
         report_path=os.path.join(d, "missing.md"))
    case("lines over cap", False, pr_state={"additions": 400, "deletions": 1})
    case("lines unknown", False, pr_state={})
    case("neg claim touched", False,
         report_text=GOOD_REPORT.replace("[C-012]", "[C-007]"),
         register=("## NEG — Non-goals\n| ID | Claim |\n|---|---|\n| C-007 | no multi-tenant |\n", True))
    case("first PR of the change", False, merged_prs=0)
    case("merged count unknown", False, merged_prs=None)
    case("no code graph", False, graph_available=False)
    case("hub node named", False, report_text=GOOD_REPORT.replace("hub_nodes: []", "hub_nodes: [auth.session]"))
    case("hub nodes not stated is not none", False,
         report_text=GOOD_REPORT.replace("hub_nodes: []\n", ""))
    case("a prose-only report is ineligible", False, report_text=PROSE_REPORT)
    case("report unreadable", False, report_text=None)
    case("no APPROVE verdict", False, report_text=GOOD_REPORT.replace("APPROVE", "REQUEST-CHANGES"))
    case("no ledger", False, ledger_path=os.path.join(d, "nope.md"))

    # open ledger row
    led2 = os.path.join(d, "l2.md"); open(led2, "w").write(_ledger(("F-002", "BLOCKER", "OPEN", ", security: false")))
    case("open ledger row", False, ledger_path=led2)
    # security mention in the ledger
    led3 = os.path.join(d, "l3.md"); open(led3, "w").write(_ledger(("F-003", "MINOR", "VERIFIED", ", security: true")))
    case("security finding", False, ledger_path=led3)
    led4 = os.path.join(d, "l4.md"); open(led4, "w").write(_ledger(("F-004", "MINOR", "VERIFIED", "")))
    case("an unclassified finding cannot be ruled out", False, ledger_path=led4)
    # architecture contract path
    case("arch contract touched", False, changed_files=["docs/architecture/overview.md"])
    case("drawio touched", False, changed_files=["system.drawio"])
    # relaxing never_for lets a first PR through — the fences are configurable, not hardcoded
    case("never_for narrowed", True, _cfg(unattended={"never_for": ["neg_claims"]}), merged_prs=0,
         graph_available=False)

    # -- the autonomy mode gates everything above it. Each of these is a repository that would
    # otherwise be perfectly eligible, refused on the mode alone.
    case("hitl refuses, whatever auto_merge says", False, _cfg(autonomy={"mode": "hitl"}))
    case("no declared mode refuses", False, _cfg(autonomy={"mode": None}))
    case("misspelled mode refuses", False, _cfg(autonomy={"mode": "hotl-ish"}))
    case("lapsed grant refuses", False, _cfg(autonomy={"review_by": "2020-01-01"}))
    case("grant with no end date refuses", False, _cfg(autonomy={"review_by": ""}))
    case("hootl with nobody acknowledging refuses", False,
         _cfg(autonomy={"acknowledged_by": ""}))
    case("hotl with nowhere to notify refuses", False,
         _cfg(autonomy={"mode": "hotl", "notify": {"channel": "", "command": "/bin/notify"}}))
    case("hotl with nothing to notify WITH refuses", False,
         _cfg(autonomy={"mode": "hotl", "notify": {"channel": "slack:#gov", "command": ""}}))
    # ...and a correctly-configured hotl is eligible, so the cases above fail for the stated
    # reason rather than because the mode fence refuses everything.
    case("hotl with a channel and a notifier is eligible", True,
         _cfg(autonomy={"mode": "hotl", "notify": {"channel": "slack:#eng-governance",
                                                   "command": "/bin/notify"}}))

    # S6 · the loop's own controls are never eligible — and no configuration can remove that.
    case("changing the policy file is never unattended", False,
         changed_files=["docs/intent/governance.yaml"])
    case("changing an installed hook is never unattended", False,
         changed_files=[".claude/intent-hooks/recheck_signoff.py"])
    case("self-protection survives emptied fences", False,
         _cfg(unattended={"never_for": [], "arch_contract_paths": [], "irreversible_paths": []}),
         changed_files=[".claude/settings.json"])

    bad = 0
    # S10 · NEG claim ids come from the register's NEG section, in the id form it actually issues.
    reg = ("## FUNC — x\n| ID | Claim |\n|---|---|\n| C-001 | a | \n"
           "## NEG — Non-goals\n| ID | Claim |\n|---|---|\n| C-007 | never store card numbers |\n")
    for label, ok in (
            ("NEG ids are read from the NEG section", neg_ids(reg) == {"C-007"}),
            ("a FUNC claim is not a NEG claim", "C-001" not in (neg_ids(reg) or set())),
            ("an unreadable register is None, not empty", neg_ids(None) is None)):
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    for name, want, cfg, kw in cases:
        got, reasons, _ = evaluate(cfg, **kw)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name} -> {'ELIGIBLE' if got else 'INELIGIBLE'}"
              + ("" if ok else f"  (expected {'ELIGIBLE' if want else 'INELIGIBLE'}; {reasons[:2]})"))
    total = len(cases) + 3
    print(f"[eligibility] self-test: {total - bad}/{total} passed")
    return 3 if bad else 0


if __name__ == "__main__":
    main()
