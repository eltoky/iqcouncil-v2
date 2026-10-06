#!/usr/bin/env python3
"""Tell the human on the loop, and prove it, before anything merges.

    notify.py send   --change ID --commit SHA [--pr N] [--config PATH] [--dry-run]
    notify.py check  [--config PATH]          # deliver a test notice now
    notify.py list   [--change ID]            # the receipts, newest first
    notify.py --self-test

WHAT THIS IS FOR

`autonomy.mode: hotl` claims a human is told each time the loop merges and can intervene. Until
this existed, the suite checked that `notify.channel` was a non-empty string and nothing else — so
a channel that had been dead since March read as "a human is watching". That is the same defect as
the 1.8.0 installer bug, which proved hooks block by checking an exit code that a MISSING
interpreter also returns. The lesson from that one is the design of this one:

    REQUIRE THE ARTIFACT, NOT THE EXIT CODE.

A notifier must exit 0 **and** print a delivery token — a message id, a URL, a ticket number,
anything the operator can follow back to the actual message. A program that does not exist exits
127. A program that exists and silently does nothing exits 0 with no output, and that is refused,
because a notifier that returns nothing has not been shown to have delivered anything.

ORDER MATTERS, AND IT IS NOT NEGOTIABLE

The notice goes out BEFORE the merge, and a failure to deliver BLOCKS the merge. Notifying after
the merge would give nobody the chance to intervene, which is the whole content of being "on" the
loop rather than out of it. So: tell them first, merge second, and if telling fails, do not merge.

The cost is explicit and worth stating plainly: if the notification path is down, hotl repositories
stop merging. That is the trade the mode makes. A repository that would rather keep merging
unwatched is describing hootl, and should declare hootl — where no notice is required and nobody
pretends otherwise.

WHAT THE RECEIPT IS, AND WHAT IT IS NOT

Each delivery writes one file under the receipts directory: change, commit, channel, token, time.
One file per notice, never appended to a shared log, for the same reason escalation records are one
file each — a single log conflicts on every merge.

The receipt is an AUDIT ARTIFACT, not a trust input. The gate does not look for an existing receipt
and proceed if it finds one; it performs a fresh delivery in the privileged run and proceeds only
if that delivery succeeds. This distinction is the security of the thing: receipts live in the
working tree, the working tree during an acceptance run is the branch under review, and a branch
that could satisfy the gate by committing a file would have defeated it. So a fabricated receipt
buys exactly nothing.

THE NOTIFIER CONTRACT  (`intent-notice/1`)

`autonomy.notify.command` names a program, READ FROM THE TRUST REF ONLY — a branch that could name
its own notifier would be naming a program the privileged step then runs, which is the
remote-execution shape that `forge.adapter` had until 1.8.0.

  in     a JSON object on STDIN: version, change, commit, pr, mode, channel, subject, body,
         policy_owner, repo, generated_at
  out    a non-empty delivery token on STDOUT, exit 0
  fail   any non-zero exit, empty output, or no answer within timeout_seconds

Nothing from the request — title, branch name, body, author — is ever placed in the command's
argv. It arrives as JSON on stdin, where it is data. Interpolating a pull-request title into a
command line is how a title becomes a shell.

Exit: 0 delivered (the token is printed) · 1 NOT delivered · 2 usage error · 3 no notice required.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, io, re, sys, json, time, shlex, hashlib, argparse, subprocess
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GOV = "docs/intent/governance.yaml"
CONTRACT = "intent-notice/1"
DEFAULT_RECEIPTS = "docs/intent/governance/notices"
DEFAULT_TIMEOUT = 20
MAX_TOKEN = 400
PLACEHOLDER_RE = re.compile(r"\bREPLACE\b|\bTODO\b|\bCHANGEME\b|example\.(?:com|org|net)", re.I)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dict(node, key):
    if not isinstance(node, dict):
        return {}
    got = node.get(key)
    return got if isinstance(got, dict) else {}


def notify_cfg(cfg):
    """The notify block, normalised, with the defaults that make absence safe."""
    n = _dict(_dict(cfg, "autonomy"), "notify")
    try:
        timeout = max(1, min(300, int(n.get("timeout_seconds") or DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return {
        "channel": str(n.get("channel") or "").strip(),
        "command": str(n.get("command") or "").strip(),
        "timeout": timeout,
        "receipts": str(n.get("receipts") or DEFAULT_RECEIPTS).strip() or DEFAULT_RECEIPTS,
    }


def usable(value):
    """A configured value that is actually a value, rather than the template's."""
    return bool(value) and not PLACEHOLDER_RE.search(value)


def payload(*, change, commit, pr, mode, channel, policy_owner, repo, subject=None, body=None):
    """What the notifier receives. One shape, versioned, so a notifier can be written once."""
    return {
        "contract": CONTRACT,
        "change": change,
        "commit": commit,
        "pr": pr,
        "mode": mode,
        "channel": channel,
        "policy_owner": policy_owner,
        "repo": repo,
        "subject": subject or f"[{mode}] {change} is about to merge at {str(commit)[:12]}",
        "body": body or (
            f"The intent loop is about to merge change {change} at commit {commit} with no "
            f"per-change human approval, under autonomy mode '{mode}'. The accountable human is "
            f"{policy_owner}. Every gate passed and the change cleared every unattended fence. "
            f"If this should not merge, stop it now."),
        "generated_at": now(),
    }


# Running a notifier is ONE implementation, shared with the waiting-on-a-person sweeper
# (intent_notify.py): shared/notice_io.py. Kept importable from here under the old names.
def notifier_argv(cmd):
    sys.path.insert(0, HERE)
    import notice_io
    return notice_io.notifier_argv(cmd)


def deliver(cmd, data, timeout):
    sys.path.insert(0, HERE)
    import notice_io
    return notice_io.deliver(cmd, data, timeout)


def write_receipt(root, receipts, data, token):
    """One file per notice. Returns its path, or None when it could not be written."""
    d = os.path.join(root, receipts, re.sub(r"[^A-Za-z0-9._-]+", "-", data["change"]))
    stamp = data["generated_at"].replace(":", "").replace("-", "")
    name = f"{str(data['commit'])[:12]}-{stamp}.md"
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True).encode() + token.encode()).hexdigest()[:16]
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        io.open(p, "w", encoding="utf-8").write(
            f"# Notice — {data['change']} at {str(data['commit'])[:12]}\n\n"
            f"- contract: `{CONTRACT}`\n- change: {data['change']}\n- commit: `{data['commit']}`\n"
            f"- request: {data['pr']}\n- autonomy mode: {data['mode']}\n"
            f"- channel: {data['channel']}\n- delivery token: `{token}`\n"
            f"- accountable: {data['policy_owner']}\n- delivered: {data['generated_at']}\n"
            f"- payload digest: `{digest}`\n\n"
            f"This records that a notice was delivered BEFORE the merge. It is an audit artifact, "
            f"not a permission: the gate performs a fresh delivery in the privileged run and never "
            f"accepts a receipt found in the working tree as proof.\n")
        return p
    except OSError:
        return None


def required_for(mode):
    """A notice is required by hotl and by nothing else. hootl states that nobody is told, and
    hitl has a human in the decision already."""
    return mode == "hotl"


def send(cfg, *, change, commit, pr=None, root=".", mode=None, dry_run=False):
    """(exit_code, report). The single entry point the merge gate uses."""
    n = notify_cfg(cfg)
    un = _dict(_dict(cfg, "merge"), "unattended")
    owner = str(un.get("policy_owner") or "").strip()
    mode = mode or str(_dict(cfg, "autonomy").get("mode") or "").strip().lower()
    rep = {"mode": mode, "channel": n["channel"] or None, "required": required_for(mode)}

    if not required_for(mode):
        rep["detail"] = f"autonomy mode '{mode}' requires no per-change notice"
        return 3, rep
    if not usable(n["channel"]):
        rep["detail"] = "autonomy.notify.channel is not set, so there is nowhere to send a notice"
        return 1, rep
    if not usable(n["command"]):
        rep["detail"] = ("autonomy.notify.command is not set, so nothing can deliver the notice. "
                         "A channel with no delivery mechanism is a label, not a notification.")
        return 1, rep

    data = payload(change=change, commit=commit, pr=pr, mode=mode, channel=n["channel"],
                   policy_owner=owner, repo=os.path.basename(os.path.abspath(root)))
    if dry_run:
        rep["detail"] = "dry run: nothing was sent"
        rep["payload"] = data
        return 3, rep
    ok, token, why = deliver(n["command"], data, n["timeout"])
    rep["token"] = token or None
    if not ok:
        rep["detail"] = why
        return 1, rep
    rep["receipt"] = write_receipt(root, n["receipts"], data, token)
    rep["detail"] = f"delivered to {n['channel']}"
    if rep["receipt"] is None:
        # The delivery happened; only the audit file failed. Say so rather than failing the merge
        # on a filesystem problem, because the notice itself — the thing hotl promises — did go.
        rep["detail"] += " (the receipt could not be written; delivery itself succeeded)"
    return 0, rep


def receipts_of(root, receipts, change=None):
    base = os.path.join(root, receipts)
    out = []
    if not os.path.isdir(base):
        return out
    for d, _subdirs, files in os.walk(base):
        if change and os.path.basename(d) != re.sub(r"[^A-Za-z0-9._-]+", "-", change):
            continue
        for f in sorted(files):
            if f.endswith(".md"):
                out.append(os.path.join(d, f))
    return sorted(out, reverse=True)


def load_cfg(path=None):
    """(policy, trusted) through govcfg, the ONE reader. Untrusted is EMPTY, never the working
    tree — the working tree is the branch under review."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load(override=path)
    if not L.trusted:
        print(f"[notify] governance is UNTRUSTED — {L.why}", file=sys.stderr)
    return L.data, L.trusted


def main():
    ap = argparse.ArgumentParser(description="deliver the human-on-the-loop notice, and prove it")
    ap.add_argument("command", nargs="?", default="send", choices=["send", "check", "list"])
    ap.add_argument("--change")
    ap.add_argument("--commit")
    ap.add_argument("--pr")
    ap.add_argument("--config")
    ap.add_argument("--root", default=".")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    try:
        cfg, trusted = load_cfg(a.config)
    except Exception as e:                                  # noqa: BLE001
        print(f"[notify] the configuration could not be read: {e}", file=sys.stderr)
        return 1
    if not trusted:
        print("[notify] refused: the configuration was not read from the trust ref, so the "
              "notifier it names is the one the branch under review supplies.", file=sys.stderr)
        return 1

    if a.command == "list":
        n = notify_cfg(cfg)
        for p in receipts_of(a.root, n["receipts"], a.change):
            print(p)
        return 0

    if a.command == "check":
        rc, rep = send(cfg, change="NOTICE-TEST", commit="0" * 40, pr=None, root=a.root,
                       mode="hotl")
        print(json.dumps(rep, indent=2) if a.json else
              (f"[notify] test notice delivered: {rep.get('token')}" if rc == 0
               else f"[notify] NOT delivered: {rep.get('detail')}"))
        return 0 if rc == 0 else 1

    if not a.change or not a.commit:
        print("[notify] --change and --commit are required", file=sys.stderr)
        return 2
    rc, rep = send(cfg, change=a.change, commit=a.commit, pr=a.pr, root=a.root,
                   dry_run=a.dry_run)
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    elif rc == 0:
        print(f"[notify] delivered to {rep['channel']} · token {rep['token']}")
    elif rc == 3:
        print(f"[notify] {rep['detail']}")
    else:
        print(f"[notify] NOT DELIVERED: {rep['detail']}", file=sys.stderr)
    return rc


# ---------------------------------------------------------------------------- self-test

def _fake(tmp, name, body, mode=0o755):
    p = os.path.join(tmp, name)
    io.open(p, "w", encoding="utf-8").write(body)
    os.chmod(p, mode)
    return p


def _cfg(cmd, *, mode="hotl", channel="slack:#gov", timeout=5, receipts=None):
    return {
        "autonomy": {"mode": mode,
                     "notify": {"channel": channel, "command": cmd,
                                "timeout_seconds": timeout,
                                "receipts": receipts or DEFAULT_RECEIPTS}},
        "merge": {"auto_merge": "unattended",
                  "unattended": {"policy_owner": "Ada Lovelace <ada@acme.io>"}},
    }


def self_test():
    import tempfile
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL  {name}  {detail}")

    tmp = tempfile.mkdtemp()
    py = sys.executable

    good = _fake(tmp, "good.py", "#!/usr/bin/env python3\nimport sys,json\n"
                 "d=json.load(sys.stdin)\nprint('msg-'+d['change'])\n")
    silent = _fake(tmp, "silent.py", "#!/usr/bin/env python3\n")
    angry = _fake(tmp, "angry.py", "#!/usr/bin/env python3\nimport sys\n"
                  "sys.stderr.write('slack said no\\n')\nsys.exit(4)\n")
    slow = _fake(tmp, "slow.py", "#!/usr/bin/env python3\nimport time\ntime.sleep(6)\nprint('x')\n")
    noexec = _fake(tmp, "noexec.py", "#!/usr/bin/env python3\nprint('x')\n", mode=0o644)
    echoer = _fake(tmp, "echo.py", "#!/usr/bin/env python3\nimport sys,json\n"
                   "print(json.dumps(json.load(sys.stdin))[:300])\n")

    # -- the happy path
    r = os.path.join(tmp, "r1")
    rc, rep = send(_cfg(f"{py} {good}", receipts="rec"), change="CHG-1", commit="a" * 40, root=r)
    check("a notifier that prints a token delivers", rc == 0, str(rep))
    check("the token is recorded", rep.get("token") == "msg-CHG-1", str(rep.get("token")))
    check("a receipt is written", rep.get("receipt") and os.path.exists(rep["receipt"]),
          str(rep.get("receipt")))
    if rep.get("receipt"):
        txt = io.open(rep["receipt"], encoding="utf-8").read()
        for want in ("CHG-1", "msg-CHG-1", CONTRACT, "aaaaaaaaaaaa"):
            check(f"the receipt records {want}", want in txt)
        check("the receipt says it is not a permission", "not a permission" in txt)
    check("one receipt per notice", len(receipts_of(r, "rec", "CHG-1")) == 1)

    # -- every way delivery fails. Each must be code 1, and none may be read as success.
    cases = [
        ("exit 0 with no token", f"{py} {silent}", "printed no delivery token"),
        ("a non-zero exit", f"{py} {angry}", "exited 4"),
        ("a timeout", f"{py} {slow}", "did not answer"),
        ("a command that does not exist", "/nonexistent/notifier", "does not exist"),
        ("a command that is not executable", noexec, "not executable"),
        ("an unparseable command line", 'sh -c "unbalanced', "could not be parsed"),
        ("an empty command", "   ", "not set"),
    ]
    for label, cmd, fragment in cases:
        rc, rep = send(_cfg(cmd, receipts="rec2"), change="CHG-2", commit="b" * 40, root=tmp)
        check(f"{label} does not deliver", rc == 1, f"rc={rc} {rep}")
        check(f"{label} says why", fragment in (rep.get("detail") or ""),
              repr(rep.get("detail"))[:160])
        check(f"{label} writes no receipt", not rep.get("receipt"))

    # -- configuration that cannot notify
    for label, cfg, fragment in [
        ("no channel", _cfg(f"{py} {good}", channel=""), "nowhere to send"),
        ("a placeholder channel", _cfg(f"{py} {good}", channel="slack:#REPLACE"), "nowhere"),
        ("no command", _cfg(""), "nothing can deliver"),
        ("a placeholder command", _cfg("REPLACE-with-your-notifier"), "nothing can deliver"),
    ]:
        rc, rep = send(cfg, change="CHG-3", commit="c" * 40, root=tmp)
        check(f"{label} does not deliver", rc == 1, f"rc={rc}")
        check(f"{label} says why", fragment in (rep.get("detail") or ""),
              repr(rep.get("detail"))[:160])

    # -- only hotl requires a notice
    for mode, want in (("hotl", False), ("hootl", True), ("hitl", True), ("", True), ("x", True)):
        rc, rep = send(_cfg(f"{py} {good}", mode=mode, receipts="rec3"), change="C", commit="d" * 40,
                       root=tmp)
        check(f"mode {mode!r} requires no notice" if want else f"mode {mode!r} requires one",
              (rc == 3) == want, f"rc={rc}")
    check("required_for is true for hotl alone",
          required_for("hotl") and not any(required_for(m) for m in ("hitl", "hootl", "", None)))

    # -- the payload: the request's text is data on stdin, never argv
    rc, rep = send(_cfg(f"{py} {echoer}", receipts="rec4"), change="CHG-4", commit="e" * 40,
                   pr="42", root=tmp)
    check("the notifier receives the payload on stdin", rc == 0 and "CHG-4" in (rep.get("token") or ""),
          str(rep)[:200])
    d = payload(change="x", commit="y", pr=1, mode="hotl", channel="c", policy_owner="o", repo="r")
    check("the payload names its contract", d["contract"] == CONTRACT)
    for k in ("change", "commit", "pr", "mode", "channel", "policy_owner", "subject", "body",
              "generated_at"):
        check(f"the payload carries {k}", k in d)
    check("the payload is JSON-serialisable", isinstance(json.dumps(d), str))

    # -- a hostile command string is not a shell
    evil = _cfg(f"{py} {good}; touch {tmp}/PWNED", receipts="rec5")
    rc, rep = send(evil, change="CHG-5", commit="f" * 40, root=tmp)
    check("a chained command is not run through a shell", not os.path.exists(f"{tmp}/PWNED"),
          "a shell metacharacter was honoured")

    # -- garbage configuration resolves to "cannot notify", never to success
    for junk in (None, {}, {"autonomy": None}, {"autonomy": {"notify": "nope"}},
                 {"autonomy": {"mode": "hotl", "notify": {"timeout_seconds": "soon"}}}):
        try:
            rc, rep = send(junk if isinstance(junk, dict) else {}, change="C", commit="0" * 40,
                           root=tmp, mode="hotl")
            check(f"garbage {junk!r} does not deliver", rc == 1, f"rc={rc}")
        except Exception as e:                              # noqa: BLE001
            check(f"garbage {junk!r} does not raise ({e})", False)
    n = notify_cfg({"autonomy": {"notify": {"timeout_seconds": "soon"}}})
    check("an unparseable timeout falls back to the default", n["timeout"] == DEFAULT_TIMEOUT)
    n = notify_cfg({"autonomy": {"notify": {"timeout_seconds": 99999}}})
    check("an absurd timeout is clamped", n["timeout"] == 300)

    # -- dry run sends nothing
    rc, rep = send(_cfg(f"{py} {good}", receipts="rec6"), change="C6", commit="1" * 40, root=tmp,
                   dry_run=True)
    check("a dry run sends nothing", rc == 3 and not rep.get("token") and not receipts_of(tmp, "rec6"))

    print(f"[notify] self-test: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
