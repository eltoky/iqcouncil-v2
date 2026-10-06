#!/usr/bin/env python3
"""Resolve the repository's autonomy mode: who is in, on, or out of the loop.

    autonomy.py resolve [--config PATH] [--json] [--today YYYY-MM-DD]
    autonomy.py --self-test

Three named modes, because "auto_merge: unattended" answers how a merge happens and says nothing
about where the human went. The mode is the declaration a team can actually discuss, review and
revoke:

  hitl   HUMAN IN THE LOOP.  A human approves each change before it merges. The loop prepares the
         decision; it does not take it. merge.auto_merge must be `off` or `after_signoff`.

  hotl   HUMAN ON THE LOOP.  The loop decides and merges eligible changes; a named human is told
         each time and can intervene, and the classes in model_routing.human_in_loop_for still
         stop and wait. Review is by exception, after the fact. Requires merge.auto_merge:
         unattended, a policy owner, an expiry date, and a NOTIFICATION CHANNEL.

  hootl  HUMAN OUT OF THE LOOP.  As hotl, but nobody is told per change; the human reads the
         ledger on a cadence instead. Requires everything hotl requires except the channel, plus
         an explicit acknowledgement by name that no human sees the happy path.

THE RULES THAT MAKE THIS WORTH HAVING, each one fail-closed:

  1. An unknown, missing or misspelled mode resolves to hitl. There is no "default to autonomous".
  2. The mode and merge.auto_merge must agree. They are two statements about the same thing, and
     two sources of truth for "may this merge without a human" is the exact shape of every bypass
     this suite has had. A disagreement resolves to hitl and is reported — never to the more
     permissive of the two, and never silently.
  3. THE GRANT EXPIRES. autonomy.review_by is mandatory for hotl and hootl, and a past date
     resolves to hitl. An autonomy grant with no end date is how a two-week experiment becomes
     permanent policy that nobody remembers agreeing to.
  4. HOTL WITHOUT A NOTIFICATION CHANNEL IS HOOTL WITH A COMFORTING LABEL. The only thing that
     distinguishes being on the loop from being out of it is that somebody is actually told. With
     no channel configured, the claim is false, so the mode resolves to hitl rather than quietly
     operating as hootl.
  5. No mode waives human_in_loop_for. hootl does not mean "no human ever": those classes become
     ineligible for autonomous merge and queue for a person. The mode widens what may proceed
     without a human; it never widens what may bypass one.

This module decides nothing on its own — it reports. The caller loads the configuration FROM THE
TRUST REF (the pinned ref, never the working tree) and refuses what this says is not permitted;
`resolve()` is pure so it can be tested exhaustively.

Exit: 0 resolved (read the result), 2 usage error.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, argparse, subprocess
from datetime import date, datetime

GOV = "docs/intent/governance.yaml"
MODES = ("hitl", "hotl", "hootl")
OWNER_RE = re.compile(r"^[^<>@]{2,}\s<[^<>@\s]+@[^<>@\s]+>$")
# Deliberately NOT `<[^>]*>$`: a valid identity is `Name <email>`, so that pattern rejected every
# correct value and only the self-test noticed. Match the template's words, not its punctuation.
PLACEHOLDER_RE = re.compile(r"example\.(?:com|org|net)|\bREPLACE\b|\bTODO\b|\bCHANGEME\b|"
                            r"\byour[-_ ]?(?:name|email|team)\b", re.I)

# Which merge policies each mode is consistent with. A mode is a claim about the human's position;
# auto_merge is the mechanism. These two tables are the same statement, so they must match.
PERMITTED_MERGE = {
    "hitl":  ("off", "after_signoff"),
    "hotl":  ("unattended",),
    "hootl": ("unattended",),
}
# Modes under which the loop may merge with no per-change human approval.
AUTONOMOUS = ("hotl", "hootl")

# The classes that stop for a human in EVERY mode. Mirrors model_routing.human_in_loop_for; this
# list is the fallback used when that key is absent, so the fence exists before it is configured.
DEFAULT_HUMAN_CLASSES = [
    "governance_change", "security_findings", "neg_claims", "arch_contract_changes",
    "irreversible_decision", "escalation_handling", "register_writes",
]


def _merge_mode(v):
    """merge.auto_merge as a string, surviving YAML 1.1's booleans.

    `auto_merge: off` — which is what every solo repository derives, and the commonest first
    install there is — parses as the boolean False, not the string "off". Unfixed, that reported a
    MODE-CONTRADICTION against a correctly derived configuration, and a refusal that makes no
    sense is what drives people to reach for a bypass. The generator now quotes the value; this
    handles the files already written, and the ones written by hand.

    Only `off` collides. `True` is not a legal value and stays unrecognised, which is correct.
    """
    if v is False:
        return "off"
    return str(v or "").strip()


def _dict(node, key):
    """A mapping at node[key], whatever is actually there. YAML hands back whatever was written,
    so `notify: nope` or `merge: nonsense` arrives as a string and every `.get` after it raises.
    A malformed autonomy block must resolve to hitl, not crash the gate that reads it."""
    if not isinstance(node, dict):
        return {}
    got = node.get(key)
    return got if isinstance(got, dict) else {}


def _norm(v):
    """A mode as written becomes a mode as meant, or stays unrecognised. Hyphens, spaces, case and
    the common long spellings all land on the same three tokens; anything else does NOT get a
    charitable reading, because guessing at an autonomy setting is exactly the wrong instinct."""
    s = str(v or "").strip().lower().replace("_", "-").replace(" ", "-")
    s = re.sub(r"-+", "-", s)
    long = {
        "human-in-the-loop": "hitl", "human-in-loop": "hitl",
        "human-on-the-loop": "hotl", "human-on-loop": "hotl",
        "human-out-of-the-loop": "hootl", "human-out-of-loop": "hootl",
        "hitl": "hitl", "hotl": "hotl", "hootl": "hootl",
    }
    return long.get(s, "")


def _placeholder(s):
    """True when a value is present but is obviously still the template's."""
    return bool(s) and bool(PLACEHOLDER_RE.search(s))


def _expiry(review_by, today):
    """(state, detail) where state is 'ok' | 'unbounded' | 'unparseable' | 'expired'."""
    s = str(review_by or "").strip()
    if not s:
        return "unbounded", ""
    try:
        when = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return "unparseable", s
    if when < today:
        return "expired", s
    return "ok", s


def _owner(cfg):
    return str(_dict(_dict(cfg, "merge"), "unattended").get("policy_owner") or "").strip()


def _human_classes(cfg):
    """THE human-required list, from policy.view — not a third copy of it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import policy
    return policy.view(cfg).human_required


def _check_identity(declared, cfg, add):
    """Accountability: a mode that merges without a per-change human needs a named owner, and
    hootl needs a second name accepting that nobody watches the happy path."""
    aut = _dict(cfg, "autonomy")
    owner = _owner(cfg)
    if not owner or _placeholder(owner) or not OWNER_RE.match(owner):
        add("OWNER-ABSENT",
            f"{declared} requires merge.unattended.policy_owner as a real 'Name <email>'; "
            f"autonomous merging with nobody accountable is not a merge this suite makes")
    if declared == "hootl":
        ack = str(aut.get("acknowledged_by") or "").strip()
        if not ack or _placeholder(ack) or not OWNER_RE.match(ack):
            add("UNACKNOWLEDGED",
                "hootl requires autonomy.acknowledged_by as a real 'Name <email>': somebody has "
                "to have accepted, by name, that no human sees an eligible change")


def _check_notify(declared, cfg, add):
    """The single thing that separates ON the loop from OUT of it — and it takes two settings,
    because a channel nothing can deliver to is a label."""
    if declared != "hotl":
        return
    n = _dict(_dict(cfg, "autonomy"), "notify")
    ch = str(n.get("channel") or "").strip()
    cmd = str(n.get("command") or "").strip()
    if not ch or _placeholder(ch):
        add("NOTIFY-ABSENT",
            "hotl requires autonomy.notify.channel: being 'on the loop' means somebody is told "
            "each time the loop merges. With no channel this repository would run as hootl under "
            "a label that says a human is watching")
    if not cmd or _placeholder(cmd):
        add("NOTIFY-NO-COMMAND",
            "hotl requires autonomy.notify.command — the program that actually delivers the "
            "notice, and whose delivery token is the proof it was sent. A channel with nothing "
            "to deliver to it is a label; this check is what stops 'a human is watching' from "
            "being a claim nobody ever tested")


def _check_expiry(declared, cfg, today, add):
    state, detail = _expiry(_dict(cfg, "autonomy").get("review_by"), today)
    if state == "unbounded":
        add("MODE-UNBOUNDED",
            f"{declared} requires autonomy.review_by (YYYY-MM-DD): an autonomy grant with no end "
            f"date is how a short experiment becomes permanent policy nobody remembers agreeing to")
    elif state == "unparseable":
        add("MODE-UNBOUNDED", f"autonomy.review_by is not a YYYY-MM-DD date: {detail!r}")
    elif state == "expired":
        add("MODE-EXPIRED",
            f"the autonomy grant lapsed on {detail}. Re-agree it and move the date forward; "
            f"until then this repository is hitl")
    return state, detail


def _check_contradiction(declared, auto_merge, add):
    """(auto_merge) — the mechanism the MODE implies, with any contradiction reported.

    `autonomy.mode` is the single statement (policy.implied_merge). An unset `merge.auto_merge` is
    no longer an error: the mode already says how a change merges. Writing a mechanism the mode
    does not permit is two statements disagreeing, and that still resolves DOWN to hitl in both
    directions — a hitl repository whose auto_merge says unattended is exactly as broken as the
    reverse."""
    if not declared:
        return auto_merge
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import policy
    implied, why = policy.implied_merge(declared, auto_merge)
    if why:
        add("MODE-CONTRADICTION",
            f"{why}. Two sources of truth for whether a human is required is how a bypass hides, "
            f"so this resolves to hitl")
        return auto_merge
    return implied


def _result(cfg, aut, *, declared, raw, effective, auto_merge, review_by, expiry, findings):
    """Assemble the record. Separated from the decision so that the decision stays readable: the
    `or None` normalisation here is a dozen branches that say nothing about policy."""
    return {
        "declared": declared or None,
        "declared_raw": raw,
        "effective": effective,
        "degraded": bool(findings),
        "autonomous_merge_permitted": effective in AUTONOMOUS,
        "auto_merge": auto_merge or None,
        "review_by": review_by or None,
        "expiry": expiry,
        "notify_channel": str(_dict(aut, "notify").get("channel") or "").strip() or None,
        "notify_command": str(_dict(aut, "notify").get("command") or "").strip() or None,
        "policy_owner": _owner(cfg) or None,
        "acknowledged_by": str(aut.get("acknowledged_by") or "").strip() or None,
        "always_human_for": _human_classes(cfg),
        "findings": findings,
    }


def resolve(cfg, *, today=None):
    """Pure. Configuration in, a resolved decision out. Never raises on bad input: bad input is
    the case this exists for, and it resolves to hitl."""
    today = today or date.today()
    cfg = cfg if isinstance(cfg, dict) else {}
    aut = _dict(cfg, "autonomy")
    raw = aut.get("mode")
    declared = _norm(raw)
    auto_merge = _merge_mode(_dict(cfg, "merge").get("auto_merge"))

    findings = []
    add = lambda code, text: findings.append([code, text])          # noqa: E731

    if not declared:
        add("MODE-UNKNOWN",
            f"autonomy.mode is {raw!r}; expected one of hitl, hotl, hootl. An unrecognised "
            f"autonomy setting is never read charitably — this repository is hitl")

    expiry, review_by = "n/a", ""
    if declared in AUTONOMOUS:
        expiry, review_by = _check_expiry(declared, cfg, today, add)
        _check_identity(declared, cfg, add)
        _check_notify(declared, cfg, add)
    auto_merge = _check_contradiction(declared, auto_merge, add)

    # Any finding at all drops the repository to the most restrictive mode. There is no partial
    # credit: a mode whose preconditions are not met is not that mode.
    effective = "hitl" if findings else (declared or "hitl")
    return _result(cfg, aut, declared=declared, raw=raw, effective=effective,
                   auto_merge=auto_merge, review_by=review_by, expiry=expiry, findings=findings)


def describe(r):
    """The one-screen account a person can act on."""
    out = []
    d = r["declared"] or f"{r['declared_raw']!r} (unrecognised)"
    out.append(f"declared   {d}")
    out.append(f"effective  {r['effective']}" + ("   [DEGRADED]" if r["degraded"] else ""))
    out.append(f"merge      {r['auto_merge'] or 'unset'}")
    if r["review_by"]:
        out.append(f"review by  {r['review_by']}")
    if r["notify_channel"]:
        out.append(f"notify     {r['notify_channel']}")
    if r["policy_owner"]:
        out.append(f"owner      {r['policy_owner']}")
    out.append("autonomous merge   " + ("PERMITTED" if r["autonomous_merge_permitted"] else "NO"))
    out.append("a human decides, in every mode, for: " + ", ".join(r["always_human_for"]))
    if r["findings"]:
        out.append("")
        out.append("why it is not what it says:")
        for code, text in r["findings"]:
            out.append(f"  {code}")
            out.append(f"    {text}")
    return "\n".join(out)


def load_cfg(path=None):
    """(policy, trusted) through govcfg, the ONE reader. Untrusted is EMPTY, never the working
    tree — the working tree is the branch under review."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load(override=path)
    if not L.trusted:
        print(f"[autonomy] governance is UNTRUSTED — {L.why}", file=sys.stderr)
    return L.data, L.trusted


def main():
    ap = argparse.ArgumentParser(description="resolve the repository's autonomy mode")
    ap.add_argument("command", nargs="?", default="resolve", choices=["resolve"])
    ap.add_argument("--config", help="read this file instead of the trust ref")
    ap.add_argument("--today", help="YYYY-MM-DD, for testing the expiry")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    try:
        cfg, trusted = load_cfg(a.config)
    except Exception as e:                                  # noqa: BLE001
        print(f"[autonomy] the configuration could not be read: {e}", file=sys.stderr)
        cfg, trusted = {}, False
    today = None
    if a.today:
        try:
            today = datetime.strptime(a.today, "%Y-%m-%d").date()
        except ValueError:
            print(f"[autonomy] --today must be YYYY-MM-DD", file=sys.stderr)
            return 2
    r = resolve(cfg, today=today)
    r["config_trusted"] = trusted
    if not trusted and r["autonomous_merge_permitted"]:
        # Belt and braces: an untrusted read never licenses autonomy, whatever it contains.
        r["autonomous_merge_permitted"] = False
        r["effective"] = "hitl"
        r["degraded"] = True
        r["findings"].append(["CONFIG-UNTRUSTED",
                              "the autonomy mode was not read from the trust ref, so it grants "
                              "nothing; this repository is hitl until it is"])
    if a.json:
        print(json.dumps(r, indent=2, sort_keys=True))
    else:
        print(describe(r))
    return 0


# ---------------------------------------------------------------------------- self-test

def _cfg(mode="hotl", merge="unattended", **over):
    c = {
        "merge": {"auto_merge": merge,
                  "unattended": {"policy_owner": "Ada Lovelace <ada@acme.io>"}},
        "autonomy": {"mode": mode, "review_by": "2099-01-01",
                     "notify": {"channel": "slack:#eng-governance",
                                "command": "./notifiers/webhook_notifier.py --url-env HOOK"},
                     "acknowledged_by": "Grace Hopper <grace@acme.io>"},
        "model_routing": {"human_in_loop_for": ["governance_change", "security_findings"]},
    }
    for k, v in over.items():
        node, _, leaf = k.partition("__")
        if leaf:
            c.setdefault(node, {})
            if leaf == "notify_channel":
                c[node].setdefault("notify", {})["channel"] = v
            elif leaf == "notify_command":
                c[node].setdefault("notify", {})["command"] = v
            elif leaf == "policy_owner":
                c[node].setdefault("unattended", {})["policy_owner"] = v
            else:
                c[node][leaf] = v
        else:
            c[k] = v
    return c


def self_test():
    T = date(2026, 6, 1)
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL  {name}")

    def codes(cfg, today=T):
        return {c for c, _ in resolve(cfg, today=today)["findings"]}

    # -- the happy paths
    r = resolve(_cfg("hotl"), today=T)
    check("hotl clean resolves to hotl", r["effective"] == "hotl" and not r["findings"])
    check("hotl permits autonomous merge", r["autonomous_merge_permitted"])
    r = resolve(_cfg("hootl"), today=T)
    check("hootl clean resolves to hootl", r["effective"] == "hootl" and not r["findings"])
    r = resolve(_cfg("hitl", merge="after_signoff"), today=T)
    check("hitl + after_signoff is clean", r["effective"] == "hitl" and not r["findings"])
    check("hitl never permits autonomous merge", not r["autonomous_merge_permitted"])
    r = resolve(_cfg("hitl", merge="off"), today=T)
    check("hitl + off is clean", not r["findings"])

    # -- rule 1: an unknown mode is hitl
    for bad in (None, "", "auto", "yolo", "HOTL!", "semi-autonomous", 7, [], "hotl hotl"):
        r = resolve(_cfg(bad), today=T)
        check(f"unknown mode {bad!r} -> hitl", r["effective"] == "hitl"
              and "MODE-UNKNOWN" in {c for c, _ in r["findings"]})
        check(f"unknown mode {bad!r} grants nothing", not r["autonomous_merge_permitted"])
    # ...but the ordinary spellings are understood
    for good, want in (("HITL", "hitl"), ("  hotl  ", "hotl"), ("human-on-the-loop", "hotl"),
                       ("human_out_of_the_loop", "hootl"), ("Human In The Loop", "hitl"),
                       ("hootl", "hootl")):
        check(f"{good!r} reads as {want}", _norm(good) == want)

    # -- YAML 1.1 turns a bare `off` into False. A solo repository derives exactly that, so this
    # was a spurious contradiction on the commonest configuration in existence.
    c = _cfg("hitl", merge=False)
    check("auto_merge False reads as 'off'", not codes(c))
    check("auto_merge False resolves to hitl", resolve(c, today=T)["effective"] == "hitl")
    check("the reported mechanism says off", resolve(c, today=T)["auto_merge"] == "off")
    check("_merge_mode(False) is 'off'", _merge_mode(False) == "off")
    check("_merge_mode(True) is not charitably read",
          _merge_mode(True) not in PERMITTED_MERGE["hitl"])
    c = _cfg("hotl", merge=True)
    check("auto_merge True still contradicts hotl", "MODE-CONTRADICTION" in codes(c))

    # -- rule 2: contradiction resolves DOWN, in both directions
    r = resolve(_cfg("hitl", merge="unattended"), today=T)
    check("hitl + unattended contradicts", "MODE-CONTRADICTION" in {c for c, _ in r["findings"]})
    check("hitl + unattended grants nothing", not r["autonomous_merge_permitted"])
    r = resolve(_cfg("hootl", merge="after_signoff"), today=T)
    check("hootl + after_signoff contradicts", "MODE-CONTRADICTION" in {c for c, _ in r["findings"]})
    check("hootl + after_signoff -> hitl", r["effective"] == "hitl")
    # 2.0.0: the mode is the single statement, so an unset mechanism is IMPLIED, not contradicted.
    r = resolve(_cfg("hotl", merge=""), today=T)
    check("hotl + unset merge implies unattended",
          r["auto_merge"] == "unattended" and "MODE-CONTRADICTION" not in {c for c, _ in r["findings"]})
    r = resolve(_cfg("hitl", merge=""), today=T)
    check("hitl + unset merge implies after_signoff", r["auto_merge"] == "after_signoff")
    # ...and writing a mechanism the mode forbids is still two statements disagreeing.
    r = resolve(_cfg("hitl", merge="unattended"), today=T)
    check("hitl + unattended still contradicts -> hitl",
          "MODE-CONTRADICTION" in {c for c, _ in r["findings"]} and r["effective"] == "hitl")

    # -- rule 3: the grant expires
    c = _cfg("hotl"); c["autonomy"]["review_by"] = ""
    check("hotl with no review_by is unbounded", "MODE-UNBOUNDED" in codes(c))
    check("unbounded hotl -> hitl", resolve(c, today=T)["effective"] == "hitl")
    c = _cfg("hotl"); c["autonomy"]["review_by"] = "2026-05-31"
    check("review_by yesterday is expired", "MODE-EXPIRED" in codes(c))
    c = _cfg("hotl"); c["autonomy"]["review_by"] = "2026-06-01"
    check("review_by today is still valid", not codes(c))
    c = _cfg("hotl"); c["autonomy"]["review_by"] = "next tuesday"
    check("unparseable review_by is unbounded", "MODE-UNBOUNDED" in codes(c))
    c = _cfg("hotl"); c["autonomy"]["review_by"] = "2026-13-45"
    check("impossible date is unbounded", "MODE-UNBOUNDED" in codes(c))
    c = _cfg("hitl", merge="off"); c["autonomy"]["review_by"] = ""
    check("hitl needs no review_by", not codes(c))

    # -- rule 4: hotl needs somewhere to send it AND something to send it with
    for ch in ("", "   ", "slack:#REPLACE", "eng@example.com"):
        c = _cfg("hotl"); c["autonomy"]["notify"]["channel"] = ch
        check(f"hotl channel {ch!r} -> NOTIFY-ABSENT", "NOTIFY-ABSENT" in codes(c))
        check(f"hotl channel {ch!r} -> hitl", resolve(c, today=T)["effective"] == "hitl")
    for cmd in ("", "   ", "REPLACE-with-your-notifier", "./notify-TODO"):
        c = _cfg("hotl"); c["autonomy"]["notify"]["command"] = cmd
        check(f"hotl command {cmd!r} -> NOTIFY-NO-COMMAND", "NOTIFY-NO-COMMAND" in codes(c))
        check(f"hotl command {cmd!r} -> hitl", resolve(c, today=T)["effective"] == "hitl")
    c = _cfg("hotl")
    check("hotl with a channel AND a command is clean", not codes(c))
    check("the command is reported", resolve(c, today=T)["notify_command"] is not None)
    c = _cfg("hootl"); c["autonomy"]["notify"] = {"channel": "", "command": ""}
    check("hootl needs neither", not codes(c))

    # -- accountability
    for owner in ("", "nobody", "Ada <ada@example.com>", "<ada@acme.io>", "REPLACE"):
        c = _cfg("hotl"); c["merge"]["unattended"]["policy_owner"] = owner
        check(f"owner {owner!r} -> OWNER-ABSENT", "OWNER-ABSENT" in codes(c))
    c = _cfg("hootl"); c["autonomy"]["acknowledged_by"] = ""
    check("hootl needs acknowledged_by", "UNACKNOWLEDGED" in codes(c))
    c = _cfg("hotl"); c["autonomy"]["acknowledged_by"] = ""
    check("hotl needs no acknowledged_by", not codes(c))

    # -- rule 5: no mode waives the human classes
    for m in ("hitl", "hotl", "hootl"):
        r = resolve(_cfg(m, merge="unattended" if m != "hitl" else "off"), today=T)
        check(f"{m} still names the human classes",
              "governance_change" in r["always_human_for"])
    r = resolve({"autonomy": {"mode": "hootl"}}, today=T)
    check("human classes default when unconfigured",
          set(DEFAULT_HUMAN_CLASSES) <= set(r["always_human_for"]))

    # -- garbage in
    for junk in (None, {}, {"autonomy": None}, {"autonomy": {"mode": {"a": 1}}},
                 {"merge": "nonsense"}, {"autonomy": {"notify": "nope"}}):
        try:
            r = resolve(junk, today=T)
            check(f"garbage {junk!r} -> hitl", r["effective"] == "hitl"
                  and not r["autonomous_merge_permitted"])
        except Exception as e:                              # noqa: BLE001
            check(f"garbage {junk!r} does not raise ({e})", False)

    # -- describe() never raises and always states the verdict
    for m in ("hitl", "hotl", "hootl", "nonsense"):
        txt = describe(resolve(_cfg(m), today=T))
        check(f"describe({m}) states the merge verdict", "autonomous merge" in txt)

    print(f"[autonomy] self-test: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
