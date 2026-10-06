#!/usr/bin/env python3
"""Ref-based leases for the intent loop.

The lock IS a git ref: refs/intent-locks/<slug>. Creating a ref that already exists is
rejected by the server, so acquisition is an atomic compare-and-set — no merge semantics,
no commits on main, nothing to race on. Metadata lives in the lock commit's message.
The human-readable LOCKS.md is GENERATED from the refs and is never authoritative.

Usage (--force steals a live lease; it is logged and should be rare):
  intent_lock.py acquire claim:C-014 --change <id> [--holder NAME] [--worktree PATH]
  intent_lock.py release claim:C-014 --change <id>
  intent_lock.py release-all --change <id>
  intent_lock.py list [--change <id>] [--json]
  intent_lock.py render                     # write docs/intent/locks/LOCKS.md from refs
  intent_lock.py force-release claim:C-014 --reason "holder on leave"
  intent_lock.py prune                      # delete expired lock refs

Exit: 0 acquired/released, 3 lost the race, 1 error.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, argparse, subprocess, getpass
from datetime import datetime, timezone, timedelta

NS = "refs/intent-locks"
CONFIG = "docs/intent/concurrency.config"
LOCK_DIR = "docs/intent/locks"
AUDIT = os.path.join(LOCK_DIR, "AUDIT.log")
REMOTE = os.environ.get("INTENT_LOCK_REMOTE", "origin")
RETRIES = 3


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def cfg(key, default):
    try:
        for line in open(CONFIG):
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].split("#", 1)[0].strip()
    except FileNotFoundError:
        pass
    return default


def now():
    return datetime.now(timezone.utc)


def slug(resource):
    return re.sub(r"[^A-Za-z0-9._-]", "_", resource)


def ref_of(resource):
    return f"{NS}/{slug(resource)}"


def fetch_locks():
    sh("git", "fetch", "--prune", REMOTE, f"+{NS}/*:{NS}/*")


def _meta_from_ref(ref):
    sha = sh("git", "rev-parse", "--verify", "--quiet", ref).stdout.strip()
    if not sha:
        return None
    body = sh("git", "log", "-1", "--format=%B", sha).stdout
    d = {}
    for line in body.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    if not d:
        return None
    d["_sha"] = sha
    return d


def read_lock(resource):
    return _meta_from_ref(ref_of(resource))


def all_locks():
    fetch_locks()
    out = []
    for line in sh("git", "for-each-ref", "--format=%(refname)", NS).stdout.splitlines():
        lk = _meta_from_ref(line.strip())
        if lk:
            lk.setdefault("resource", line.strip().split("/")[-1])
            out.append(lk)
    return out


def is_live(lk):
    if not lk or "expires" not in lk:
        return False
    try:
        return datetime.fromisoformat(lk["expires"].replace("Z", "+00:00")) > now()
    except Exception:
        return False


def make_lock_commit(meta):
    """Orphan commit on an empty tree, metadata in the message. Worktree untouched."""
    tree = sh("git", "hash-object", "-t", "tree", "/dev/null").stdout.strip()
    if not tree:
        tree = sh("git", "mktree").stdout.strip()
    r = subprocess.run(["git", "commit-tree", tree], input=meta, capture_output=True, text=True)
    return r.stdout.strip()


AUDIT_NS = "refs/intent-locks-audit"


def audit(action, resource, who, detail=""):
    """Record a force-release or expired-reclaim as ONE IMMUTABLE REF per event.

    This used to append to docs/intent/locks/AUDIT.log, `git add` it, commit on whatever branch was
    checked out, and `git push origin HEAD:main` — so a reclaim run from a change branch committed
    that branch's staged work and pushed the change branch to main. A single-file append is also
    the one shape that is not safe when agents run concurrently.

    Now it is the pattern the lock itself already uses: an orphan commit on an empty tree carrying
    the record in its message, pushed create-only to its own ref. No working tree, no index, no
    branch is touched, and two agents can never collide. `intent_lock.py audit-log` reads them.
    """
    stamp = now().strftime("%Y%m%dT%H%M%S%fZ")
    body = (f"lock-audit: {action} {resource}\n\n"
            f"time: {now().isoformat()}\naction: {action}\nresource: {resource}\n"
            f"by: {who}\ndetail: {detail}\n")
    commit = make_lock_commit(body)
    if not commit:
        print(f"[lock] WARNING: could not record the audit event {action} {resource}",
              file=sys.stderr)
        return False
    slug = ref_of(resource).rsplit("/", 1)[-1]
    ref = f"{AUDIT_NS}/{stamp}-{slug}"
    r = sh("git", "push", REMOTE, f"{commit}:{ref}")
    if r.returncode != 0:
        # The remote may be unreachable; keep the record locally rather than lose it.
        sh("git", "update-ref", ref, commit)
    return True


def audit_log():
    """Every recorded audit event, oldest first, from the audit refs (remote and local)."""
    sh("git", "fetch", "-q", REMOTE, f"+{AUDIT_NS}/*:{AUDIT_NS}/*")
    refs = sh("git", "for-each-ref", "--sort=refname", "--format=%(objectname)", AUDIT_NS)
    out = []
    for sha in (refs.stdout or "").split():
        msg = sh("git", "log", "-1", "--format=%B", sha).stdout or ""
        out.append(" | ".join(l.split(": ", 1)[1] for l in msg.splitlines()
                              if l.split(": ", 1)[0] in ("time", "action", "resource", "by",
                                                          "detail") and ": " in l))
    return out


def acquire(a):
    resource, change = a.resource, a.change
    holder = a.holder or os.environ.get("INTENT_AGENT_ID") or getpass.getuser()
    ttl = int(cfg("LOCK_TTL_HOURS", "24"))
    for attempt in range(RETRIES):
        fetch_locks()
        cur = read_lock(resource)
        if is_live(cur):
            if cur.get("change_id") == change:
                print(f"[lock] already held by this change ({resource})")
                return 0
            print(f"[lock] LOST \u2014 {resource} held by {cur.get('holder')} "
                  f"(change {cur.get('change_id')}, expires {cur.get('expires')})")
            return 3
        exp = (now() + timedelta(hours=ttl)).isoformat()
        meta = (f"resource: {resource}\nholder: {holder}\nchange_id: {change}\n"
                f"acquired: {now().isoformat()}\nexpires: {exp}\n"
                + (f"worktree: {a.worktree}\n" if a.worktree else ""))
        commit = make_lock_commit(meta)
        if not commit:
            print("[lock] could not create lock object")
            return 1
        if cur:
            # Expired ref present: reclaim, but only if nobody changed it since we looked.
            push = sh("git", "push",
                      f"--force-with-lease={ref_of(resource)}:{cur['_sha']}",
                      REMOTE, f"{commit}:{ref_of(resource)}")
            if push.returncode == 0:
                audit("reclaim-expired", resource, holder,
                      f"previous={cur.get('holder')} expired={cur.get('expires')}")
        else:
            # Create-only: the server rejects this if the ref already exists. This is the CAS.
            push = sh("git", "push", REMOTE, f"{commit}:{ref_of(resource)}")
        if push.returncode == 0:
            print(f"[lock] ACQUIRED {resource} \u2192 {holder} (expires {exp})")
            return 0
        print(f"[lock] push rejected (attempt {attempt + 1}/{RETRIES}) \u2014 someone moved first, re-checking")
    cur = read_lock(resource)
    print(f"[lock] could not acquire {resource} after {RETRIES} attempts"
          + (f" \u2014 held by {cur.get('holder')}" if cur else ""))
    return 3


def _delete_ref(resource):
    ok = sh("git", "push", REMOTE, f":{ref_of(resource)}").returncode == 0
    sh("git", "update-ref", "-d", ref_of(resource))
    return ok


def release(a, resource=None, quiet=False):
    resource = resource or a.resource
    fetch_locks()
    lk = read_lock(resource)
    if not lk:
        if not quiet:
            print(f"[lock] no lock on {resource}")
        return 0
    if getattr(a, "change", None) and lk.get("change_id") != a.change and not getattr(a, "force", False):
        print(f"[lock] refusing: {resource} belongs to change {lk.get('change_id')}, not {a.change}")
        return 1
    if _delete_ref(resource):
        print(f"[lock] released {resource}")
        return 0
    print(f"[lock] failed to release {resource}")
    return 1


def release_all(a):
    n = 0
    for lk in all_locks():
        if lk.get("change_id") == a.change:
            release(a, lk["resource"], quiet=True)
            n += 1
    print(f"[lock] released {n} lock(s) for change {a.change}")
    return 0


def lst(a):
    rows = [lk for lk in all_locks() if not a.change or lk.get("change_id") == a.change]
    for r in rows:
        r["live"] = is_live(r)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("[lock] no locks held")
        return 0
    w = max(len(r.get("resource", "")) for r in rows) + 2
    for r in rows:
        print(f"  {'LIVE   ' if r['live'] else 'EXPIRED'} {r.get('resource',''):<{w}}"
              f"{r.get('holder','?'):<14} change={r.get('change_id','?'):<26} expires={r.get('expires','?')}")
    return 0


def render(a):
    rows = all_locks()
    os.makedirs(LOCK_DIR, exist_ok=True)
    L = ["# Locks (generated from refs/intent-locks/* \u2014 do not edit)",
         f"Generated: {now().isoformat()}", "",
         "| Resource | Holder | Change | Expires | State |", "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: x.get("resource", "")):
        L.append(f"| {r.get('resource','')} | {r.get('holder','?')} | {r.get('change_id','?')} | "
                 f"{r.get('expires','?')} | {'live' if is_live(r) else 'EXPIRED'} |")
    if not rows:
        L.append("| \u2014 | \u2014 | \u2014 | \u2014 | none held |")
    open(os.path.join(LOCK_DIR, "LOCKS.md"), "w").write("\n".join(L) + "\n")
    print(f"[lock] rendered {len(rows)} lock(s) \u2192 {LOCK_DIR}/LOCKS.md")
    return 0


def force_release(a):
    fetch_locks()
    lk = read_lock(a.resource)
    if not lk:
        print(f"[lock] no lock on {a.resource}")
        return 0
    who = os.environ.get("INTENT_AGENT_ID") or getpass.getuser()
    if _delete_ref(a.resource):
        audit("FORCE-RELEASE", a.resource, who, f"from={lk.get('holder')} reason={a.reason}")
        print(f"[lock] FORCE-RELEASED {a.resource} (was {lk.get('holder')}) \u2014 audited")
        return 0
    print("[lock] force-release failed")
    return 1


def prune(a):
    n = 0
    for lk in all_locks():
        if not is_live(lk) and _delete_ref(lk["resource"]):
            n += 1
    print(f"[lock] pruned {n} expired lock ref(s)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("acquire", "release", "force-release"):
        s = sub.add_parser(name)
        s.add_argument("resource")
        s.add_argument("--change")
        s.add_argument("--holder")
        s.add_argument("--worktree")
        s.add_argument("--reason", default="")
        s.add_argument("--force", action="store_true")
    s = sub.add_parser("release-all")
    s.add_argument("--change", required=True)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("--change")
    s.add_argument("--json", action="store_true")
    sub.add_parser("render")
    sub.add_parser("prune")
    sub.add_parser("audit-log")
    a = ap.parse_args()
    if cfg("CONCURRENCY_MODE", "detect") != "reserve" and a.cmd in ("acquire", "release", "release-all"):
        print(f"[lock] CONCURRENCY_MODE is not 'reserve' \u2014 locking is disabled (set it in {CONFIG})")
        return 0
    if a.cmd == "audit-log":
        for line in audit_log():
            print(line)
        return 0
    return {"acquire": acquire, "release": release, "release-all": release_all, "list": lst,
            "render": render, "force-release": force_release, "prune": prune}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
