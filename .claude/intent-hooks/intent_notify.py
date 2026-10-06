#!/usr/bin/env python3
"""Tell a person when the loop is waiting on them — on Slack or Teams — once, then remind, then escalate.

    intent_notify.py sweep [--now ISO] [--dry-run] [--json]   find every wait, send what is due
    intent_notify.py list [--change ID]                       what was sent, and its receipts
    intent_notify.py test --channel NAME                      send a test notice through a channel
    intent_notify.py --self-test

Exit: 0 nothing failed · 1 a notice could not be delivered, or the policy could not be trusted
      2 usage error

Why this exists
---------------
The loop driver knows when a change cannot move until a person acts — an intent nobody has
accepted, a NEEDS-USER finding, a sign-off, an escalation, a register update a bot could not push,
a change that stopped moving, an autonomy grant about to lapse — and it told nobody. Under hitl that
costs time; under an autonomous mode it silently stops the loop.

What it does
------------
A SWEEP walks every tracked change (and the repository's own grant), names each WAIT by reason, and
sends what is due through the routes in `loop_notices` (governance.yaml):

  * the first notice as soon as the wait begins (or in the day's digest, for a digest route);
  * a reminder every `remind_after_hours` while it lasts;
  * ONE escalation, to `escalate_to` (default: the policy owner), at `escalate_after_hours`;
  * nothing more once the wait ends — the evidence landing is what stops it.

EXACTLY ONCE, whatever runs it. Each notice has a key — change, reason, the state it is about, and
which reminder — and is CLAIMED by creating `refs/intent/notices/<change>/<key>` with a create-only
push before anything is sent. Two sweepers at the same moment: the remote accepts one claim and the
other skips. A claim records each channel's receipt token when delivered.

A FAILED DELIVERY BLOCKS NOTHING — the change is already waiting. It is retried on later sweeps,
channel by channel, up to `max_attempts`; then the notice is abandoned and ONE `delivery_failed`
notice goes to the escalation contact, so a dead channel is itself reported. (The human-on-the-loop
MERGE notice, notify.py, is the opposite: there a failed delivery blocks the merge.)

WHO IS TOLD COMES FROM TRUSTED SOURCES. The policy, the routes, the channels and the people map are
read from the trust ref only (govcfg); CODEOWNERS and accepted intents from the protected branch;
the escalation chain and the policy owner from policy. A branch can at most name the decider of
its own, not-yet-accepted intent — and if that names nobody useful, the wait still escalates to the
policy owner. Untrusted policy sends nothing, and says so.

Channels are notifier COMMANDS (`intent-notice/1`), run by the one shared runner (notice_io), so a
command is resolved against the trusted checkout and nothing from a branch reaches argv. Text a
branch wrote travels as data, and the Slack/Teams notifier escapes it.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import argparse, datetime as _dt, fnmatch, hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import intent_loop as L                                  # same skill; installed alongside
import stages as S

CONTRACT = "intent-notice/1"
NOTICE_NS = "refs/intent/notices"
PENDING = "docs/intent/governance/pending"
RESOLVED = "docs/intent/governance/resolved"
EX_OK, EX_FAIL, EX_USAGE = 0, 1, 2
RETRY_MINUTES = 15
STUCK_CLAIM_MINUTES = 10

# reason -> (remind_after_hours, escalate_after_hours, default recipients)
DEFAULTS = {
    "intent_acceptance": (24, 72, "decider"),
    "needs_user": (24, 72, "originator"),
    "human_review": (8, 24, "codeowners"),
    "escalation": (4, 12, "escalation_chain"),
    "assembly_pr": (8, 24, "policy_owner"),
    "stalled": (24, 72, "policy_owner"),
    "grant_expiring": (24, 0, "policy_owner"),
    "delivery_failed": (0, 0, "policy_owner"),
}
ACTIONS = {
    "intent_acceptance": "accept or reject the intent — merge its intent.md with `Status: accepted`",
    "needs_user": "answer the NEEDS-USER finding(s) in the fix ledger",
    "human_review": "review the request and sign off (or request changes)",
    "escalation": "decide the escalation: sign a release or a rejection code",
    "assembly_pr": "merge the register-assembly pull request",
    "stalled": "find out why the change stopped moving",
    "grant_expiring": "renew the autonomy grant (autonomy.review_by) or let it lapse to hitl",
    "delivery_failed": "fix the notification channel: notices about this change are not arriving",
}


# ---------------------------------------------------------------- time

def parse_t(s):
    try:
        t = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def iso(t):
    return t.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- policy

def _num(v, default):
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return float(default)


def _off(v):
    return v is False or str(v).strip().lower() in ("false", "no", "off", "0")


def _d(v):
    return v if isinstance(v, dict) else {}


def _names(v):
    """A channel list as written: a list, or one name as a string. Anything else -> None."""
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return [str(x) for x in v] if isinstance(v, list) else None


def _route(reason, r, ln, channels, problems):
    """One route, with the reason's defaults. Rule 5: a value that is not a route reads as ON, and
    is reported — a typo must not silently switch a notice off."""
    rem, esc, to = DEFAULTS[reason]
    if r is None or r is True or (isinstance(r, str) and not _off(r)):
        r = {}
    elif _off(r) and not isinstance(r, dict):
        r = {"enabled": False}
    elif not isinstance(r, dict):
        problems.append(f"routes.{reason}: {r!r} is not a route — read as on, with the defaults")
        r = {}
    wanted = _names(r.get("channels")) or _names(ln.get("default_channels")) or list(channels)
    unknown = [c for c in wanted if c not in channels]
    if unknown:
        problems.append(f"routes.{reason}: no channel named {', '.join(unknown)}")
    who = r.get("to")
    if who is not None and not isinstance(who, (str, list)):
        problems.append(f"routes.{reason}.to: {who!r} is not a person or a role — using {to}")
        who = None
    return {"enabled": not _off(r.get("enabled", True)), "to": who or to,
            "channels": [c for c in wanted if c in channels],
            "remind": _num(r.get("remind_after_hours"), rem),
            "escalate": _num(r.get("escalate_after_hours"), esc),
            "after": _num(r.get("after_hours"), 8), "days_before": _num(r.get("days_before"), 7),
            # An escalation is never held for a digest: it exists to be read now.
            "digest": str(r.get("mode") or "").lower() == "digest" and reason != "escalation"}


def config(gov, armed_only=True):
    """The loop_notices block, normalised. Absent or switched off -> None (nothing is sent).
    `armed_only=False` is for `test` alone: a channel is proved BEFORE the loop is switched on to
    rely on it, the same order as the hotl merge notice. Anything malformed is collected in
    `problems` (the sweep reports them and exits 1) rather than raised or read as "off"."""
    ln = gov.get("loop_notices") if isinstance(gov, dict) else None
    if not isinstance(ln, dict) or (armed_only and _off(ln.get("enabled", True))):
        return None
    problems = []
    raw = ln.get("channels")
    if raw is not None and not isinstance(raw, dict):
        problems.append("loop_notices.channels is not a mapping of name -> {command}")
    channels = {}
    for k, v in _d(raw).items():
        cmd = str(_d(v).get("command") or "").strip()
        if cmd:
            channels[str(k)] = cmd
        else:
            problems.append(f"channels.{k}: no command")
    if not channels:
        problems.append("loop_notices has no usable channel — nothing can be sent")
    if ln.get("routes") is not None and not isinstance(ln.get("routes"), dict):
        problems.append("loop_notices.routes is not a mapping — every route read with its defaults")
    routes = {reason: _route(reason, _d(ln.get("routes")).get(reason), ln, channels, problems)
              for reason in DEFAULTS}
    return {"channels": channels, "routes": routes, "problems": problems,
            "timeout": int(_num(ln.get("timeout_seconds"), 20)) or 20,
            "max_attempts": int(_num(ln.get("max_attempts"), 4)) or 4,
            "escalate_to": ln.get("escalate_to") if isinstance(ln.get("escalate_to"), (str, list))
            and ln.get("escalate_to") else "policy_owner",
            "digest_hour": int(_num(ln.get("digest_hour_utc"), 8)) % 24,
            "link": str(ln.get("link_template") or "")}


# ---------------------------------------------------------------- recipients (trusted sources only)

def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def person(people, ref):
    """{name, slack, teams} for an id, a name, an email or a @github handle; the bare name otherwise."""
    if not ref:
        return None
    ref = str(ref).strip()
    email = (re.search(r"<([^>]+@[^>]+)>", ref) or re.search(r"(\S+@\S+)", ref) or [None, None])[1]
    bare = _norm(re.sub(r"\s*<[^>]*>", "", ref))
    asked = {k for k in (_norm(ref), bare, _norm(email)) if k}
    if not asked:
        return None                       # `<!channel>` and friends name nobody — never the first entry
    for pid, p in _d(people).items():
        if not isinstance(p, dict):
            continue
        gh = str(p.get("github") or "").lstrip("@")
        keys = {k for k in (_norm(pid), _norm(p.get("name")), _norm(p.get("email")),
                            _norm("@" + gh) if gh else "") if k}
        if asked & keys:
            return {"name": p.get("name") or pid, "slack": p.get("slack"), "teams": p.get("teams")}
    return {"name": re.sub(r"\s*<[^>]*>", "", ref).strip()[:80]} if bare else None


def intent_field(text, field):
    """`Author: <name, role>. Status: …` — the name, without the role (intent-change-record format)."""
    m = re.search(rf"(?im)\b{field}:\s*([^.\n|]+)", text or "")
    return m.group(1).split(",")[0].strip() if m else None


def codeowners(base, files):
    """Owners of `files` per CODEOWNERS on the PROTECTED branch (last matching rule wins)."""
    text = next((t for t in (L.show(base, p) for p in (".github/CODEOWNERS", "CODEOWNERS",
                                                        "docs/CODEOWNERS")) if t), "")
    rules = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2:
            rules.append((parts[0], parts[1:]))
    owners = []
    for f in files:
        hit = None
        for pat, who in rules:
            p = pat.lstrip("/")
            if fnmatch.fnmatch(f, p) or fnmatch.fnmatch(f, p.rstrip("/") + "/*") \
                    or (p.endswith("/") and f.startswith(p)) or (pat == "*"):
                hit = who
        for w in hit or []:
            if w not in owners:
                owners.append(w)
    return owners


def recipients(role, ctx, gov):
    """The people a role names, resolved through the trusted people map."""
    people = _d(gov.get("people"))
    roles = role if isinstance(role, list) else [role]
    out = []
    for r in roles:
        for ref in _role_refs(r, ctx, gov):
            p = person(people, ref)
            if p and p not in out:
                out.append(p)
    return out


def _role_refs(role, ctx, gov):
    if role in ("decider", "originator"):
        field = "Decider" if role == "decider" else "Author"
        return [intent_field(ctx.get("intent_text"), field) or intent_field(ctx.get("intent_text"), "Author")]
    if role == "codeowners":
        return codeowners(ctx.get("base"), ctx.get("files") or [])
    if role == "escalation_chain":
        chain = _d(gov.get("escalation")).get("chain")
        return [c.get("id") or c.get("name") for c in (chain if isinstance(chain, list) else [])[:1]
                if isinstance(c, dict)]
    if role == "policy_owner":
        return [_d(_d(gov.get("merge")).get("unattended")).get("policy_owner")
                or _d(gov.get("autonomy")).get("acknowledged_by")]
    return [role] if isinstance(role, str) else []       # an explicit person id


# ---------------------------------------------------------------- waits

def _since(evs):
    return parse_t(evs[-1].get("at")) if evs else None


def change_waits(change, now, cfg):
    """[wait dicts] for one tracked change: reason, anchor (the state it is about), since, detail."""
    evs = L.read_events(change)
    st = L.fold(change, evs)
    if not st["opened"]:
        return []
    head = L.head_of(st)
    stage, _ = L.current_stage(st, head)
    if stage == S.TERMINAL:
        return []
    base = L.protected_ref()
    last = evs[-1] if evs else {}
    ctx = {"change": change, "stage": stage, "head": head, "base": base, "pr": st.get("pr"),
           "intent_text": (L.show(base, f"docs/intent/changes/{change}/intent.md") if base else None)
           or L.show(head, f"docs/intent/changes/{change}/intent.md")}
    found = []
    for fn in (_w_intent, _w_needs_user, _w_review, _w_escalation, _w_assembly, _w_stalled):
        w = fn(st, evs, ctx, now, cfg)
        if w:
            found.append(dict(w, change=change, stage=stage, ctx=ctx,
                              since=w.get("since") or _since(evs) or now,
                              anchor=w.get("anchor") or last.get("_sha", "")[:12]))
    return found


def _w_intent(st, evs, ctx, now, cfg):
    return {"reason": "intent_acceptance", "detail": "the intent has not been accepted on the "
            "protected branch"} if ctx["stage"] == "intent" else None


def _w_needs_user(st, evs, ctx, now, cfg):
    if ctx["stage"] != "fix":
        return None
    rec, _ = L._ledger({"change": st["change"], "st": st, "head": ctx["head"]})
    ids = [i for i, _, status in (rec or {}).get("open_findings") or [] if "NEEDS-USER" in status]
    if not ids:
        return None
    return {"reason": "needs_user", "detail": "waiting on: " + ", ".join(ids[:8]),
            # The findings, not the head: a push that leaves them open is the same wait, and must
            # not restart its reminders or send a second escalation.
            "anchor": hashlib.sha256(",".join(sorted(ids)).encode()).hexdigest()[:12]}


def _w_review(st, evs, ctx, now, cfg):
    mg = st["done"].get("merge_gate")
    if ctx["stage"] != "merged" or not mg or "HUMAN-REVIEW" not in str(mg.get("why")):
        return None
    files = []
    if ctx["base"] and ctx["head"]:
        files = [f for f in L.out("diff", "--name-only", "--no-renames", f"{ctx['base']}...{ctx['head']}")
                 .splitlines() if f]
    ctx["files"] = files
    return {"reason": "human_review", "detail": f"merge gate decided HUMAN-REVIEW on {str(mg.get('head'))[:10]}",
            "since": parse_t(mg.get("at")), "anchor": str(mg.get("_sha", ""))[:12]}


def _w_escalation(st, evs, ctx, now, cfg):
    if not ctx["head"]:
        return None
    names = lambda d: [n.rsplit("/", 1)[-1] for n in L.out(  # noqa: E731
        "ls-tree", "--name-only", f"{ctx['head']}:{d}").splitlines() if n.endswith(".json")]
    resolved = {n.split("-", 1)[0] for n in names(RESOLVED)}
    open_ = sorted(n for n in names(PENDING) if n.split("-", 1)[0] not in resolved)
    if not open_:
        return None
    return {"reason": "escalation", "detail": f"{len(open_)} escalation(s) open: "
            + ", ".join(n.split("-", 1)[0] for n in open_[:5]),
            "anchor": hashlib.sha256(",".join(open_).encode()).hexdigest()[:12]}


def _w_assembly(st, evs, ctx, now, cfg):
    if st.get("assembled") or "merged" not in st["done"]:
        return None
    waiting = [e for e in evs if e.get("type") == "assembly-pr"]
    return {"reason": "assembly_pr", "detail": "branch protection refused the post-merge actor; the "
            "register update was opened as a pull request", "since": parse_t(waiting[-1].get("at")),
            "anchor": str(waiting[-1].get("_sha", ""))[:12]} if waiting else None


def _w_stalled(st, evs, ctx, now, cfg):
    s = S.BY_NAME.get(ctx["stage"])
    after = cfg["routes"]["stalled"]["after"]
    t = _since(evs)
    if not s or s.actor != "agent" or not t or (now - t).total_seconds() < after * 3600:
        return None
    return {"reason": "stalled", "detail": f"no progress at `{ctx['stage']}` for {int((now - t).total_seconds() // 3600)}h",
            "since": t + _dt.timedelta(hours=after)}


def repo_waits(gov, now, cfg):
    """Waits that belong to the repository rather than to a change: the autonomy grant."""
    aut = _d(gov.get("autonomy"))
    raw = str(aut.get("review_by") or "").strip()
    mode, rb = str(aut.get("mode") or ""), parse_t(raw if len(raw) > 10 else raw + "T00:00:00+00:00")
    days = cfg["routes"]["grant_expiring"]["days_before"]
    if mode not in ("hotl", "hootl") or not rb or now < rb - _dt.timedelta(days=days):
        return []
    return [{"reason": "grant_expiring", "change": "_repository", "stage": "-", "anchor": iso(rb)[:10],
             "since": rb - _dt.timedelta(days=days), "ctx": {"base": L.protected_ref()},
             "detail": f"autonomy.mode {mode} lapses to hitl on {iso(rb)[:10]}"}]


# ---------------------------------------------------------------- schedule

def due_slot(w, route, now):
    """(slot, escalation?) due now, or None. Slot 0 is the first notice; k>=1 the k-th reminder;
    'esc' the escalation. When sweeps were missed, only the LATEST due slot is sent — a backlog of
    reminders arriving together tells nobody anything more."""
    waited = (now - w["since"]).total_seconds() / 3600
    if waited < 0:
        return None
    if route["escalate"] and waited >= route["escalate"]:
        return "esc", True
    if route["remind"] and waited >= route["remind"]:
        return str(int(waited // route["remind"])), False
    return "0", False


# ---------------------------------------------------------------- the claim refs (exactly once)

def notice_ref(w, slot):
    return f"{NOTICE_NS}/{L.slug(w['change'])}/{w['reason']}-{w['anchor'] or 'x'}-{slot}"


FUTURE_SLACK = _dt.timedelta(minutes=5)


def _wall():
    return _dt.datetime.now(_dt.timezone.utc)


def read_claim(ref):
    """(doc, sha). A claim that is not the shape this program writes is {"state": "unreadable"}:
    the refs are writable by anyone who can push, so a claim is validated like any other input."""
    sha = L.out("rev-parse", "--verify", "--quiet", ref)
    if not sha:
        return None, None
    try:
        doc = json.loads(L.out("log", "-1", "--format=%B", sha).split("\n\n", 1)[1])
    except (IndexError, ValueError):
        return {"state": "unreadable"}, sha
    chans = doc.get("channels") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or not isinstance(doc.get("state"), str) or not isinstance(chans, dict) \
            or not all(isinstance(v, dict) for v in chans.values()):
        return {"state": "unreadable"}, sha
    for v in chans.values():
        v["attempts"] = int(_num(v.get("attempts"), 0))
    return doc, sha


def write_claim(ref, doc, old):
    """The new claim's sha when the ref moved from `old` ('' = must not exist), else None."""
    body = f"intent-loop-notice\n\n{json.dumps(doc, sort_keys=True)}\n"
    new = L.git("commit-tree", L.empty_tree(), inp=body).stdout.strip()
    return new if new and L.cas(ref, new, old or "") else None


def claim(ref, now, cfg):
    """(doc, sha) when this sweep owns the notice now. Otherwise (None, None) — another sweeper has
    it, or it is settled — or (None, why) for a claim that must be REPORTED: unreadable, or dated
    in the future (either would otherwise silence the notice for good).

    Two clocks, deliberately: `at` is the sweep's time (retries are scheduled in it, and tests move
    it), `claimed_at` the WALL clock at the moment of claiming, so a long sweep cannot write a claim
    that a concurrent sweep already takes for stuck."""
    doc, sha = read_claim(ref)
    wall = _wall()
    if doc is None:
        doc = {"state": "claimed", "at": iso(now), "claimed_at": iso(wall), "channels": {}}
        new = write_claim(ref, doc, "")
        return (doc, new) if new else (None, None)
    if doc["state"] == "unreadable":
        return None, "unreadable"
    at, cat = parse_t(doc.get("at")), parse_t(doc.get("claimed_at"))
    if (at and at > now + FUTURE_SLACK) or (cat and cat > wall + FUTURE_SLACK):
        return None, "future-dated"
    retry = doc["state"] == "failed" and (not at or (now - at).total_seconds() >= RETRY_MINUTES * 60)
    stuck = doc["state"] == "claimed" and (not cat or (wall - cat).total_seconds() >= STUCK_CLAIM_MINUTES * 60)
    if not (retry or stuck):
        return None, None
    doc = dict(doc, state="claimed", claimed_at=iso(wall))
    new = write_claim(ref, doc, sha)
    return (doc, new) if new else (None, None)


def pending_channels(doc, route, cfg):
    return [c for c in route["channels"] if not (doc["channels"].get(c) or {}).get("token")
            and (doc["channels"].get(c) or {}).get("attempts", 0) < cfg["max_attempts"]]


def settle(ref, doc, own, results, cfg, now):
    """Record what happened — delivered / failed (retry later) / abandoned (give up, report) — by
    compare-and-set against THIS sweep's own claim. If another sweeper took the claim over meanwhile
    the record is theirs to write: the result is `lost`, and nothing of theirs is overwritten."""
    for c, (token, why) in results.items():
        prev = doc["channels"].get(c) or {}
        doc["channels"][c] = ({"token": token} if token else
                              {"error": str(why)[:200], "attempts": prev.get("attempts", 0) + 1})
    done = [bool(v.get("token")) or v.get("attempts", 0) >= cfg["max_attempts"]
            for v in doc["channels"].values()]
    if all(v.get("token") for v in doc["channels"].values()):
        doc["state"] = "delivered"
    elif all(done):
        doc["state"] = "abandoned"
    else:
        doc["state"] = "failed"
    doc["at"] = iso(now)
    return doc["state"] if write_claim(ref, doc, own) else "lost"


# ---------------------------------------------------------------- the notice

def payload(w, slot, esc, gov, cfg):
    route = cfg["routes"][w["reason"]]
    to = cfg["escalate_to"] if esc else route["to"]
    link = _link(cfg["link"], w)
    return {"contract": CONTRACT, "kind": "waiting", "reason": w["reason"], "change": w["change"],
            "stage": w["stage"], "action": ACTIONS.get(w["reason"]), "detail": w.get("detail"),
            "waited_hours": round((w["now"] - w["since"]).total_seconds() / 3600, 1),
            "reminder": int(slot) if slot not in ("0", "esc") else 0, "escalation": esc,
            "recipients": recipients(to, w["ctx"], gov), "link": link, "generated_at": iso(w["now"])}


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _link(template, w):
    """`link_template` with {change} and {pr}. An unknown field is blank and a malformed template
    is no link — a typo in the template must not stop the notice it decorates."""
    try:
        return template.format_map(_Blank(change=w["change"], pr=w["ctx"].get("pr") or "")) if template else ""
    except (ValueError, IndexError, AttributeError, KeyError, TypeError):
        return ""


def send(cmd_by_channel, channels, data, timeout):
    import notice_io
    out = {}
    for c in channels:
        ok, token, why = notice_io.deliver(cmd_by_channel[c], dict(data, channel=c), timeout)
        out[c] = (token if ok else None, why)
    return out


# ---------------------------------------------------------------- sweep

FAILED_STATES = ("failed", "abandoned", "unreadable", "future-dated", "error")


def _waits(gov, cfg, now):
    """(waits, rows). One change that cannot be read is REPORTED and skipped — it must not stop
    the notices for every other change (refs/intent/* is writable by anyone who can push)."""
    waits, rows = [], []
    for c in L.tracked_changes():
        try:
            waits += change_waits(c, now, cfg)
        except Exception as e:                           # noqa: BLE001 — reported, exit 1
            rows.append(("-", "unreadable", c, "-", "unreadable"))
            print(f"[notify] change {c}: state unreadable ({type(e).__name__}: {e})", file=sys.stderr)
    try:
        waits += repo_waits(gov, now, cfg)
    except Exception as e:                               # noqa: BLE001
        rows.append(("-", "grant_expiring", "_repository", "-", "error"))
        print(f"[notify] the autonomy grant could not be read ({e})", file=sys.stderr)
    for w in waits:
        if w["since"] > now + FUTURE_SLACK:              # a forged or skewed event must not silence it
            rows.append(("-", w["reason"], w["change"], "-", "future-dated"))
            w["since"] = now
    return waits, rows


def sweep(gov, cfg, now, dry_run=False):
    """[(ref, reason, change, slot, state)] for every notice acted on in this sweep."""
    L.fetch(f"{NOTICE_NS}/*")
    waits, report = _waits(gov, cfg, now)
    digest = []
    for w in waits:
        try:
            report += _one(w, gov, cfg, now, dry_run, digest)
        except Exception as e:                           # noqa: BLE001 — reported, exit 1
            report.append(("-", w["reason"], w["change"], "-", "error"))
            print(f"[notify] {w['change']} {w['reason']}: {type(e).__name__}: {e}", file=sys.stderr)
    return report + _send_digests(digest, gov, cfg, now)


def _one(w, gov, cfg, now, dry_run, digest):
    w["now"] = now
    route = cfg["routes"].get(w["reason"])
    if not route or not route["enabled"] or not route["channels"]:
        return []
    due = due_slot(w, route, now)
    if not due:
        return []
    slot, esc = due
    ref = notice_ref(w, slot)
    held = route["digest"] and not esc
    if held and now.hour != cfg["digest_hour"] and read_claim(ref)[0] is None:
        return []                                        # a FIRST notice waits for the digest; a retry does not
    if dry_run:
        return [(ref, w["reason"], w["change"], slot, "due")]
    data = payload(w, slot, esc, gov, cfg)               # built BEFORE claiming: nothing strands a claim
    doc, sha = claim(ref, now, cfg)
    if doc is None:
        return [(ref, w["reason"], w["change"], slot, sha)] if sha else []
    if held and not doc["channels"]:
        digest.append((w, ref, sha, doc, route, data))
        return []
    state = settle(ref, doc, sha, send(cfg["channels"], pending_channels(doc, route, cfg), data,
                                        cfg["timeout"]), cfg, now)
    rows = [(ref, w["reason"], w["change"], slot, state)]
    return rows + (_report_dead_channel(w, gov, cfg, now) if state == "abandoned" else [])


def _send_digests(entries, gov, cfg, now):
    """One message per CHANNEL listing every notice still pending on it — so a channel that already
    delivered a notice is never sent it again, and each notice is settled channel by channel."""
    by_chan, results = {}, {e[1]: {} for e in entries}
    for e in entries:
        for c in pending_channels(e[3], e[4], cfg):
            by_chan.setdefault(c, []).append(e)
    for c, es in by_chan.items():
        who = []
        for e in es:
            who += [r for r in e[5]["recipients"] if r not in who]
        head = dict(es[0][5], reason="digest", change=f"{len(es)} waiting", recipients=who, detail=None,
                    stage="-", reminder=0, escalation=False,
                    items=[{"change": e[5]["change"], "reason": e[5]["reason"], "action": e[5]["action"]} for e in es])
        (token, why), = send(cfg["channels"], [c], head, cfg["timeout"]).values()
        for e in es:
            results[e[1]][c] = (token, why)
    out = []
    for w, ref, sha, doc, route, data in entries:
        state = settle(ref, doc, sha, results[ref], cfg, now)
        out.append((ref, "digest", data["change"], "-", state))
        out += _report_dead_channel(w, gov, cfg, now) if state == "abandoned" else []
    return out


def _report_dead_channel(w, gov, cfg, now):
    """Notices about a change were abandoned: tell the escalation contact, ONCE, on every channel
    that still works — a channel that cannot deliver must not be the only one asked to say so."""
    dw = dict(w, reason="delivery_failed", anchor=hashlib.sha256(w["anchor"].encode()).hexdigest()[:12])
    ref = notice_ref(dw, "0")
    data = payload(dw, "0", True, gov, cfg)
    doc, sha = claim(ref, now, cfg)
    if doc is None:
        return []
    res = send(cfg["channels"], list(cfg["channels"]), data, cfg["timeout"])
    return [(ref, "delivery_failed", w["change"], "0", settle(ref, doc, sha, res, dict(cfg, max_attempts=1), now))]


# ---------------------------------------------------------------- CLI

def load_policy():
    import govcfg
    G = govcfg.load(trusted=True)
    if not G.trusted:
        print(f"[notify] governance is UNTRUSTED — {G.why}. Nothing is sent: a notifier named by an "
              f"untrusted policy is a program a branch chose.", file=sys.stderr)
        return None
    return G.data


def cmd_sweep(a):
    gov = load_policy()
    if gov is None:
        return EX_FAIL
    cfg = config(gov)
    if cfg is None:
        print("[notify] loop_notices is not configured — nothing to send")
        return EX_OK
    for p in cfg["problems"]:
        print(f"[notify] CONFIG: {p}", file=sys.stderr)
    now = parse_t(a.now) if a.now else _dt.datetime.now(_dt.timezone.utc)
    rows = sweep(gov, cfg, now, dry_run=a.dry_run)
    if a.json:
        print(json.dumps([dict(zip(("ref", "reason", "change", "slot", "state"), r)) for r in rows], indent=1))
    for ref, reason, change, slot, state in ([] if a.json else rows):
        print(f"  {state:<10} {reason:<18} {change:<32} slot {slot}")
    failed = [r for r in rows if r[4] in FAILED_STATES]
    if failed:
        print(f"[notify] {len(failed)} notice(s) not delivered or not readable — see above; "
              f"failed deliveries are retried on later sweeps", file=sys.stderr)
    return EX_FAIL if failed or cfg["problems"] else EX_OK


def cmd_list(a):
    L.fetch(f"{NOTICE_NS}/*")
    prefix = f"{NOTICE_NS}/{L.slug(a.change)}" if a.change else NOTICE_NS
    for ref in L.out("for-each-ref", "--format=%(refname)", prefix).splitlines():
        doc, _ = read_claim(ref)
        tokens = ", ".join(f"{c}={v.get('token') or v.get('error')}" for c, v in (doc or {}).get("channels", {}).items())
        print(f"  {(doc or {}).get('state', '?'):<10} {ref[len(NOTICE_NS) + 1:]}  {tokens}")
    return EX_OK


def cmd_test(a):
    gov = load_policy()
    cfg = config(gov, armed_only=False) if gov is not None else None
    if not cfg or a.channel not in cfg["channels"]:
        print(f"[notify] no channel {a.channel!r} in loop_notices.channels", file=sys.stderr)
        return EX_FAIL
    now = _dt.datetime.now(_dt.timezone.utc)
    data = {"contract": CONTRACT, "kind": "waiting", "reason": "stalled", "change": "test-notice",
            "stage": "-", "action": "nothing — this is a test of the channel", "detail": "intent_notify.py test",
            "waited_hours": 0, "recipients": [], "generated_at": iso(now)}
    (token, why), = send(cfg["channels"], [a.channel], data, cfg["timeout"]).values()
    print(f"[notify] delivered: {token}" if token else f"[notify] NOT delivered: {why}")
    return EX_OK if token else EX_FAIL


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("sweep")
    s.add_argument("--now", help="evaluate as of this ISO time (tests)")
    s.add_argument("--dry-run", action="store_true", help="say what is due; claim and send nothing")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("--change")
    s = sub.add_parser("test")
    s.add_argument("--channel", required=True)
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.cmd:
        ap.print_help()
        return EX_USAGE
    return {"sweep": cmd_sweep, "list": cmd_list, "test": cmd_test}[a.cmd](a)


# ---------------------------------------------------------------- self-test (pure parts)

def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        if not cond:
            print(f"  FAIL {label}")

    gov = {"loop_notices": {"channels": {"slack": {"command": "x"}, "teams": {"command": "y"}},
                            "routes": {"needs_user": {"mode": "digest"}, "stalled": {"enabled": False},
                                       "escalation": {"mode": "digest", "channels": ["teams"]}}},
           "people": {"ada": {"name": "Ada Lovelace", "slack": "U0ADA", "teams": "ada@x.io",
                              "email": "ada@x.io", "github": "ada-l"}},
           "merge": {"unattended": {"policy_owner": "Ada Lovelace <ada@x.io>"}},
           "escalation": {"chain": [{"id": "ada", "name": "Ada"}]}}
    cfg = config(gov)
    check("absent block sends nothing", config({}) is None)
    check("switched off sends nothing", config({"loop_notices": {"enabled": "off"}}) is None)
    check("routes default to every channel", cfg["routes"]["human_review"]["channels"] == ["slack", "teams"])
    check("a route can name its channels", cfg["routes"]["escalation"]["channels"] == ["teams"])
    check("a route can be switched off", cfg["routes"]["stalled"]["enabled"] is False)
    check("an escalation is never held for a digest", cfg["routes"]["escalation"]["digest"] is False)
    check("a digest route is held", cfg["routes"]["needs_user"]["digest"] is True)
    t0 = _dt.datetime(2026, 10, 1, 9, tzinfo=_dt.timezone.utc)
    w = {"since": t0}
    r = cfg["routes"]["human_review"]                    # remind 8h, escalate 24h
    at = lambda h: due_slot(w, r, t0 + _dt.timedelta(hours=h))  # noqa: E731
    check("first notice at once", at(0) == ("0", False))
    check("first reminder after 8h", at(8.5) == ("1", False))
    check("a missed sweep sends the latest reminder only", at(17) == ("2", False))
    check("escalation at 24h, once", at(30) == ("esc", True) and at(90) == ("esc", True))
    check("a wait that has not begun is not due", at(-1) is None)
    p = person(gov["people"], "Ada Lovelace <ada@x.io>")
    check("a policy owner resolves through the people map", p == {"name": "Ada Lovelace", "slack": "U0ADA", "teams": "ada@x.io"})
    check("a @github handle resolves", person(gov["people"], "@ada-l")["slack"] == "U0ADA")
    check("an unknown name stays a plain name", person(gov["people"], "Zed <z@q.io>") == {"name": "Zed"})
    check("the escalation chain resolves", recipients("escalation_chain", {}, gov)[0]["teams"] == "ada@x.io")
    check("a decider comes from the intent", recipients("decider", {"intent_text": "Author: Ada Lovelace, PO. Status: draft"},
                                                         gov)[0]["slack"] == "U0ADA")
    check("ACTIONS cover every reason", set(ACTIONS) == set(DEFAULTS))
    check("a bracket-only name names nobody, not the first entry without an email",
          person({"bob": {"name": "Bob", "slack": "U0BOB"}}, "<!channel>") is None)
    odd = config({"loop_notices": {"channels": {"slack": {"command": "x"}},
                                   "routes": {"escalation": True, "needs_user": {"channels": "slack"},
                                              "human_review": {"channels": ["slak"]}, "stalled": 7}}})
    check("`escalation: on` is ON (rule 5)", odd["routes"]["escalation"]["enabled"] is True)
    check("a channel written as a string is a channel", odd["routes"]["needs_user"]["channels"] == ["slack"])
    check("a channel typo and a non-route are reported, not silent",
          any("slak" in p for p in odd["problems"]) and any("routes.stalled" in p for p in odd["problems"])
          and odd["routes"]["stalled"]["enabled"] is True)
    bad = config({"loop_notices": {"channels": ["slack"], "routes": ["x"]}, "merge": {"unattended": "x"},
                  "escalation": {"chain": {"a": 1}}})
    check("malformed blocks are problems, not exceptions", len(bad["problems"]) >= 2
          and recipients(["escalation_chain", "policy_owner"], {}, {"merge": {"unattended": "x"},
                                                                    "escalation": {"chain": {"a": 1}}}) == [])
    check("a link template with an unknown field is no exception",
          _link("https://x/{pr_number}/{change}", {"change": "c-1", "ctx": {}}) == "https://x//c-1"
          and _link("https://x/{0}", {"change": "c", "ctx": {}}) == "")
    print(f"[notify-loop] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
