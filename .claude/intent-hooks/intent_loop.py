#!/usr/bin/env python3
"""The loop driver: where every change is, what proves it may move on, and who is working on it.

    intent_loop.py stages                                   the stage list (one source: stages.py)
    intent_loop.py open <change> [--branch B]               start tracking a change
    intent_loop.py status <change> [--json]                 where it is, from its event log
    intent_loop.py next <change> [--json]                   the work to do now, and what proves it done
    intent_loop.py advance <change> [--stage S] [--pr N] [--merge-commit SHA] [--worker W]
                                                            check the stage's evidence; record done/failed
    intent_loop.py queue [--json]                           every open change, its stage and its lease
    intent_loop.py claim [--worker W] [--minutes M] [--change ID] [--json]  lease the next (or a named) change
    intent_loop.py renew <change> [--worker W] [--minutes M]
    intent_loop.py release <change> [--worker W] [--force]
                    --force drops a LIVE lease someone else holds — for a worker known to be dead,
                    rather than waiting out LOOP_LEASE_MINUTES. It is recorded in the change's log.
    intent_loop.py post-merge <change> --merge-commit SHA [--push]   the post-merge actor
    intent_loop.py assemble <change> [--push] [--dry-run]   one register fragment -> the register
    intent_loop.py --self-test

Exit codes: 0 ok · 1 error · 3 lost a race / leased by someone else · 4 not yet (evidence absent)
            5 failed (the stage's check failed; the change was sent back) · 6 push refused

Why this exists
---------------
Twenty-five skills were chained by a numbered table in CLAUDE.md and hand-off sentences in
SKILL.md files. Nothing could say where a change was, nothing picked work up, nothing ran after a
merge, and two archive runs could allocate the same register ids. This is the missing driver. It
decides nothing the gates decide: every stage is done only when its EVIDENCE says so (a committed
file, a structured report field, a commit reachable from the protected branch), and the gates
still run where they always ran. Every unknown is "not yet", never "done".

State — one event log per change, on a ref
------------------------------------------
`refs/intent/changes/<change>` is a chain of commits on the empty tree, one per event, the event as
JSON in the message — the pattern the lock refs already prove. Appending is a compare-and-set: a
force-with-lease push against the tip that was read (create-only for the first event), so two
workers can never both append on top of the same state. The ref is outside every branch, so every
clone and every branch sees the same state, and nothing a pull request contains can write it.

Work queue — leases on the existing lock refs
---------------------------------------------
A worker CLAIMS a change by acquiring the lease `refs/intent-locks/loop_<change>` (create-only push:
the server is the arbiter), works the current stage, ADVANCES it, and RELEASES. A lease expires
(LOOP_LEASE_MINUTES, default 60), so a worker that died does not hold a change forever, and an
expired lease is reclaimed only against the tip that was read. Only stages whose actor is `agent`
are claimable; `human` and `ci` stages wait for the person or the forge.

Commit binding
--------------
From `test` on, a stage's done-ness is bound to the commit it was proven on. If the change's head
moves by anything other than a report under docs/intent/reports/, those stages are no longer done
and the loop resumes at the first of them — an intent review of yesterday's code is not a review.

The post-merge actor
--------------------
`post-merge` runs on the protected branch after a merge (intent-post-merge.yml): it verifies the
merge commit is reachable from the protected branch, records it, releases the change's locks, and
assembles its register fragment — provisional ids promoted to permanent ones under a lease, so two
merges can never allocate the same id. The rest of archive-sync (graphs, docs sweep, compliance
matrix) is the `archive` stage, worked by an agent like any other.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import argparse, datetime as _dt, getpass, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stages as S                                      # same skill; installed alongside

NS = "refs/intent/changes"
LEASE_NS = "refs/intent-locks"
REMOTE = os.environ.get("INTENT_LOOP_REMOTE", "origin")
CONFIG = "docs/intent/concurrency.config"
REPORTS = "docs/intent/reports"
FRAGMENTS = "docs/intent/register.d"
REGISTER = "docs/intent/INTENT_REGISTER.md"
COMPLIANCE = "docs/intent/INTENT_COMPLIANCE.md"
EVENT_HEAD = "intent-loop-event"
# Paths a commit may touch without making the code it proved anything about different: the loop's
# own reports, and the escalation records the acceptance workflow commits to the PR branch (which
# used to send every change back to `test` the moment the gate wrote one).
NOT_CODE = (REPORTS + "/", "docs/intent/governance/")
RETRIES = 12                      # under contention; each retry backs off with jitter
EX_OK, EX_ERR, EX_RACE, EX_NOTYET, EX_FAILED, EX_PUSH = 0, 1, 3, 4, 5, 6


# ---------------------------------------------------------------- git plumbing

def git(*a, inp=None):
    return subprocess.run(["git", *a], input=inp, capture_output=True, text=True,
                          stdin=None if inp is not None else subprocess.DEVNULL)


def out(*a):
    r = git(*a)
    return r.stdout.strip() if r.returncode == 0 else ""


def has_remote():
    return git("remote", "get-url", REMOTE).returncode == 0


ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def valid_id(change):
    """A change id the loop accepts AS WRITTEN. Ids used to be rewritten (`/` → `_`), so `feat/x`
    and `feat_x` were one ref and finishing one archived the other. An id that would need rewriting
    is refused instead."""
    return bool(ID_OK.match(change or "")) and ".." not in change and not change.endswith(".lock")


def slug(s):
    return s if valid_id(s) else re.sub(r"[^A-Za-z0-9._-]", "_", s)


def now():
    return _dt.datetime.now(_dt.timezone.utc)


def cfg(key, default):
    try:
        for line in open(CONFIG, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"') or default
    except OSError:
        pass
    return default


def empty_tree():
    return out("hash-object", "-t", "tree", "/dev/null") or out("mktree")


def cas(ref, new, old):
    """Move `ref` from `old` ('' = must not exist) to `new`, atomically. True when it moved.

    With a remote, the remote is the arbiter (--force-with-lease against what was read); the
    local ref follows. Without one, `git update-ref` performs the same compare-and-set locally."""
    if has_remote():
        r = git("push", "-q", f"--force-with-lease={ref}:{old}", REMOTE, f"{new}:{ref}")
        if r.returncode != 0:
            return False
        git("update-ref", ref, new)
        return True
    return git("update-ref", ref, new, old or "0" * 40).returncode == 0


def fetch(pattern):
    if has_remote():
        git("fetch", "-q", "--prune", REMOTE, f"+{pattern}:{pattern}")


def protected_ref():
    """The protected branch, FULLY QUALIFIED (a tag or branch named `origin/main` must not stand in
    for it — the 2.0.0 review took over the trust ref that way). With a remote only the remote's
    branch counts, never a local one an agent can commit to; $INTENT_TRUST_REF, when it names a
    branch, is the only candidate."""
    pin = (os.environ.get("INTENT_TRUST_REF") or "").strip()
    if pin:
        q = pin if pin.startswith("refs/") else ("refs/remotes/" + pin if "/" in pin else None)
        cands = (q,) if q and q.startswith(("refs/remotes/", "refs/heads/")) else ()
    elif has_remote():
        cands = (f"refs/remotes/{REMOTE}/main", f"refs/remotes/{REMOTE}/master")
    else:
        cands = ("refs/heads/main", "refs/heads/master")
    for ref in cands:
        if out("rev-parse", "--verify", "--quiet", ref):
            return ref
    return None


def branch_name(ref):
    """`refs/remotes/origin/main` or `refs/heads/main` -> `main`."""
    for pre in (f"refs/remotes/{REMOTE}/", "refs/heads/"):
        if ref.startswith(pre):
            return ref[len(pre):]
    return ref


# ---------------------------------------------------------------- the event log

def ref_of(change):
    return f"{NS}/{slug(change)}"


def read_events(change):
    """[event dicts], oldest first. A commit whose message is not an event is skipped, and said."""
    ref = ref_of(change)
    if not out("rev-parse", "--verify", "--quiet", ref):
        return []
    log = git("log", "--reverse", "--format=%H%x00%B%x1e", ref).stdout
    evs = []
    for chunk in log.split("\x1e"):
        sha, _, body = chunk.strip("\n").partition("\x00")
        if not sha:
            continue
        head, _, js = body.partition("\n\n")
        try:
            ev = json.loads(js) if head.strip() == EVENT_HEAD else None
        except ValueError:
            ev = None
        if isinstance(ev, dict):
            ev["_sha"] = sha
            evs.append(ev)
        else:
            print(f"[loop] {ref}: commit {sha[:10]} is not a loop event — ignored", file=sys.stderr)
    return evs


def append(change, event):
    """Append one event with compare-and-set. Returns the new tip, or None after losing every race."""
    ref = ref_of(change)
    event = dict(event, at=now().isoformat(timespec="seconds"),
                 by=os.environ.get("INTENT_AGENT_ID") or getpass.getuser())
    msg = f"{EVENT_HEAD}\n\n{json.dumps(event, sort_keys=True)}\n"
    tree = empty_tree()
    for attempt in range(RETRIES):
        fetch(ref)
        old = out("rev-parse", "--verify", "--quiet", ref)
        args = ["commit-tree", tree] + (["-p", old] if old else [])
        new = git(*args, inp=msg).stdout.strip()
        if new and cas(ref, new, old):
            return new
        _backoff(attempt)
    return None


def _backoff(attempt):
    import random, time
    time.sleep(min(2.0, 0.05 * (2 ** attempt)) * random.uniform(0.5, 1.5))


# ---------------------------------------------------------------- folding events into a state

def fold(change, evs):
    st = {"change": change, "opened": False, "branch": None, "pr": None, "merge_commit": None,
          "done": {}, "last_failure": None, "assembled": None, "archived": False}
    for ev in evs:
        _apply(st, ev)
    return st


def _str(v):
    return v if isinstance(v, str) else None


def _apply(st, ev):
    """Fold one event. Anyone who can push can write the chain, so every field is type-checked: an
    event of the wrong shape is ignored, never allowed to raise (a TypeError here once stopped the
    notices for EVERY change, not only the forged one)."""
    t = ev.get("type")
    st["last_at"] = _str(ev.get("at")) or st.get("last_at")
    if t == "open":
        st["opened"], st["branch"] = True, _str(ev.get("branch"))
    elif t == "done" and _str(ev.get("stage")) in S.BY_NAME:
        st["done"][ev["stage"]] = ev
        st["pr"] = ev.get("pr") or st["pr"]
        st["merge_commit"] = ev.get("merge_commit") or st["merge_commit"]
    elif t == "failed" and _str(ev.get("rewind_to")) in S.BY_NAME:
        cut = S.NAMES.index(ev["rewind_to"])
        st["done"] = {k: v for k, v in st["done"].items() if S.NAMES.index(k) < cut}
        st["last_failure"] = ev
    elif t == "assembled":
        st["assembled"] = ev
    if t == "done" and _str(ev.get("stage")) == "archive":
        st["archived"] = True


def head_of(st):
    """The change's head commit, as the REMOTE has it. A stale clone used to read its own copy of
    the branch (or its own HEAD) and record a stage done on an old commit — rewinding every other
    worker. With a remote the branch is fetched and only the remote's tip counts; a branch the
    remote does not have is no head at all. Without a remote, the local branch, else HEAD."""
    b = st.get("branch")
    if has_remote():
        if not b:
            return None
        git("fetch", "-q", REMOTE, f"+refs/heads/{b}:refs/remotes/{REMOTE}/{b}")
        return out("rev-parse", "--verify", "--quiet", f"refs/remotes/{REMOTE}/{b}^{{commit}}") or None
    for ref in ([f"refs/heads/{b}"] if b else []) + ["HEAD"]:
        sha = out("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if sha:
            return sha
    return None


def code_moved(old, new):
    """True when `new` differs from `old` by more than reports — or when that cannot be shown."""
    if not old or not new:
        return True
    if old == new:
        return False
    if git("merge-base", "--is-ancestor", old, new).returncode != 0:
        return True                                    # rewritten history: nothing is carried over
    # --no-renames: a rename of code INTO reports/ would otherwise show only its new path.
    changed = [p for p in out("diff", "--name-only", "--no-renames", old, new).splitlines() if p]
    return any(not p.startswith(NOT_CODE) for p in changed)


def current_stage(st, head):
    """(stage name | TERMINAL, [stale stage names]). A stage is done when its event says so — and,
    for a bound stage, only while the code it was proven on is still the code."""
    if st["archived"]:
        return S.TERMINAL, []
    merged = "merged" in st["done"]
    stale = []
    for s in S.STAGES:
        ev = st["done"].get(s.name)
        if ev is None and not (merged and S.NAMES.index(s.name) < S.NAMES.index("merged")):
            return s.name, stale
        if ev is not None and s.bound and not merged and code_moved(ev.get("head"), head):
            stale.append(s.name)
            return s.name, stale
    return S.TERMINAL, stale


# ---------------------------------------------------------------- what proves each stage

def show(head, path):
    """A file's text at a commit, or None. Evidence is read from the COMMIT, never the worktree."""
    if not head:
        return None
    r = git("show", f"{head}:{path}")
    return r.stdout if r.returncode == 0 else None


def report(head, prefix, st):
    """The report for this change: keyed by change id during the local loop, by PR number after."""
    keys = [slug(st["change"])] + ([str(st["pr"])] if st.get("pr") else [])
    for k in keys:
        text = show(head, f"{REPORTS}/{prefix}_{k}.md")
        if text is not None:
            return text, f"{prefix}_{k}.md"
    return None, f"{prefix}_{keys[0]}.md"


def _verdicts():
    import verdicts
    return verdicts


def chk_committed(rel):
    def check(ctx):
        path = rel.replace("<id>", ctx["change"])
        return ("done", f"{path} is committed") if show(ctx["head"], path) is not None \
            else ("not-yet", f"{path} is not committed on the change's head")
    return check


def chk_gate(prefix):
    def check(ctx):
        text, name = report(ctx["head"], prefix, ctx["st"])
        if text is None:
            return "not-yet", f"no {name} on the change's head"
        rec = _verdicts().facts(text)
        if not rec["structured"] or rec["problems"]:
            return "not-yet", (f"{name} has no valid intent-report/1 block "
                               f"({'; '.join(rec['problems'][:2]) or 'none'}) — prose is not evidence")
        if rec["gate"] in ("PASS", "PASS-WITH-UPDATES"):
            return "done", f"{name}: gate {rec['gate']}"
        if rec["gate"] == "BLOCK":
            return "failed", f"{name}: gate BLOCK"
        return "not-yet", f"{name} records no gate outcome"
    return check


def chk_implement(ctx):
    base = protected_ref()
    if not base or not ctx["head"]:
        return "not-yet", "no protected branch or head to compare against"
    # Commits that touch something OUTSIDE docs/intent/ — the intent, spec and plan commits are
    # not an implementation, and counted as one they let `implement` pass with no code.
    n = out("rev-list", "--count", f"{base}..{ctx['head']}", "--", ".", ":(exclude)docs/intent")
    return ("done", f"{n} commit(s) beyond {base} change code") if n.isdigit() and int(n) > 0 \
        else ("not-yet", f"nothing beyond {base} touches anything outside docs/intent/")


def _ledger(ctx):
    text, name = report(ctx["head"], "FIX_LEDGER", ctx["st"])
    return (_verdicts().facts(None, text) if text is not None else None), name


def chk_review(ctx):
    rec, name = _ledger(ctx)
    if rec is None:
        return "not-yet", f"no {name}"
    if not rec["structured"] or rec["open_findings"] is None:
        return "not-yet", f"{name} has no intent-report/1 findings block"
    return "done", f"{name}: {len(rec['open_findings'])} open finding(s) to fix"


def chk_fix(ctx):
    rec, name = _ledger(ctx)
    if rec is None or rec["open_findings"] is None:
        return "not-yet", f"no readable {name}"
    if rec["open_findings"] or rec["unparseable_findings"] or rec["problems"]:
        return "not-yet", (f"{name}: {len(rec['open_findings'])} open, "
                           f"{len(rec['unparseable_findings'])} unparseable, "
                           f"{len(rec['problems'])} block problem(s)")
    return "done", f"{name}: every finding resolved"


def chk_intent_review(ctx):
    text, name = report(ctx["head"], "PR_INTENT", ctx["st"])
    if text is None:
        return "not-yet", f"no {name}"
    rec = _verdicts().facts(text)
    if not rec["structured"] or rec["problems"]:
        return "not-yet", f"{name} has no valid intent-report/1 block — prose is not evidence"
    if rec["verdict"] in ("APPROVE", "APPROVE-WITH-FOLLOW-UPS"):
        return "done", f"{name}: {rec['verdict']}"
    if rec["verdict"] in ("REQUEST-CHANGES", "BLOCK"):
        return "failed", f"{name}: {rec['verdict']}"
    return "not-yet", f"{name} records no verdict"


def chk_pr(ctx):
    pr = ctx.get("pr") or ctx["st"].get("pr")
    return ("done", f"pull request #{pr}") if pr else ("not-yet", "no pull request number given (--pr)")


def chk_merge_gate(ctx):
    if not (ctx.get("pr") or ctx["st"].get("pr")):
        return "not-yet", "no pull request recorded"
    text, name = report(ctx["head"], "MERGE_GATE", dict(ctx["st"], pr=ctx.get("pr") or ctx["st"]["pr"]))
    if text is None:
        return "not-yet", f"no {name}"
    rec = _verdicts().facts(text)
    if not rec["structured"] or rec["problems"]:
        return "not-yet", f"{name} has no valid intent-report/1 block"
    if rec["decision"] in ("AUTO-MERGE", "HUMAN-REVIEW"):
        return "done", f"{name}: {rec['decision']}"
    if rec["decision"] == "BLOCK":
        return "failed", f"{name}: BLOCK"
    return "not-yet", f"{name} records no decision"


def chk_merged(ctx):
    sha, base = ctx.get("merge_commit") or ctx["st"].get("merge_commit"), protected_ref()
    if not sha or not base:
        return "not-yet", "no merge commit given (--merge-commit), or no protected branch"
    if git("merge-base", "--is-ancestor", sha, base).returncode != 0:
        return "not-yet", f"{sha[:10]} is not reachable from {base}"
    return "done", f"{sha[:10]} is on {base}"


def chk_archive(ctx):
    st = ctx["st"]
    frag = show(protected_ref() or "HEAD", f"{FRAGMENTS}/{slug(st['change'])}.md")
    if frag is not None and not st.get("assembled"):
        return "not-yet", "the register fragment has not been assembled (post-merge)"
    m = st.get("merge_commit")
    if not m:
        return "not-yet", "no merge recorded"
    later = out("log", "--format=%H", f"{m}..{protected_ref() or 'HEAD'}", "--", COMPLIANCE)
    return ("done", f"{COMPLIANCE} regenerated after the merge") if later \
        else ("not-yet", f"{COMPLIANCE} has not been regenerated since {m[:10]}")


def chk_intent(ctx):
    """Acceptance is a HUMAN act, recorded as a merge to the protected branch (intent-change-record):
    the evidence is an intent.md that says `Status: accepted` ON THE PROTECTED BRANCH. A file an
    agent commits on its own branch proves only that it was written."""
    path = f"docs/intent/changes/{ctx['change']}/intent.md"
    base = protected_ref()
    text = show(base, path) if base else None
    if text is None:
        return "not-yet", f"{path} is not on the protected branch — an intent is accepted by merging it"
    if not re.search(r"(?im)^\W*Status:\s*accepted\b", text):
        return "not-yet", f"{path} is on {base} but does not say `Status: accepted`"
    return "done", f"{path} accepted on {base}"


CHECKS = {
    "intent": chk_intent,
    "spec": chk_gate("SPEC_GATE"),
    "plan": chk_committed("docs/intent/changes/<id>/plan.md"),
    "implement": chk_implement,
    "test": chk_gate("TESTS"),
    "review": chk_review,
    "fix": chk_fix,
    "intent_review": chk_intent_review,
    "pr": chk_pr,
    "merge_gate": chk_merge_gate,
    "merged": chk_merged,
    "archive": chk_archive,
}


# ---------------------------------------------------------------- leases (the work queue)

def lease_ref(change):
    return f"{LEASE_NS}/loop_{slug(change)}"


def read_lease(change):
    ref = lease_ref(change)
    sha = out("rev-parse", "--verify", "--quiet", ref)
    if not sha:
        return None
    meta = {}
    for line in out("log", "-1", "--format=%B", sha).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip()] = v.strip()
    meta["_sha"] = sha
    return meta


def lease_live(meta):
    try:
        return bool(meta) and _dt.datetime.fromisoformat(meta["expires"]) > now()
    except (KeyError, ValueError):
        return False                                   # an unreadable lease is not a live one


def take_lease(change, worker, minutes, renew=False):
    """(ok, holder-or-reason). Create-only, or reclaim/renew against the exact tip that was read."""
    ref = lease_ref(change)
    fetch(ref)
    cur = read_lease(change)
    if lease_live(cur) and cur.get("worker") != worker:
        return False, f"leased by {cur.get('worker')} until {cur.get('expires')}"
    if renew and not (cur and cur.get("worker") == worker):
        return False, "you hold no lease on it to renew"
    exp = (now() + _dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    body = (f"loop-lease: {change}\n\nresource: loop:{change}\nworker: {worker}\n"
            f"change_id: {change}\nholder: {worker}\nacquired: {now().isoformat(timespec='seconds')}\n"
            f"expires: {exp}\n")
    new = git("commit-tree", empty_tree(), inp=body).stdout.strip()
    if new and cas(ref, new, cur["_sha"] if cur else ""):
        if cur and cur.get("worker") != worker:
            _audit_reclaim(change, cur, worker)
        return True, exp
    return False, "another worker moved first"


def _audit_reclaim(change, cur, worker):
    """An expired lease taken over is recorded the way intent_lock records one: an immutable audit
    ref per event (refs/intent-locks-audit/). Before this the driver reclaimed silently while the
    skill said every reclaim was audited."""
    try:
        import intent_lock
        intent_lock.REMOTE = REMOTE
        intent_lock.audit("reclaim-expired", f"loop:{change}", worker,
                          f"previous={cur.get('worker')} expired={cur.get('expires')}")
    except Exception as e:                                   # noqa: BLE001
        print(f"[loop] WARNING: the reclaim of {change} could not be audited ({e})", file=sys.stderr)


def drop_lease(change, worker, force=False):
    fetch(lease_ref(change))          # a lease taken from another clone is invisible until fetched
    cur = read_lease(change)
    if not cur:
        return True
    if cur.get("worker") != worker and lease_live(cur) and not force:
        return False
    if has_remote():
        git("push", "-q", f"--force-with-lease={lease_ref(change)}:{cur['_sha']}", REMOTE,
            f":{lease_ref(change)}")
    git("update-ref", "-d", lease_ref(change), cur["_sha"])
    return True


# ---------------------------------------------------------------- commands

def worker_of(a):
    return getattr(a, "worker", None) or os.environ.get("INTENT_AGENT_ID") or getpass.getuser()


def load(change):
    fetch(ref_of(change))
    st = fold(change, read_events(change))
    head = head_of(st)
    stage, stale = current_stage(st, head)
    return st, head, stage, stale


def describe(change):
    st, head, stage, stale = load(change)
    s = S.BY_NAME.get(stage)
    fetch(lease_ref(change))
    lease = read_lease(change)
    return {"change": change, "tracked": st["opened"], "stage": stage,
            "actor": s.actor if s else None, "owner": s.owner if s else None,
            "last_at": st.get("last_at"), "waiting_on": _waiting_on(st, head, stage),
            "routing": ({"kind": s.kind, "stage": s.name} if s and s.kind else None),
            "proves": s.proves if s else None, "stale": stale, "head": head,
            "pr": st["pr"], "merge_commit": st["merge_commit"],
            "last_failure": (st["last_failure"] or {}).get("why"),
            "lease": ({"worker": lease.get("worker"), "expires": lease.get("expires"),
                       "live": lease_live(lease)} if lease else None)}


def _waiting_on(st, head, stage):
    """'human' when an agent stage cannot move without a person (a NEEDS-USER finding), else None."""
    if stage != "fix":
        return None
    rec, _ = _ledger({"change": st["change"], "st": st, "head": head})
    rows = (rec or {}).get("open_findings") or []
    return "human" if any("NEEDS-USER" in (status or "") for _, _, status in rows) else None


def cmd_open(a):
    if not valid_id(a.change):
        print(f"[loop] refused: {a.change!r} is not a change id (letters, digits, '.', '_', '-'; "
              f"no '..'). Ids are never rewritten: two ids that rewrote alike would be one change")
        return EX_ERR
    fetch(ref_of(a.change))
    if read_events(a.change):
        print(f"[loop] {a.change} is already tracked")
        return EX_RACE
    tip = append(a.change, {"type": "open", "branch": a.branch or f"change/{a.change}"})
    print(f"[loop] tracking {a.change}" if tip else f"[loop] could not record {a.change}")
    return EX_OK if tip else EX_RACE


def cmd_status(a):
    d = describe(a.change)
    if a.json:
        print(json.dumps(d, indent=1))
        return EX_OK if d["tracked"] else EX_ERR
    if not d["tracked"]:
        print(f"[loop] {a.change} is not tracked — intent_loop.py open {a.change}")
        return EX_ERR
    print(f"[loop] {a.change}: stage {d['stage']}"
          + (f" ({d['actor']}: {d['owner']})" if d["actor"] else "")
          + (f" — STALE: the code moved since {', '.join(d['stale'])} was proven" if d["stale"] else "")
          + (f" — last sent back: {d['last_failure']}" if d["last_failure"] else ""))
    if d["proves"]:
        print(f"       done when: {d['proves']}")
    return EX_OK


def cmd_next(a):
    d = describe(a.change)
    if a.json:
        print(json.dumps(d, indent=1))
    elif d["stage"] == S.TERMINAL:
        print(f"[loop] {a.change} is archived — nothing to do")
    else:
        r = d["routing"]
        print(f"[loop] {a.change} → {d['stage']} · {d['actor']} · {d['owner']}\n"
              f"       done when: {d['proves']}"
              + (f"\n       route:     model_route.py --kind {r['kind']} --stage {r['stage']} --announce"
                 if r and d["actor"] == "agent" else ""))
    return EX_OK if d["tracked"] else EX_ERR


def _lease_blocks(change, worker):
    cur = read_lease(change)
    return lease_live(cur) and cur.get("worker") != worker, cur


def cmd_advance(a):
    st, head, stage, _ = load(a.change)
    if not st["opened"]:
        print(f"[loop] {a.change} is not tracked")
        return EX_ERR
    if a.stage and a.stage != stage:
        print(f"[loop] refused: {a.change} is at {stage}, not {a.stage} — stages are not skipped")
        return EX_ERR
    if stage == S.TERMINAL:
        print(f"[loop] {a.change} is archived")
        return EX_OK
    behind = _behind(st, head, stage)
    if behind:
        print(f"[loop] refused: {behind}")
        return EX_RACE
    # Advancing needs the lease. It is fetched from the remote (a stale copy refused the right
    # worker and admitted the wrong one), and when nobody holds it this call takes it for its own
    # duration — so two workers can never advance one change at once, claim or no claim.
    worker = worker_of(a)
    fetch(lease_ref(a.change))
    cur = read_lease(a.change)
    if lease_live(cur) and cur.get("worker") != worker:
        print(f"[loop] refused: {a.change} is leased by {cur.get('worker')} until {cur.get('expires')}")
        return EX_RACE
    mine = lease_live(cur)
    if not mine:
        ok, why = take_lease(a.change, worker, 5)
        if not ok:
            print(f"[loop] refused: {why}")
            return EX_RACE
    try:
        ctx = {"change": a.change, "st": st, "head": head, "pr": a.pr, "merge_commit": a.merge_commit}
        verdict, why = CHECKS[stage](ctx)
        return _record(a.change, stage, verdict, why, head, a)
    finally:
        if not mine:
            drop_lease(a.change, worker)


def _behind(st, head, stage):
    """A reason this head may not be recorded, or None. Once a commit has been recorded for the
    change, an event may only be recorded on that commit or a descendant of it: a worker on a stale
    clone used to record `test done` on an older commit and send everyone back."""
    # Not for the stages whose evidence is not on the change's branch: the accepted intent and the
    # merge live on the protected branch, and after a (squash) merge the branch's commits are not
    # ancestors of anything that follows.
    if "merged" in st["done"] or stage in ("intent", "merged", "archive"):
        return None
    seen = [e.get("head") for e in st["done"].values() if e.get("head")]
    if st.get("last_failure") and st["last_failure"].get("head"):
        seen.append(st["last_failure"]["head"])
    if not head:
        return "the change's branch is not on the remote — nothing to record against"
    for h in seen:
        if h != head and git("merge-base", "--is-ancestor", h, head).returncode != 0:
            return (f"the head {head[:10]} does not contain {h[:10]}, already recorded for this "
                    f"change — a stale or rewritten branch cannot move the loop backwards")
    return None


def _record(change, stage, verdict, why, head, a):
    if verdict == "not-yet":
        print(f"[loop] {change}: {stage} is NOT done — {why}")
        return EX_NOTYET
    if verdict == "failed":
        back = S.BY_NAME[stage].on_fail or stage
        tip = append(change, {"type": "failed", "stage": stage, "rewind_to": back, "why": why,
                              "head": head})
        print(f"[loop] {change}: {stage} FAILED — {why}; back to {back}" if tip
              else f"[loop] {change}: could not record the failure (lost every race)")
        return EX_FAILED if tip else EX_RACE
    ev = {"type": "done", "stage": stage, "why": why, "head": head}
    if a.pr:
        ev["pr"] = a.pr
    if a.merge_commit:
        ev["merge_commit"] = a.merge_commit
    tip = append(change, ev)
    print(f"[loop] {change}: {stage} done — {why} → next: {S.after(stage)}" if tip
          else f"[loop] {change}: could not record the advance (lost every race)")
    return EX_OK if tip else EX_RACE


def tracked_changes():
    fetch(f"{NS}/*")
    refs = out("for-each-ref", "--format=%(refname)", NS).splitlines()
    return [r[len(NS) + 1:] for r in refs if r.strip()]


def cmd_queue(a):
    fetch(f"{LEASE_NS}/*")
    rows = [describe(c) for c in tracked_changes()]
    rows = [r for r in rows if r["stage"] != S.TERMINAL]
    if a.json:
        print(json.dumps(rows, indent=1))
        return EX_OK
    if not rows:
        print("[loop] no change in flight")
    for r in rows:
        ls = r["lease"]
        who = (f"leased by {ls['worker']}" if ls and ls["live"] else "free")
        waits = "  ← waiting on a person" if (r.get("waiting_on") or r["actor"] == "human") else ""
        print(f"  {r['change']:<32} {r['stage']:<14} {r['actor']:<6} {who}{waits}")
    return EX_OK


def cmd_claim(a):
    fetch(f"{LEASE_NS}/*")
    worker = worker_of(a)
    # Oldest-moved first, so a change that keeps failing cannot starve the rest by sorting first;
    # `--change` claims one by name. A change whose fix stage is waiting on a NEEDS-USER finding is
    # a human's turn, whatever the stage's usual actor.
    rows = [describe(c) for c in ([a.change] if getattr(a, "change", None) else tracked_changes())]
    rows.sort(key=lambda d: d.get("last_at") or "")
    for d in rows:
        c = d["change"]
        if d["actor"] != "agent" or d.get("waiting_on") or (d["lease"] and d["lease"]["live"]):
            continue
        ok, info = take_lease(c, worker, a.minutes)
        if ok:
            d["lease"] = {"worker": worker, "expires": info, "live": True}
            print(json.dumps(d, indent=1) if a.json
                  else f"[loop] claimed {c} at {d['stage']} until {info}")
            return EX_OK
    print("[loop] nothing to claim — every agent stage is leased, or none is waiting")
    return EX_RACE


def cmd_renew(a):
    ok, info = take_lease(a.change, worker_of(a), a.minutes, renew=True)
    print(f"[loop] lease on {a.change} renewed until {info}" if ok else f"[loop] refused: {info}")
    return EX_OK if ok else EX_RACE


def cmd_release(a):
    fetch(lease_ref(a.change))
    held = read_lease(a.change) if a.force else None
    ok = drop_lease(a.change, worker_of(a), force=a.force)
    if ok and held and lease_live(held) and held.get("worker") != worker_of(a):
        append(a.change, {"type": "lease-forced", "from": held.get("worker"),
                          "expires": held.get("expires")})
    print(f"[loop] lease on {a.change} released" if ok else
          f"[loop] refused: the live lease on {a.change} is someone else's (--force to take it)")
    return EX_OK if ok else EX_RACE


# ---------------------------------------------------------------- the post-merge actor

def cmd_post_merge(a):
    st, _, _, _ = load(a.change)
    if not st["opened"]:
        print(f"[loop] {a.change} is not tracked — nothing to finalise")
        return EX_ERR
    if "merged" not in st["done"]:
        verdict, why = chk_merged({"st": st, "merge_commit": a.merge_commit})
        if verdict != "done":
            print(f"[loop] refused: {why}")
            return EX_NOTYET
        if not append(a.change, {"type": "done", "stage": "merged", "why": why,
                                 "merge_commit": a.merge_commit, "head": a.merge_commit}):
            return EX_RACE
    _release_change_locks(a.change)
    rc = cmd_assemble(a)
    if rc in (EX_OK,):
        print(f"[loop] {a.change}: merged and assembled — next: archive (an agent runs "
              f"intent-archive-sync)")
    return rc


def _release_change_locks(change):
    """Every lock this change held, released — the first step of archive-sync, now mechanical."""
    fetch(f"{LEASE_NS}/*")
    n = 0
    for ref in out("for-each-ref", "--format=%(refname)", LEASE_NS).splitlines():
        body = out("log", "-1", "--format=%B", ref)
        if re.search(rf"^change_id:\s*{re.escape(change)}\s*$", body, re.M):
            sha = out("rev-parse", ref)
            if has_remote():
                git("push", "-q", f"--force-with-lease={ref}:{sha}", REMOTE, f":{ref}")
            git("update-ref", "-d", ref, sha)
            n += 1
    print(f"[loop] released {n} lock(s) held by {change}")


# ---------------------------------------------------------------- register fragment assembly

PERM = re.compile(r"\bC-(\d{3,})\b")


def provisional(change):
    return re.compile(r"\bC-" + re.escape(change) + r"-(\d+)\b")


def promote(register_text, fragment_text, change):
    """(fragment with permanent ids, {provisional: permanent}). Sequential after the register's max."""
    used = [int(m) for m in PERM.findall(register_text)]
    width = max([3] + [len(m) for m in PERM.findall(register_text)])
    nxt = (max(used) if used else 0) + 1
    seen = []
    for m in provisional(change).finditer(fragment_text):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    seen.sort(key=lambda pid: int(pid.rsplit("-", 1)[1]))
    ids = {pid: f"C-{nxt + i:0{width}d}" for i, pid in enumerate(seen)}
    text = provisional(change).sub(lambda m: ids[m.group(0)], fragment_text)
    return text, ids


def sections(text):
    """[(heading line, [body lines])] for every `## ` section."""
    out_, cur = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = (line, [])
            out_.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return out_


def _category(heading):
    return heading[3:].split("—")[0].split(" - ")[0].strip().upper()


def insert_rows(reg_lines, heading, rows, header):
    """Append claim rows to the end of the table under the register section that matches."""
    cat = _category(heading)
    for i, line in enumerate(reg_lines):
        if line.startswith("## ") and _category(line) == cat:
            j = i + 1
            while j < len(reg_lines) and not reg_lines[j].startswith("|"):
                j += 1
            while j < len(reg_lines) and reg_lines[j].startswith("|"):
                j += 1
            return reg_lines[:j] + rows + reg_lines[j:]
    return reg_lines + ["", heading] + header + rows


def supersede(reg_lines, old, new, date):
    """Set the Status cell of claim `old` to SUPERSEDED by `new`. False if `old` is not found."""
    header = None
    for i, line in enumerate(reg_lines):
        if line.startswith("| ID "):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if cells and cells[0] == old and header and "Status" in header:
            k = header.index("Status")
            cells[k] = f"SUPERSEDED by {new} ({date})"
            reg_lines[i] = "| " + " | ".join(cells) + " |"
            return True
    return False


def apply_fragment(register_text, fragment_text, change, date):
    """(new register text, ids, problems). Pure: the whole assembly, testable without git."""
    frag, ids = promote(register_text, fragment_text, change)
    reg = register_text.splitlines()
    problems = []
    for heading, body in sections(frag):
        cat = _category(heading)
        if cat == "SUPERSESSIONS":
            for m in re.finditer(r"(C-\d+)\s*(?:→|->|SUPERSEDED by)\s*(C-\d+)", "\n".join(body)):
                if not supersede(reg, m.group(1), m.group(2), date):
                    problems.append(f"{m.group(1)} is not in the register, so it cannot be superseded")
        elif cat == "CHANGELOG":
            entries = [l for l in body if l.strip().startswith("-")]
            bad = [l for l in entries if re.match(r"^-\s*(Version|Date|Built by|Confidence):", l.strip())]
            if bad:
                problems.append(f"a changelog line impersonates a register header line: {bad[0]!r}")
            reg = insert_rows(reg, "## Changelog", entries, [])
        else:
            table = [l for l in body if l.startswith("|")]
            rows = [l for l in table if PERM.match(l.strip("| ").split("|")[0].strip() or "")]
            reg = insert_rows(reg, heading, rows, [l for l in table if l not in rows])
    return "\n".join(_bump_version(reg)) + "\n", ids, problems


def _bump_version(reg):
    """Bump the version in the HEADER only — the first `- Version:` line before the first section.
    It used to bump every such line, so a fragment changelog entry `- Version: 99` became a second
    version line, bumped to 100."""
    out_, done = [], False
    for line in reg:
        if not done and line.startswith("## "):
            done = True
        if not done and line.startswith("- Version:"):
            line = re.sub(r"(Version:\s*)(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1),
                          line, count=1)
            done = True
        out_.append(line)
    return out_


def cmd_assemble(a):
    st, _, _, _ = load(a.change)
    if st.get("assembled"):
        print(f"[loop] {a.change}: already assembled — {st['assembled'].get('ids')}")
        return EX_OK
    dry = getattr(a, "dry_run", False)
    if has_remote() and not dry and not getattr(a, "push", False):
        print("[loop] refused: with a remote, an assembly is final only once it is pushed — the ids "
              "it promotes are not reserved until then. Re-run with --push (the workflow does).")
        return EX_ERR
    ok, why = take_lease("register-assembly", worker_of(a), 15)
    if not ok:
        print(f"[loop] register assembly is busy — {why}")
        return EX_RACE
    try:
        return _assemble_leased(a, dry)
    finally:
        drop_lease("register-assembly", worker_of(a))


def _assemble_leased(a, dry):
    """Everything that reads the register happens UNDER the lease and against a FRESH protected
    tip. The check used to run before the lease, against this clone's last fetch, so two actors
    could both pass it and both promote into the same ids (reproduced: C-003 twice)."""
    if has_remote():
        git("fetch", "-q", REMOTE)
    base = protected_ref()
    if not base:
        print("[loop] refused: no protected branch to assemble on")
        return EX_ERR
    if not dry and out("rev-parse", "HEAD") != out("rev-parse", base):
        print(f"[loop] refused: HEAD is not {base}. Assembly runs on the protected branch's tip — "
              f"pull, then re-run")
        return EX_RACE
    frag_path = f"{FRAGMENTS}/{slug(a.change)}.md"
    # Read from the PROTECTED BRANCH, not the working tree: a checkout that lacks the file (a
    # detached or empty clone) must not conclude there is nothing to assemble and say so for good.
    if show(base, frag_path) is None:
        print(f"[loop] {a.change}: {base} carries no register fragment — nothing to assemble")
        return EX_OK if dry else _mark_assembled(a.change, {})
    return _assemble_locked(a, frag_path, base)


def _assemble_locked(a, frag_path, base):
    reg_text = show(base, REGISTER) or ""
    frag_text = show(base, frag_path)
    new, ids, problems = apply_fragment(reg_text, frag_text, a.change, now().date().isoformat())
    for p in problems:
        print(f"[loop] PROBLEM: {p}")
    if problems:
        return EX_ERR
    if getattr(a, "dry_run", False):
        sys.stdout.write(new)
        print(f"[loop] dry run — would promote {ids}", file=sys.stderr)
        return EX_OK
    os.makedirs(os.path.dirname(REGISTER), exist_ok=True)
    open(REGISTER, "w", encoding="utf-8").write(new)
    if os.path.exists(frag_path):
        os.remove(frag_path)
    git("add", "--", REGISTER, frag_path)
    msg = (f"intent-loop: assemble register fragment {a.change}\n\n"
           + "".join(f"{k} -> {v}\n" for k, v in ids.items())
           + "\nGenerated-by: intent_loop.py assemble (deterministic; the fragment was reviewed "
             "in the merged change)\n")
    if git("commit", "-q", "-m", msg, "--", REGISTER, frag_path).returncode != 0:
        print("[loop] could not commit the assembly")
        return EX_ERR
    if getattr(a, "push", False) and has_remote():
        rc = _push_assembly(base)
        if rc == EX_PUSH:
            # Recorded so the notifier can tell someone: a register update is now a pull request
            # waiting for a person (intent_notify.py, reason assembly_pr).
            append(a.change, {"type": "assembly-pr", "why": "branch protection refused the push"})
        if rc != EX_OK:
            return rc
    return _mark_assembled(a.change, ids)


def _push_assembly(base):
    """Push the assembly commit. A refusal because the branch MOVED is a race: the commit is undone
    and the caller re-runs against the new tip (exit 3). Only a refusal with the branch unmoved is
    branch protection (exit 6) — it used to be reported as protection either way, and the workflow
    then opened a pull request carrying ids another merge had already taken."""
    branch = branch_name(base)
    parent = out("rev-parse", "HEAD~1")
    if git("push", "-q", REMOTE, f"HEAD:refs/heads/{branch}").returncode == 0:
        git("fetch", "-q", REMOTE)
        return EX_OK
    git("fetch", "-q", REMOTE)
    if out("rev-parse", base) != parent:
        git("reset", "-q", "--keep", parent)
        print(f"[loop] {branch} moved while assembling — the assembly is undone; pull and re-run")
        return EX_RACE
    print(f"[loop] PUSH REFUSED to {branch}: the assembly is committed locally. Branch protection "
          f"must allow this actor, or open the assembly as a pull request")
    return EX_PUSH


def _mark_assembled(change, ids):
    tip = append(change, {"type": "assembled", "ids": ids, "commit": out("rev-parse", "HEAD")})
    print(f"[loop] {change}: assembled {ids or '(no fragment)'}" if tip
          else f"[loop] {change}: assembled, but the event could not be recorded")
    return EX_OK if tip else EX_RACE


# ---------------------------------------------------------------- self-test

def _self_test():
    import tempfile, shutil
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        if not cond:
            print(f"  FAIL {label}")

    reg = ("# Intent Register — t\n- Version: 3  |  Date: 2026-10-01\n\n## FUNC — Functional\n"
           "| ID | Claim | Source | Decider | Priority | Type | Status | Conflicts |\n|---|---|---|---|---|---|---|---|\n"
           "| C-001 | a | S-01 | PM | MUST | TESTABLE | ACTIVE | — |\n"
           "| C-007 | b | S-01 | PM | MUST | TESTABLE | ACTIVE | — |\n\n## Changelog\n- v3 start\n")
    frag = ("# Register fragment — chg-9\n\n## FUNC — Functional\n"
            "| ID | Claim | Source | Decider | Priority | Type | Status | Conflicts |\n|---|---|---|---|---|---|---|---|\n"
            "| C-chg-9-02 | second | S-01 | PM | MUST | TESTABLE | ACTIVE | — |\n"
            "| C-chg-9-01 | first, replaces C-007 | S-01 | PM | MUST | TESTABLE | ACTIVE | — |\n\n"
            "## NEG — Non-goals\n| ID | Claim | Source | Decider | Priority | Type | Status | Conflicts |\n"
            "|---|---|---|---|---|---|---|---|\n| C-chg-9-03 | never x | S-01 | PM | MUST | TESTABLE | ACTIVE | — |\n\n"
            "## Supersessions\n- C-007 → C-chg-9-01\n\n## Changelog\n- chg-9: two claims, one non-goal\n")
    new, ids, probs = apply_fragment(reg, frag, "chg-9", "2026-10-05")
    check("ids follow the register's max, in provisional order",
          ids == {"C-chg-9-01": "C-008", "C-chg-9-02": "C-009", "C-chg-9-03": "C-010"})
    check("no provisional id survives", "C-chg-9" not in new)
    check("rows land in their category", new.index("| C-009 |") < new.index("## Changelog")
          and new.index("| C-008 |") > new.index("## FUNC"))
    check("a category the register lacks is created", "## NEG" in new and "| C-010 |" in new)
    check("supersession sets the status", "SUPERSEDED by C-008 (2026-10-05)" in new)
    check("the changelog is appended", "- chg-9: two claims" in new)
    check("the version is bumped", "- Version: 4" in new and not probs)
    inj = frag.replace("- chg-9: two claims, one non-goal", "- Version: 99")
    _, _, probs = apply_fragment(reg, inj, "chg-9", "d")
    check("a changelog line cannot impersonate the header", bool(probs))
    check("only the header version is bumped", new.count("- Version:") == 1)
    _, _, probs = apply_fragment(reg, frag.replace("C-007 →", "C-099 →"), "chg-9", "d")
    check("superseding a missing claim is a problem, not silence", bool(probs))
    # A second merge promotes AFTER the first, never into the same numbers.
    new2, ids2, _ = apply_fragment(new, frag.replace("chg-9", "chg-10"), "chg-10", "d")
    check("a later fragment never reuses an id", set(ids2.values()).isdisjoint(ids.values()))

    # The state machine, on a real repository with no remote: events, binding, rewinds, leases.
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(*c)
        os.makedirs("docs/intent/changes/c1")
        open("README", "w").write("x\n")
        git("add", "."); git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "change/c1")
        A = argparse.Namespace
        check("open", cmd_open(A(change="c1", branch=None)) == EX_OK)
        check("a second open loses", cmd_open(A(change="c1", branch=None)) == EX_RACE)
        adv = lambda **k: cmd_advance(A(change="c1", stage=None, pr=None, merge_commit=None,  # noqa: E731
                                        worker="w1", **k))
        check("intent not yet: nothing committed", adv() == EX_NOTYET)
        # Written on the change branch by an agent: NOT accepted. Acceptance is a merge to the
        # protected branch with `Status: accepted` — a human act the driver cannot be talked into.
        open("docs/intent/changes/c1/intent.md", "w").write("# i\nStatus: accepted\n")
        git("add", "."); git("commit", "-q", "-m", "intent on the branch")
        check("an intent on the change branch is not an accepted intent", adv() == EX_NOTYET)
        git("checkout", "-q", "main")
        os.makedirs("docs/intent/changes/c1", exist_ok=True)
        open("docs/intent/changes/c1/intent.md", "w").write("# i\nStatus: draft\n")
        git("add", "."); git("commit", "-q", "-m", "draft on main")
        check("a draft on the protected branch is not accepted", adv() == EX_NOTYET)
        open("docs/intent/changes/c1/intent.md", "w").write("# i\nStatus: accepted\n")
        git("add", "."); git("commit", "-q", "-m", "accepted on main")
        git("checkout", "-q", "change/c1"); git("merge", "-q", "-X", "theirs", "main", "-m", "sync")
        check("intent done once accepted on the protected branch", adv() == EX_OK)
        check("a stage cannot be skipped", cmd_advance(A(change="c1", stage="plan", pr=None,
              merge_commit=None, worker="w1")) == EX_ERR)
        os.makedirs("docs/intent/reports", exist_ok=True)
        open("docs/intent/reports/SPEC_GATE_c1.md", "w").write("**Verdict: PASS**\n")
        git("add", "."); git("commit", "-q", "-m", "spec prose")
        check("a prose-only gate report is not evidence", adv() == EX_NOTYET)
        open("docs/intent/reports/SPEC_GATE_c1.md", "w").write("---\nintent-report: 1\ngate: PASS\n---\n")
        git("add", "."); git("commit", "-q", "-m", "spec")
        check("spec done on a structured PASS", adv() == EX_OK)
        open("docs/intent/changes/c1/plan.md", "w").write("# p\n")
        git("add", "."); git("commit", "-q", "-m", "plan")
        check("plan", adv() == EX_OK)
        check("implement is not done by intent, spec and plan commits alone", adv() == EX_NOTYET)
        open("a.py", "w").write("print(1)\n")
        git("add", "."); git("commit", "-q", "-m", "code")
        check("implement", adv() == EX_OK)
        open("docs/intent/reports/TESTS_c1.md", "w").write("---\nintent-report: 1\ngate: PASS\n---\n")
        git("add", "."); git("commit", "-q", "-m", "tests")
        check("test", adv() == EX_OK)
        _, _, stage, _ = load("c1")
        check("a reports-only commit does not unbind", stage == "review")
        open("a.py", "w").write("print(2)\n")
        git("add", "."); git("commit", "-q", "-m", "code moved")
        _, _, stage, stale = load("c1")
        check("a code change unbinds the bound stages", stage == "test" and stale == ["test"])
        check("re-proving on the new head", adv() == EX_OK)
        led = ("---\nintent-report: 1\nfindings:\n  - {id: F-1, severity: MAJOR, status: OPEN, "
               "security: false}\n---\n")
        open("docs/intent/reports/FIX_LEDGER_c1.md", "w").write(led)
        git("add", "."); git("commit", "-q", "-m", "ledger")
        check("review done on a structured ledger", adv() == EX_OK)
        check("fix not done while a finding is open", adv() == EX_NOTYET)
        open("docs/intent/reports/FIX_LEDGER_c1.md", "w").write(led.replace("OPEN", "VERIFIED"))
        git("add", "."); git("commit", "-q", "-m", "fixed")
        check("fix done when closed", adv() == EX_OK)
        open("docs/intent/reports/PR_INTENT_c1.md", "w").write("---\nintent-report: 1\nverdict: REQUEST-CHANGES\n---\n")
        git("add", "."); git("commit", "-q", "-m", "review says no")
        check("a REQUEST-CHANGES verdict fails the stage", adv() == EX_FAILED)
        _, _, stage, _ = load("c1")
        check("...and sends the change back to fix", stage == "fix")
        check("leases: one worker", take_lease("c1", "w1", 5)[0])
        check("leases: a second worker is refused", not take_lease("c1", "w2", 5)[0])
        check("a leased change cannot be advanced by another", cmd_advance(A(change="c1", stage=None,
              pr=None, merge_commit=None, worker="w2")) == EX_RACE)
        check("leases: release", drop_lease("c1", "w1") and take_lease("c1", "w2", 5)[0])
        drop_lease("c1", "w2")
        # A stale head may not move the loop backwards.
        git("checkout", "-q", "-b", "stale", "HEAD~3")
        st_ = fold("c1", read_events("c1"))
        check("a head that does not contain recorded commits is refused",
              _behind(st_, out("rev-parse", "HEAD"), "fix") is not None)
        git("checkout", "-q", "change/c1")
        check("the current head is accepted", _behind(st_, out("rev-parse", "HEAD"), "fix") is None)
        check("an id that would need rewriting is refused", cmd_open(A(change="feat/x", branch=None)) == EX_ERR
              and not valid_id("a..b") and valid_id("claims-export-2026.09_x"))
        check("garbage in the log is skipped, not fatal",
              git("update-ref", ref_of("c2"), git("commit-tree", empty_tree(), inp="junk\n")
                  .stdout.strip()).returncode == 0 and fold("c2", read_events("c2"))["opened"] is False)
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print(f"[loop] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true", help="run the driver's own battery")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("stages")
    for name in ("open", "status", "next", "advance", "renew", "release", "post-merge", "assemble"):
        s = sub.add_parser(name)
        s.add_argument("change")
        s.add_argument("--json", action="store_true")
        s.add_argument("--branch")
        s.add_argument("--stage", choices=S.NAMES)
        s.add_argument("--pr", type=int)
        s.add_argument("--merge-commit")
        s.add_argument("--worker")
        s.add_argument("--minutes", type=int, default=int(cfg("LOOP_LEASE_MINUTES", "60")))
        s.add_argument("--push", action="store_true")
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--force", action="store_true")
    for name in ("queue", "claim"):
        s = sub.add_parser(name)
        s.add_argument("--json", action="store_true")
        s.add_argument("--worker")
        s.add_argument("--change", help="claim: this change, by name, rather than the next in line")
        s.add_argument("--minutes", type=int, default=int(cfg("LOOP_LEASE_MINUTES", "60")))
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.cmd:
        ap.print_help()
        return EX_ERR
    if a.cmd == "stages":
        for i, s in enumerate(S.STAGES, 1):
            print(f"{i:>2}. {s.name:<14} {s.actor:<6} {s.owner}\n    done when: {s.proves}")
        return EX_OK
    return {"open": cmd_open, "status": cmd_status, "next": cmd_next, "advance": cmd_advance,
            "queue": cmd_queue, "claim": cmd_claim, "renew": cmd_renew, "release": cmd_release,
            "post-merge": cmd_post_merge, "assemble": cmd_assemble}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
