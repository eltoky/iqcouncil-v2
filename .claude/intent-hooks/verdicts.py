#!/usr/bin/env python3
"""The ONE reader of review reports — structured first, prose only as a labelled fallback.

    verdicts.py --check FILE [FILE ...]   validate the intent-report/1 block in each file
    verdicts.py --facts --report R [--ledger L]   the normalised record the gates read, as JSON
    verdicts.py --self-test

Why this module exists
----------------------
The gates read their inputs out of prose. "Is the verdict APPROVE?" was a regex for `Verdict:
APPROVE`; "does it touch a hub node?" was a regex for the words "hub node"; "is any finding a
security finding?" was an English vocabulary (residual F-003) run over the whole ledger; the
generated share was a regex in two places whose docstring claimed they shared one (residual
F-009); and the NEG fence searched for an id form the register never issues (S10). A report worded
slightly differently passed or failed for reasons nobody wrote down.

So review skills write a small YAML block at the very top of the report:

    ---
    intent-report: 1
    verdict: APPROVE               # PR intent: APPROVE | APPROVE-WITH-FOLLOW-UPS | REQUEST-CHANGES | BLOCK
    decision: HUMAN-REVIEW         # merge gate: AUTO-MERGE | HUMAN-REVIEW | BLOCK
    gate: PASS                     # a stage gate (spec, tests): PASS | PASS-WITH-UPDATES | BLOCK
    generated_share: 20            # integer percent (provenance H3)
    claims_touched: [C-012, C-007] # every register id this change realises or affects
    hub_nodes: []                  # hub nodes touched; [] is an ANSWER ("none"), absent is not
    findings:                      # fix ledger rows
      - {id: F-001, severity: MINOR, status: VERIFIED, security: false}
    ---

and the gates read FIELDS. Prose is still read when the block is absent — so a report written
before 2.0.0 is not suddenly unreadable — but the record says `structured: false`, and the
unattended path treats an unstructured report as ineligible: with nobody looking, a regex over
wording is not evidence.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, argparse

MODULE_MARKER = "intent-loop/verdicts/1"
SCHEMA = "intent-report"
# The pr-review skill's own vocabulary. Matched EXACTLY: the old prose regex `Verdict:\s*APPROVE`
# matched the prefix of APPROVE-WITH-FOLLOW-UPS, so "approve, with follow-ups" read as a plain approve
# on the unattended path — while the merge-gate skill sends follow-ups to a human.
VERDICTS = ("APPROVE", "APPROVE-WITH-FOLLOW-UPS", "REQUEST-CHANGES", "BLOCK")
DECISIONS = ("AUTO-MERGE", "HUMAN-REVIEW", "BLOCK")
# A stage gate's own outcome (spec gate, test run): what the loop driver advances on.
GATES = ("PASS", "PASS-WITH-UPDATES", "BLOCK")
SEVERITIES = ("BLOCKER", "MAJOR", "MINOR", "NIT", "INFO")
TERMINAL = {"VERIFIED", "KILLED", "MERGED-INTO"}
ID_RE = re.compile(r"^[A-Za-z]+-\d+$")

# The prose fallback, kept in ONE place. Each pattern is what a pre-2.0 report actually wrote.
_P_VERDICT = re.compile(r"\bVerdict:\s*\**\s*(APPROVE-WITH-FOLLOW-UPS|APPROVE|REQUEST-CHANGES|"
                        r"CHANGES-REQUESTED|BLOCK)(?![\w-])", re.I)
_P_SHARE = re.compile(r"\*\*Generated share\*\*\s*[:—-]\s*~?(\d+)\s*%")
_P_HUB = re.compile(r"\bhub node", re.I)
_P_CLAIM = re.compile(r"\bC-\d+\b")
SECURITY_RE = re.compile(
    r"\b(?:security|vulnerab\w*|CVE-\d{4}-\d+|CWE-\d+|exploit\w*|"
    r"injection|SQLi|XSS|CSRF|SSRF|XXE|RCE|LFI|RFI|"
    r"auth(?:entication|oriz\w+)?[ -]?bypass|privilege[ -]escalation|path[ -]traversal|"
    r"deserializ\w+|hard[ -]?coded[ -](?:secret|credential|password|key)|"
    r"secret[ -]leak\w*|credential[ -]leak\w*|remote[ -]code[ -]execution)\b", re.I)


# ---------------------------------------------------------------- the block

MAX_BLOCK = 64 * 1024


def _strict_yaml():
    """A SafeLoader that refuses what a report block never needs and an attacker can use:
    DUPLICATE keys (`verdict: BLOCK` then `verdict: APPROVE` read as APPROVE) and ANCHORS/ALIASES
    (a 377-byte alias bomb expanded to 5.2M characters, and nested ones hang the job) — both
    reproduced in the 2.0.0 review."""
    import yaml

    class Strict(yaml.SafeLoader):
        def compose_node(self, parent, index):
            ev = self.peek_event()
            if isinstance(ev, yaml.events.AliasEvent) or getattr(ev, "anchor", None):
                raise yaml.YAMLError("anchors and aliases are not allowed in an intent-report block")
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            seen = set()
            for k, _ in node.value:
                key = self.construct_object(k, deep=deep)
                if key in seen:
                    raise yaml.YAMLError(f"duplicate key {key!r} in an intent-report block")
                seen.add(key)
            return super().construct_mapping(node, deep)
    return Strict


def _fence(text):
    """(lines, index of the closing ---) or (lines, None)."""
    lines = text.split("\n")
    try:
        return lines, lines.index("---", 1)
    except ValueError:
        return lines, None


def body_after_block(text):
    """The report's text after its block — the prose a tripwire may read. A horizontal rule (`---`)
    later in the body is part of the body, not a second fence."""
    lines, end = _fence(text or "")
    return "\n".join(lines[end + 1:]) if (text or "").startswith("---") and end is not None else (text or "")


def _load_block(raw):
    """(doc, problem) for the text between the fences — size-capped, strictly parsed."""
    if len(raw) > MAX_BLOCK:
        return None, f"the intent-report block is larger than {MAX_BLOCK} bytes"
    try:
        import yaml
        return yaml.load(raw, Loader=_strict_yaml()) or {}, None          # noqa: S506 — strict SafeLoader
    except Exception as e:                                       # noqa: BLE001
        return None, f"the intent-report block does not parse: {e}"


def block(text):
    """(doc, problem). (None, None) when the text has no block at all."""
    if not text or not text.startswith("---"):
        return None, None
    lines, end = _fence(text)
    if end is None:
        return None, "the report opens a front-matter block with --- and never closes it"
    doc, problem = _load_block("\n".join(lines[1:end]))
    if problem:
        return None, problem
    if not isinstance(doc, dict) or SCHEMA not in doc:
        return None, None                                        # some other front matter
    return doc, None


def _validate_finding(i, row):
    """Problems with one findings[] row."""
    if not isinstance(row, dict):
        return [f"findings[{i}]: must be a mapping"]
    p = []
    if not ID_RE.match(str(row.get("id") or "")):
        p.append(f"findings[{i}].id: {row.get('id')!r} is not an id like F-001")
    if str(row.get("severity") or "").upper() not in SEVERITIES:
        p.append(f"findings[{i}].severity: {row.get('severity')!r} not in {SEVERITIES}")
    if not str(row.get("status") or "").strip():
        p.append(f"findings[{i}].status: required")
    if not isinstance(row.get("security"), bool):
        p.append(f"findings[{i}].security: required, true or false — a finding nobody "
                 f"classified is not a non-security finding")
    return p


def _validate_enum(doc, key, allowed):
    v = doc.get(key)
    if v is not None and str(v).upper() not in allowed:
        return [f"{key}: {v!r} is not one of {', '.join(allowed)}"]
    return []


def validate(doc):
    """Problems with an intent-report/1 block, as lines. Empty means conformant."""
    p = []
    if doc.get(SCHEMA) != 1:
        p.append(f"{SCHEMA}: must be 1, got {doc.get(SCHEMA)!r}")
    p += (_validate_enum(doc, "verdict", VERDICTS) + _validate_enum(doc, "decision", DECISIONS)
          + _validate_enum(doc, "gate", GATES))
    g = doc.get("generated_share")
    if g is not None and (isinstance(g, bool) or not isinstance(g, int) or not 0 <= g <= 100):
        p.append(f"generated_share: must be an integer 0-100, got {g!r}")
    for key in ("claims_touched", "hub_nodes"):
        val = doc.get(key)
        if val is not None and not isinstance(val, list):
            p.append(f"{key}: must be a list ([] means none), got {type(val).__name__}")
    found = doc.get("findings")
    if found is not None and not isinstance(found, list):
        p.append("findings: must be a list")
    for i, row in enumerate(found if isinstance(found, list) else []):
        p += _validate_finding(i, row)
    return p


# ---------------------------------------------------------------- prose fallback (labelled)

def _prose_ledger(text):
    """(open_rows, unparseable, security) from a pre-2.0 markdown ledger table."""
    rows, bad = [], []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s.startswith("|") or set(s) <= set("|- :"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4:
            if re.match(r"^[A-Za-z]+-?\d+$", cells[0] if cells else ""):
                bad.append(s[:70])
            continue
        ident, sev, status = cells[0], cells[1].upper(), cells[3].upper()
        status = re.sub(r"\s*\([^)]*\)\s*$", "", status).strip()
        if not ID_RE.match(ident):
            if re.match(r"^[A-Za-z]+-?\d+$", ident):
                bad.append(s[:70])
            continue
        if status in ("STATUS", "VERDICT") or not status:
            continue
        if status not in TERMINAL:
            rows.append((ident, sev, status))
    return rows, bad, bool(SECURITY_RE.search(text or ""))


def _prose_share(text):
    if not text or "## Provenance" not in text:
        return None
    sec = text.split("## Provenance", 1)[1].split("\n## ", 1)[0]
    m = _P_SHARE.search(sec)
    return int(m.group(1)) if m else None


def _prose_verdict(text):
    m = _P_VERDICT.search(text or "")
    if not m:
        return None
    v = m.group(1).upper()
    return "REQUEST-CHANGES" if v == "CHANGES-REQUESTED" else v


# ---------------------------------------------------------------- the normalised record

def _list_or_none(v):
    return list(v) if isinstance(v, list) else None


def _report_facts(rec, text):
    """Fill the report fields: from the block, or from prose — labelled — when there is none."""
    doc, prob = block(text)
    if prob:
        rec["problems"].append(f"report: {prob}")
    if doc is None:
        rec["structured"] = False
        rec["verdict"] = _prose_verdict(text)
        rec["generated_share"] = _prose_share(text)
        rec["claims_touched"] = sorted(set(_P_CLAIM.findall(text)))
        rec["hub_nodes"] = ["(named in prose)"] if _P_HUB.search(text) else []
        return
    rec["problems"] += [f"report: {x}" for x in validate(doc)]
    for key in ("verdict", "decision", "gate"):                 # the three enums, read exactly
        v = doc.get(key)
        rec[key] = str(v).upper() if v not in (None, "") else None
    rec["generated_share"] = doc.get("generated_share")
    rec["claims_touched"] = _list_or_none(doc.get("claims_touched"))
    rec["hub_nodes"] = _list_or_none(doc.get("hub_nodes"))


def _security_of(rows):
    """True if any finding is classified security; None if any is unclassified; else False."""
    sec = [r.get("security") for r in rows]
    if any(s is True for s in sec):
        return True
    return None if any(not isinstance(s, bool) for s in sec) else False


def _ledger_facts(rec, text):
    """Fill the ledger fields from the block's findings, or from the prose table."""
    doc, prob = block(text)
    if prob:
        rec["problems"].append(f"ledger: {prob}")
    if doc is None or doc.get("findings") is None:
        rec["structured"] = False
        rec["open_findings"], rec["unparseable_findings"], rec["security"] = _prose_ledger(text)
        return
    rec["problems"] += [f"ledger: {x}" for x in validate(doc)]
    rows = [r for r in doc.get("findings") or [] if isinstance(r, dict)]
    rec["open_findings"] = [(str(r.get("id")), str(r.get("severity") or "").upper(),
                             str(r.get("status") or "").upper()) for r in rows
                            if str(r.get("status") or "").upper() not in TERMINAL]
    rec["security"] = _security_of(rows)
    # A TRIPWIRE, not the signal: the block classifies, but if it classifies nothing as security
    # while the ledger's own wording describes a vulnerability, the two disagree — and a
    # classification that contradicts its finding is not trusted with nobody looking.
    body = body_after_block(text)
    if rec["security"] is False and SECURITY_RE.search(body):
        rec["problems"].append(
            "ledger: no finding is classified `security: true`, but the ledger's own wording "
            "describes a vulnerability — the classification and the finding disagree")


def facts(report_text=None, ledger_text=None):
    """One record for the gates. Every field is None when it is UNKNOWN — never a default.

    structured   True only if every input that was supplied carried a valid block
    problems     validation problems in any block (a malformed block is not ignored)
    """
    rec = {"structured": True, "problems": [], "verdict": None, "generated_share": None,
           "claims_touched": None, "hub_nodes": None, "open_findings": None,
           "unparseable_findings": [], "security": None, "decision": None, "gate": None}
    if report_text is not None:
        _report_facts(rec, report_text)
    if ledger_text is not None:
        _ledger_facts(rec, ledger_text)
    return rec


def read_file(path):
    """The text of a report, or None when it cannot be read — never an empty string."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- self-test

STRUCTURED_REPORT = ("---\nintent-report: 1\nverdict: APPROVE\ngenerated_share: 20\n"
                     "claims_touched: [C-012]\nhub_nodes: []\n---\n# PR intent\n")
STRUCTURED_LEDGER = ("---\nintent-report: 1\nfindings:\n"
                     "  - {id: F-001, severity: MINOR, status: VERIFIED, security: false}\n---\n")


def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    r = facts(STRUCTURED_REPORT, STRUCTURED_LEDGER)
    check("a structured pair is structured", r["structured"] and not r["problems"])
    check("fields are read, not guessed", r["verdict"] == "APPROVE" and r["generated_share"] == 20
          and r["claims_touched"] == ["C-012"] and r["hub_nodes"] == [] and r["security"] is False
          and r["open_findings"] == [])
    # Prose wording no longer decides anything when a block exists.
    r = facts(STRUCTURED_REPORT + "\nThis mentions a hub node and Verdict: BLOCK in passing.\n")
    check("prose cannot override the block", r["verdict"] == "APPROVE" and r["hub_nodes"] == [])
    # Security is a FIELD: a SQL-injection finding that never says "security" is still caught...
    led = ("---\nintent-report: 1\nfindings:\n"
           "  - {id: F-002, severity: BLOCKER, status: FIXED-unverified, security: true}\n---\n")
    r = facts(None, led)
    check("a security finding is a field", r["security"] is True)
    check("an unverified fix is still open", r["open_findings"] == [("F-002", "BLOCKER", "FIXED-UNVERIFIED")])
    # A block that says no security while the table describes one is a disagreement, not a pass.
    r = facts(None, STRUCTURED_LEDGER + "| F-001 | MINOR | sql injection in login | VERIFIED |\n")
    check("block/wording disagreement on security is a problem", bool(r["problems"]))
    r = facts(None, STRUCTURED_LEDGER + "| F-001 | MINOR | typo in a comment | VERIFIED |\n")
    check("an ordinary finding raises no disagreement", not r["problems"])
    # ...and an unclassified finding is UNKNOWN, not "not security".
    led = "---\nintent-report: 1\nfindings:\n  - {id: F-003, severity: MINOR, status: VERIFIED}\n---\n"
    r = facts(None, led)
    check("an unclassified finding is unknown", r["security"] is None and r["problems"])
    # Malformed blocks are problems, never silently ignored.
    for bad in ("---\nintent-report: 1\nverdict: MAYBE\n---\n",
                "---\nintent-report: 1\ngenerated_share: 120\n---\n",
                "---\nintent-report: 1\nhub_nodes: none\n---\n",
                "---\nintent-report: 1\nverdict: [unclosed\n---\n",
                "---\nintent-report: 1\nverdict: APPROVE\n"):
        check(f"malformed block is a problem: {bad[:40]!r}", bool(facts(bad)["problems"]))
    # The fallback still reads a pre-2.0 report, and says it did.
    legacy = "# PR\n\n**Verdict: APPROVE**\n\n## Provenance\n\n**Generated share** — 30%\n"
    r = facts(legacy)
    check("prose is read, and labelled", r["structured"] is False and r["verdict"] == "APPROVE"
          and r["generated_share"] == 30)
    check("APPROVE-WITH-FOLLOW-UPS is NOT approve (prose)",
          facts("**Verdict: APPROVE-WITH-FOLLOW-UPS**")["verdict"] == "APPROVE-WITH-FOLLOW-UPS")
    check("APPROVE-WITH-FOLLOW-UPS is NOT approve (structured)",
          facts("---\nintent-report: 1\nverdict: APPROVE-WITH-FOLLOW-UPS\n---\n")["verdict"]
          == "APPROVE-WITH-FOLLOW-UPS")
    check("a merge-gate decision is read", facts("---\nintent-report: 1\ndecision: HUMAN-REVIEW\n---\n")
          ["decision"] == "HUMAN-REVIEW")
    check("a stage gate is read", facts("---\nintent-report: 1\ngate: PASS-WITH-UPDATES\n---\n")["gate"]
          == "PASS-WITH-UPDATES")
    check("an unknown gate value is a problem", bool(facts("---\nintent-report: 1\ngate: OK\n---\n")["problems"]))
    check("prose CHANGES-REQUESTED normalises", facts("Verdict: CHANGES-REQUESTED")["verdict"]
          == "REQUEST-CHANGES")
    r = facts(None, "| F-1 | BLOCKER | sql injection | OPEN |\n")
    check("prose ledger: open row and security vocabulary", r["open_findings"] and r["security"])
    # Hostile YAML is refused, not interpreted.
    check("duplicate keys are a problem, not last-wins",
          bool(facts("---\nintent-report: 1\nverdict: BLOCK\nverdict: APPROVE\n---\n")["problems"]))
    check("aliases are a problem", bool(facts("---\nintent-report: 1\na: &x [1]\nhub_nodes: *x\n---\n")["problems"]))
    check("an oversized block is a problem", bool(facts("---\nintent-report: 1\nx: '" + "a" * 70000 + "'\n---\n")["problems"]))
    # A horizontal rule in the body must not hide the security wording from the tripwire.
    r = facts(None, STRUCTURED_LEDGER + "| F-001 | MINOR | sql injection in login | VERIFIED |\n\n---\n\nnotes\n")
    check("a --- rule in the body does not hide the wording", bool(r["problems"]))
    # Unknown stays unknown.
    r = facts(None, None)
    check("no inputs -> every field unknown", all(r[k] is None for k in
          ("verdict", "generated_share", "claims_touched", "hub_nodes", "open_findings", "security")))
    check("other front matter is not an intent report", block("---\ntitle: x\n---\n") == (None, None))
    print(f"[verdicts] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", nargs="+", metavar="FILE", help="validate each file's block")
    ap.add_argument("--facts", action="store_true", help="print the normalised record")
    ap.add_argument("--report")
    ap.add_argument("--ledger")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.check:
        bad = 0
        for f in a.check:
            text = read_file(f)
            doc, prob = block(text or "")
            probs = ([prob] if prob else []) + (validate(doc) if doc else [])
            if doc is None and not prob:
                probs = [f"no {SCHEMA}/1 block — the gates fall back to reading prose"]
            bad += 1 if probs else 0
            print(f"[verdicts] {f}: " + ("ok" if not probs else "; ".join(probs)))
        return 1 if bad else 0
    if a.facts:
        print(json.dumps(facts(read_file(a.report), read_file(a.ledger)), indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
