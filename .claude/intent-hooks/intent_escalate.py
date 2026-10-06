#!/usr/bin/env python3
"""Escalation for the intent loop: challenge-response release codes signed by an approver.
Designed to survive concurrent use: parallel CI runs, many branches, several approvers.

  keygen  --out PATH                          approver, once: create a key pair; prints the public key
  request --change ID --reason TEXT           open an escalation -> prints a REQUEST code
  sign    REQUEST_CODE --key PATH --approver ID [--decision release|reject]
                                              approver, on THEIR machine -> prints a RELEASE code
  verify  RELEASE_CODE                        any machine: record the decision, report the outcome
  check   --change ID [--commit SHA]          CI: is this change released at this commit?
  override --change ID --reason TEXT          local bypass — only if the protected config allows it
  check --change ID [--no-override]           re-verify a stored decision; --no-override ignores
                                              local overrides (fork PRs cannot attribute one)
  sign CODE --key F --approver ID [--yes]     --yes skips the interactive confirmation (CI only)
  status  [--change ID]                       escalations and recorded decisions
  log-verify [--range A..B]                   records are append-only: report any modified or deleted
  log-render [--out PATH]                     hash-chained export of the events, stamped with the commit

Security model
  * Unforgeable: a release is an Ed25519 signature; the private key lives only with the approver.
  * Bound: the signature covers escalation id + change + exact commit + expiry + decision + approver.
  * Self-contained: the release code carries those fields, so it verifies on ANY machine against the
    approver chain on the protected branch. No local state is trusted or required.
  * Trusted config: approver keys and allow_local_override come from the protected branch (origin/main).

Concurrency model (no shared mutable file on any branch)
  * Escalation id = hash(change, commit): simultaneous requests for one commit are ONE escalation.
  * Every decision is its own file (resolved/<id>-<decision>-<approver>.json); every log event is its
    own file (events/). Distinct files never conflict when branches merge.
  * The outcome is computed from ALL valid decisions, so it does not depend on arrival order:
    any valid REJECT blocks; otherwise any valid RELEASE releases; otherwise an allowed override.
  * Records are append-only, enforced from git history (log-verify); the chained log is an export.
  * Decisions may also be posted as PR comments; check reads them, so a rejection cannot be withheld.
Exit: 0 released/ok, 3 blocked/rejected/refused, 1 error.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, json, glob, base64, hashlib, secrets, argparse, subprocess, getpass, time
from datetime import datetime, timezone, timedelta

GOV = "docs/intent/governance.yaml"
BASE = "docs/intent/governance"
LOG = f"{BASE}/ESCALATIONS.log"
# The trust ref is resolved in ONE place, govcfg.resolve_ref(). Two different refs were
# both called "the trust ref" before 2.0.0, so routing floors could lag merged policy.
# One explicit table used by sign, verify and check. (An earlier version used the first letter,
# and "release" and "reject" both start with R.)
DECISION_CODE = {"release": "R", "reject": "X"}


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def b64e(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s):
    """Decode, and REFUSE a non-canonical encoding.

    Base64's final character carries fewer significant bits than it has room for, so several
    different spellings of a code decode to identical bytes. The signature still verifies — nothing
    is forgeable this way, because the bytes that were signed are the same bytes — but it means one
    decision has many valid-looking codes, and "this exact string is the approver's code" stops
    being true. Audit trails and tamper tests both depend on that being true.

    Re-encoding the decoded bytes and comparing is the whole check: a canonical encoding round-trips
    to itself, and any other spelling does not.
    """
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != s:
        raise ValueError("non-canonical base64: this is not the code as it was issued")
    return raw
def now(): return datetime.now(timezone.utc)
def iso(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
def parse(s): return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_cfg(trusted=True):
    """(policy, source) through govcfg.

    trusted=True is used for every decision about who may approve (verify, override, check). When
    no trust ref carries the policy the approver chain is EMPTY, so nobody can be verified —
    before 2.0.0 this fell back to the working-tree copy with a warning, which let a branch name its
    own approvers (CLAUDE.md rule 3 said the opposite). trusted=False is only the requester path,
    which reads a TTL to put on the request and grants nothing.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load(trusted=trusted)
    if trusted and not L.trusted:
        print(f"[escalate] governance is UNTRUSTED — {L.why}. No approver can be verified.",
              file=sys.stderr)
    return L.data, L.source


def head_sha():
    """The commit carrying the CHANGE: latest commit touching anything outside the governance
    records. Governance commits (events, decisions) must not move the commit a release covers."""
    r = sh("git", "log", "-1", "--format=%H", "--", ".", f":(exclude){BASE}")
    return r.stdout.strip() or sh("git", "rev-parse", "HEAD").stdout.strip()


def rid_for(change, commit):
    """Deterministic: every request for the same change + commit is the same escalation."""
    return hashlib.sha256(f"{change}|{commit}".encode()).hexdigest()[:8].upper()


def actor():
    return os.environ.get("INTENT_AGENT_ID") or getpass.getuser()


def message(rid, change, commit, exp, decision, approver):
    return f"intent-escalation|v2|{rid}|{change}|{commit}|{exp}|{decision}|{approver}".encode()


def commit_records(msg):
    """Commit governance records. Several processes may share a working tree (two agents in one
    checkout), so retry on git's index lock; a failed commit never loses data — the files stay and
    the next commit picks them up."""
    sh("git", "add", "-A", BASE)
    for _ in range(5):
        r = sh("git", "commit", "-q", "-m", msg)
        if r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr):
            return
        if "index.lock" not in (r.stdout + r.stderr):
            return
        time.sleep(0.2 + secrets.randbelow(300) / 1000)
        sh("git", "add", "-A", BASE)


def event(action, change, commit, detail):
    """One file per event: branches never conflict on the log. Folded into the chain on main."""
    os.makedirs(f"{BASE}/events", exist_ok=True)
    t = now()
    name = f"{t.strftime('%Y%m%dT%H%M%S')}{t.microsecond:06d}_{action}_{secrets.token_hex(3)}.json"
    json.dump({"ts": t.isoformat(), "action": action, "change": change, "commit": commit,
               "actor": actor(), "detail": detail}, open(f"{BASE}/events/{name}", "w"), sort_keys=True)


# ---------------------------------------------------------------- approver side
def keygen(a):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as S
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(S.Encoding.PEM, S.PrivateFormat.PKCS8, S.NoEncryption())
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, pem); os.close(fd)
    pub = k.public_key().public_bytes(S.Encoding.Raw, S.PublicFormat.Raw)
    print(f"[escalate] private key written to {a.out} (mode 600) — keep it on YOUR machine only")
    print(f"[escalate] add this to governance.yaml under your chain entry:\n  pubkey: \"ed25519:{b64e(pub)}\"")
    return 0


def load_private(path):
    from cryptography.hazmat.primitives import serialization as S
    data = open(os.path.expanduser(path), "rb").read()
    try:
        return S.load_pem_private_key(data, password=None)
    except Exception:
        return S.load_ssh_private_key(data, password=None)


def decode_request(code):
    kind, body = code.strip().split(".", 1)
    assert kind == "ESCQ"
    return json.loads(b64d(body))


def sign(a):
    try:
        req = decode_request(a.request_code)
    except Exception:
        print("[escalate] not a valid request code", file=sys.stderr); return 1
    print(f"[escalate] Escalation {req['rid']}")
    print(f"  change : {req['change']}\n  commit : {req['commit']}\n  expires: {req['exp']}")
    print(f"  reason : {req['reason']}\n  raised : by {req['by']} ({req['trigger']})")
    print("  Review the change at that exact commit first — your code applies only to that commit.")
    if not a.yes and input(f"  {a.decision.upper()} this escalation? type YES: ").strip() != "YES":
        print("[escalate] not signed"); return 3
    sig = load_private(a.key).sign(message(req["rid"], req["change"], req["commit"], req["exp"], a.decision, a.approver))
    body = {"rid": req["rid"], "change": req["change"], "commit": req["commit"], "exp": req["exp"],
            "d": DECISION_CODE[a.decision], "apr": a.approver}
    print(f"\n[escalate] send this {a.decision.upper()} code back to the requester:")
    print(f"ESCR.{b64e(json.dumps(body, separators=(',', ':')).encode())}.{b64e(sig)}")
    return 0


# ---------------------------------------------------------------- shared verification
def decode_release(code):
    kind, body, sig = code.strip().split(".", 2)
    assert kind == "ESCR"
    d = json.loads(b64d(body))
    decision = {v: k for k, v in DECISION_CODE.items()}[d["d"]]
    return d, decision, b64d(sig)


def signature_ok(cfg, d, decision, sig):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    chain = {c.get("id"): c for c in (cfg.get("escalation") or {}).get("chain", [])}
    ent = chain.get(d["apr"])
    if not ent or not str(ent.get("pubkey", "")).startswith("ed25519:"):
        return False, f"approver '{d['apr']}' is not in the escalation chain"
    try:
        Ed25519PublicKey.from_public_bytes(b64d(ent["pubkey"].split(":", 1)[1])).verify(
            sig, message(d["rid"], d["change"], d["commit"], d["exp"], decision, d["apr"]))
        return True, ""
    except Exception:
        return False, "signature does not verify — not produced by that approver's key, or altered"


CODE_RE = __import__("re").compile(r"ESCR\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def comment_codes(a):
    """Release/reject codes posted as PR comments. An approver can record a decision directly on the
    PR, so a requester cannot suppress a rejection by never verifying it — and fork PRs, whose branch
    CI cannot write to, still have a place for decisions. Authorship is irrelevant: only the
    signature counts."""
    text = ""
    if getattr(a, "comments_json", None):
        text = json.dumps(json.load(open(a.comments_json)))
    elif getattr(a, "pr", None):
        # Via the forge adapter, so an approver can record a decision on a merge request, a work
        # item or a review thread on any forge, not only a GitHub PR comment. A forge that cannot
        # answer yields no codes, which fails closed: no release found.
        try:
            import sys as _s, os as _o
            _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
            import forge as FG
            # TRUSTED policy only. This read used `open(GOV)` — the working tree — and in CI the
            # working tree is the PR, so the PR chose the adapter and the gate executed it with the
            # token in scope. Reproduced before the fix; UAT-G28 is that attack.
            _gov, _src = load_cfg(trusted=True)
            cm = FG.Forge.from_config(_gov).comments(int(a.pr))
            if cm is None:
                # UNREADABLE is not the same as EMPTY. Rejections live in comments, so a token
                # revoked, a rate limit or a network blip silently turned "rejected by the approver"
                # into "no decision found" — and a stored release then stood. Say so and fail closed.
                print("[escalate] CANNOT READ the request's comments — a rejection recorded there "
                      "would be invisible. Treating the decision as UNKNOWN.", file=sys.stderr)
                return ["__COMMENTS_UNREADABLE__"]
            text = json.dumps(cm)
        except Exception as e:
            print(f"[escalate] CANNOT READ the request's comments ({e}). Treating the decision as "
                  f"UNKNOWN rather than as 'nothing was rejected'.", file=sys.stderr)
            return ["__COMMENTS_UNREADABLE__"]
    return sorted(set(CODE_RE.findall(text)))


def outcome(cfg, src, change, commit, extra_codes=(), allow_override=True):
    """Order-independent decision from ALL recorded decisions for change + commit.
    Never trusts a record's status field: every release/reject is re-verified from its code.

    EXPIRY IS ENFORCED HERE, not only in `verify`. `check` — what CI runs, and what reads decisions
    out of request comments — resolves through this function, so an expired release was honoured
    for ever on the one path that actually gates a merge. A rejection, by contrast, is NOT expired
    away: a refusal that lapses into permission would be the wrong failure direction."""

    def _expired(d):
        try:
            return now() > parse(d["exp"])
        except (KeyError, ValueError, TypeError):
            return True          # unparseable expiry is an unknown, and an unknown does not release
    esc = cfg.get("escalation") or {}
    rel, rej, ovr, ignored, expired = [], [], [], [], []
    for p in sorted(glob.glob(f"{BASE}/resolved/*.json")):
        r = json.load(open(p))
        if r.get("change") != change or r.get("commit") != commit:
            continue
        if r.get("kind") == "force-override":
            (ovr if (allow_override and esc.get("allow_local_override", False)) else ignored).append(r.get("by", "?"))
            continue
        try:
            d, decision, sig = decode_release(r["code"])
            ok, why = signature_ok(cfg, d, decision, sig)
            if not ok or d["change"] != change or d["commit"] != commit:
                ignored.append(os.path.basename(p)); continue
            if decision != "reject" and _expired(d):
                expired.append(os.path.basename(p)); continue
            (rej if decision == "reject" else rel).append(d["apr"])
        except Exception:
            ignored.append(os.path.basename(p))
    if "__COMMENTS_UNREADABLE__" in extra_codes:
        return 3, ("NOT RELEASED — the request's comments could not be read, and a rejection "
                   "recorded there would be invisible. An unknown decision does not release.")
    for code in extra_codes:                       # decisions posted as PR comments
        try:
            d, decision, sig = decode_release(code)
            ok, _ = signature_ok(cfg, d, decision, sig)
            if ok and d["change"] == change and d["commit"] == commit:
                if decision != "reject" and _expired(d):
                    expired.append("comment code"); continue
                (rej if decision == "reject" else rel).append(d["apr"])
        except Exception:
            pass
    if rej:
        extra = f" (a release by {', '.join(sorted(set(rel)))} does not override it)" if rel else ""
        return 3, f"BLOCKED — rejected by {', '.join(sorted(set(rej)))}{extra}. A rejection from any approver holds."
    if rel:
        return 0, f"RELEASED — by {', '.join(sorted(set(rel)))} (verified against {src})"
    if ovr:
        return 0, f"RELEASED BY OVERRIDE — by {', '.join(sorted(set(ovr)))} (overrides allowed on {src})"
    return 3, "NOT RELEASED — no valid decision recorded for this commit" + (
        f" ({len(ignored)} record(s) ignored: invalid or not allowed)" if ignored else "") + (
        f" ({len(expired)} EXPIRED release(s) found — ask the approver for a fresh code)"
        if expired else "")


# ---------------------------------------------------------------- requester / CI side
def request(a):
    cfg, _ = load_cfg(trusted=False)
    ttl = int((cfg.get("escalation") or {}).get("code_ttl_minutes", 120))
    commit = a.commit or head_sha()
    rid = rid_for(a.change, commit)
    # a rejection for this exact commit is final: asking again needs a new commit
    for p in glob.glob(f"{BASE}/resolved/{rid}-reject-*-*.json"):
        print(f"[escalate] BLOCKED — escalation {rid} for {a.change} at {commit[:10]} was already REJECTED "
              f"by {json.load(open(p)).get('approver')}. Fix the change and push a new commit.")
        return 3
    # Every writer writes its own uniquely named file — no path is ever written by two writers, so
    # concurrent runs rebase and merge cleanly. Several files may carry the same escalation id; they
    # are the same escalation.
    live = [json.load(open(p)) for p in sorted(glob.glob(f"{BASE}/pending/{rid}-*.json"))]
    live = [r for r in live if now() <= parse(r["exp"])]
    if live:
        rec = live[0]; fresh = False
    else:
        pend = f"{BASE}/pending/{rid}-{secrets.token_hex(3)}.json"
        rec = {"rid": rid, "change": a.change, "commit": commit, "exp": iso(now() + timedelta(minutes=ttl)),
               "reason": a.reason, "by": actor(), "trigger": a.trigger, "opened": now().isoformat()}
        os.makedirs(f"{BASE}/pending", exist_ok=True)
        json.dump(rec, open(pend, "w"), indent=1)
        event("ESCALATION-REQUEST", a.change, commit, f"rid={rid} trigger={a.trigger} reason={a.reason}")
        commit_records(f"escalation: request {rid} {a.change}")
        fresh = True
    code = "ESCQ." + b64e(json.dumps({k: rec[k] for k in ("rid", "change", "commit", "exp", "reason", "by", "trigger")}).encode())
    chain = (cfg.get("escalation") or {}).get("chain", [])
    who = ", ".join(f"{c.get('name')} <{c.get('contact')}>" for c in chain) or "(no escalation chain configured)"
    print(f"[escalate] BLOCKED — escalation {rid} {'opened' if fresh else 'already open'} for {a.change} "
          f"at {commit[:10]} (expires {rec['exp']})")
    print(f"[escalate] send this REQUEST code to an approver: {who}")
    print(code)
    print("[escalate] they run: intent_escalate.py sign <code> --key <their key> --approver <their id>")
    print("[escalate] then paste their code here: intent_escalate.py verify <code>  — and push")
    return 3


def verify(a):
    cfg, src = load_cfg(trusted=True)
    try:
        d, decision, sig = decode_release(a.release_code)
    except Exception:
        print("[escalate] not a valid release code", file=sys.stderr); return 1
    fail = None
    ok, why = signature_ok(cfg, d, decision, sig)
    if not ok:
        fail = f"{why} (chain read from {src})"
    elif now() > parse(d["exp"]):
        fail = f"code expired at {d['exp']}"
    elif head_sha() != d["commit"]:
        fail = (f"the change moved: this code is for {d['commit'][:10]}, HEAD is {head_sha()[:10]}. "
                f"A decision covers one exact commit — open a new escalation for the new commit")
    elif d["rid"] != rid_for(d["change"], d["commit"]):
        fail = "the code's escalation id does not match its change and commit"
    if fail:
        print(f"[escalate] REFUSED — {fail}", file=sys.stderr)
        event("VERIFY-REFUSED", d.get("change", "?"), d.get("commit", "?"), f"rid={d.get('rid')} {fail}")
        commit_records(f"escalation: refused {d.get('rid')}")
        return 3
    os.makedirs(f"{BASE}/resolved", exist_ok=True)
    already = glob.glob(f"{BASE}/resolved/{d['rid']}-{decision}-{d['apr']}-*.json")
    path = f"{BASE}/resolved/{d['rid']}-{decision}-{d['apr']}-{secrets.token_hex(3)}.json"
    if not already:   # idempotent here; across machines a duplicate is harmless (the outcome dedupes)
        json.dump({"rid": d["rid"], "change": d["change"], "commit": d["commit"], "decision": decision,
                   "approver": d["apr"], "code": a.release_code.strip(), "recorded": now().isoformat(),
                   "by": actor()}, open(path, "w"), indent=1)
        event("ESCALATION-" + decision.upper(), d["change"], d["commit"], f"rid={d['rid']} approver={d['apr']}")
        commit_records(f"escalation: {decision} {d['rid']} by {d['apr']}")
    code, msg = outcome(cfg, src, d["change"], d["commit"])
    print(f"[escalate] {decision} by {d['apr']} recorded for {d['change']} at {d['commit'][:10]}.")
    print(f"[escalate] outcome: {msg}")
    if code == 0:
        print("[escalate] Proceed to the next stage — push so CI sees the decision.")
    return code


def check(a):
    cfg, src = load_cfg(trusted=True)
    commit = a.commit or head_sha()
    code, msg = outcome(cfg, src, a.change, commit, comment_codes(a), allow_override=not a.no_override)
    print(f"[escalate] {a.change} at {commit[:10]}: {msg}")
    return code


def override(a):
    cfg, src = load_cfg(trusted=True)
    esc = cfg.get("escalation") or {}
    commit = head_sha()
    if not esc.get("allow_local_override", False):
        print(f"[escalate] REFUSED — local override is disabled by governance on {src}. "
              "An approver in the escalation chain must release this.", file=sys.stderr)
        event("OVERRIDE-REFUSED", a.change, commit, f"reason={a.reason}")
        commit_records(f"escalation: override refused {a.change}")
        return 3
    if esc.get("override_requires_reason", True) and len((a.reason or "").strip()) < 15:
        print("[escalate] REFUSED — an override needs a real reason (15+ characters).", file=sys.stderr)
        return 3
    rid = rid_for(a.change, commit)
    os.makedirs(f"{BASE}/resolved", exist_ok=True)
    json.dump({"rid": rid, "change": a.change, "commit": commit, "kind": "force-override",
               "by": actor(), "reason": a.reason, "recorded": now().isoformat()},
              open(f"{BASE}/resolved/{rid}-force-override-{actor()}-{secrets.token_hex(3)}.json", "w"), indent=1)
    event("FORCE-OVERRIDE", a.change, commit, f"rid={rid} reason={a.reason} config={src}")
    commit_records(f"escalation: FORCE-OVERRIDE {a.change}")
    code, msg = outcome(cfg, src, a.change, commit)
    print(f"[escalate] FORCE-OVERRIDE recorded for {a.change} by {actor()} — logged permanently.")
    print(f"[escalate] outcome: {msg}")
    return code


def status(a):
    for p in sorted(glob.glob(f"{BASE}/pending/*.json")):
        r = json.load(open(p))
        if not a.change or r["change"] == a.change:
            print(f"  open        {r['rid']}  {r['change']:<24} {r['commit'][:10]}  {r['reason'][:50]}")
    for p in sorted(glob.glob(f"{BASE}/resolved/*.json")):
        r = json.load(open(p))
        if not a.change or r["change"] == a.change:
            kind = r.get("decision") or r.get("kind")
            print(f"  {kind:<11} {r['rid']}  {r['change']:<24} {r['commit'][:10]}  {r.get('approver', r.get('by', ''))}")
    return 0


# ---------------------------------------------------------------- the log
# The guarantee is git history on the protected branch, not a hash chain. Every record is written
# once and never changed, so the rule is simple and checkable: under pending/, resolved/ and events/,
# files are only ever ADDED. Any modification, rename or deletion — including removing the most
# recent entries — is a violation, reported with the commit and its author. Protected-branch rules
# (no force-push, no history rewrite) keep that history itself intact. ESCALATIONS.log is a rendered,
# hash-chained EXPORT of the events for auditors, stamped with the commit it was rendered from.
APPEND_ONLY = [f"{BASE}/pending", f"{BASE}/resolved", f"{BASE}/events"]


def history_violations(rng=None):
    args = ["git", "log", "--format=@@%H|%an|%ad", "--date=short", "--name-status", "--no-renames",
            "--diff-filter=DMRT"] + ([rng] if rng else []) + ["--"] + APPEND_ONLY
    r = sh(*args)
    if r.returncode != 0:
        print(f"[escalate] CANNOT VERIFY the record history: {' '.join(args)} failed — "
              f"{(r.stderr or '').strip()[:160]}", file=sys.stderr)
        print("[escalate] An unreadable history is an UNKNOWN, not an all-clear. Check the range "
              "(a repository whose default branch is not `main` needs --range, or INTENT_TRUST_REF).",
              file=sys.stderr)
        return 3
    out, cur, v = r.stdout, None, []
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].split("|")
        elif line.strip() and cur:
            status, path = line.split("\t", 1)
            v.append((cur[0][:10], cur[1], cur[2], {"D": "DELETED", "M": "MODIFIED", "T": "CHANGED TYPE"}.get(status[0], status), path))
    return v


def log_verify(a):
    rng = getattr(a, "range", None)
    v = history_violations(rng)
    scope = f"range {rng}" if rng else "full history"
    if v:
        print(f"[escalate] GOVERNANCE RECORDS ALTERED ({scope}) — records are append-only:", file=sys.stderr)
        for sha, who, when, what, path in v:
            print(f"  {what:<12} {path}  in {sha} by {who} on {when}", file=sys.stderr)
        return 3
    n = len(glob.glob(f"{BASE}/events/*.json"))
    print(f"[escalate] records intact ({scope}): nothing added was ever modified or deleted · {n} event(s) on this branch")
    if os.path.exists(LOG):
        prev = "0" * 64
        for i, line in enumerate([l for l in open(LOG).read().splitlines() if l.strip()], 1):
            e = json.loads(line); h = e.pop("hash")
            if e["prev"] != prev or hashlib.sha256((prev + json.dumps(e, sort_keys=True)).encode()).hexdigest() != h:
                print(f"[escalate] EXPORT TAMPERED at entry {i} of {LOG}", file=sys.stderr)
                return 3
            prev = h
    return 0


def log_render(a):
    """Render the events into a hash-chained export. Not committed by CI — nothing pushes to main."""
    commit = sh("git", "rev-parse", "HEAD").stdout.strip()
    evs = sorted(((json.load(open(f)), f) for f in glob.glob(f"{BASE}/events/*.json")), key=lambda x: (x[0]["ts"], x[1]))
    prev, out = "0" * 64, []
    head = {"ts": now().isoformat(), "action": "EXPORT", "change": "-", "commit": commit, "actor": actor(),
            "detail": f"{len(evs)} events rendered from {commit}", "prev": prev}
    head["hash"] = hashlib.sha256((prev + json.dumps(head, sort_keys=True)).encode()).hexdigest()
    out.append(head); prev = head["hash"]
    for e, _ in evs:
        e = {**e, "prev": prev}
        e["hash"] = hashlib.sha256((prev + json.dumps(e, sort_keys=True)).encode()).hexdigest()
        out.append(e); prev = e["hash"]
    path = a.out or LOG
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").write("".join(json.dumps(e, sort_keys=True) + "\n" for e in out))
    print(f"[escalate] rendered {len(evs)} event(s) to {path}, stamped with {commit[:10]}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    s = ap.add_subparsers(dest="cmd", required=True)
    k = s.add_parser("keygen"); k.add_argument("--out", required=True)
    r = s.add_parser("request"); r.add_argument("--change", required=True); r.add_argument("--reason", required=True)
    r.add_argument("--commit"); r.add_argument("--trigger", default="user")
    g = s.add_parser("sign"); g.add_argument("request_code"); g.add_argument("--key", required=True)
    g.add_argument("--approver", required=True); g.add_argument("--decision", default="release", choices=["release", "reject"])
    g.add_argument("--yes", action="store_true")
    v = s.add_parser("verify"); v.add_argument("release_code")
    c = s.add_parser("check"); c.add_argument("--change", required=True); c.add_argument("--commit")
    c.add_argument("--pr"); c.add_argument("--comments-json"); c.add_argument("--no-override", action="store_true")
    o = s.add_parser("override"); o.add_argument("--change", required=True); o.add_argument("--reason", default="")
    t = s.add_parser("status"); t.add_argument("--change")
    lv = s.add_parser("log-verify"); lv.add_argument("--range")
    lr = s.add_parser("log-render"); lr.add_argument("--out")
    a = ap.parse_args()
    return {"keygen": keygen, "request": request, "sign": sign, "verify": verify, "check": check,
            "override": override, "status": status, "log-render": log_render,
            "log-verify": log_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
