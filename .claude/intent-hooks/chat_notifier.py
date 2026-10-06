#!/usr/bin/env python3
"""Slack and Microsoft Teams notifier for the intent loop — an implementation of `intent-notice/1`.

    chat_notifier.py --service slack --url-env INTENT_SLACK_WEBHOOK          Slack incoming webhook
    chat_notifier.py --service slack --token-env INTENT_SLACK_BOT_TOKEN \\
                     --channel C0123ABCD                                     Slack bot (chat.postMessage)
    chat_notifier.py --service teams --url-env INTENT_TEAMS_WEBHOOK          Teams Workflows webhook
    chat_notifier.py --self-test

It reads ONE notice as JSON on stdin and prints ONE delivery token on stdout (exit 0), or prints
why not on stderr (exit 1). It carries two kinds of notice:

  * the human-on-the-loop merge notice (`kind` absent or "merge") — what `notify.py` sends before an
    unattended merge, as `webhook_notifier.py` always has;
  * the WAITING notice (`kind: "waiting"`) — what `intent_notify.py` sends when a change cannot move
    until a person acts: the reason, the action that unblocks it, who is asked (with real
    @-mentions when the people map gives a Slack user id or a Teams UPN), how long it has waited,
    and whether this is a reminder or an escalation. A digest carries several in `items`.

SECRETS COME FROM THE ENVIRONMENT, NEVER FROM GOVERNANCE. A webhook URL or a bot token is a bearer
credential. governance.yaml (committed, readable by everyone and every fork) names only the
VARIABLE to read; the value lives in the CI secret store.

TEAMS: Office 365 connectors (the old "Incoming Webhook") were retired from Teams in May 2026. The
replacement is a Workflows ("When a Teams webhook request is received") webhook, which accepts an
Adaptive Card in a `message` envelope — that is what this sends. Mentions use the card's
`msteams.entities`; whether a mention pings depends on the flow posting the card into the channel.

UNTRUSTED TEXT IS ESCAPED, NOT TRUSTED. A notice carries text a pull request wrote — a change id, a
finding, a title. In Slack, `<!channel>`, `<@U…>` and `<https://evil|looks-safe>` are markup, and
`&`, `<`, `>` must be escaped or a branch can page the whole workspace or disguise a link. In Teams,
`<at>…</at>` is a mention and `[text](url)` is a link. Only mentions this program builds itself,
from the trusted people map, survive; everything else is rendered as text.

DELIVERY IS JUDGED BY THE ANSWER. Slack's webhook answers 200 "ok"; its API answers JSON with
`ok: true` and the message `ts` (the best receipt there is — it identifies the message). A Teams
Workflows webhook answers 202 with no body: accepted for delivery, not proof of a read, and the token
says exactly that. Anything else is not delivered, and the caller is told.

Stdlib only.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, hashlib, argparse, urllib.request, urllib.error

CONTRACT = "intent-notice/1"
SLACK_API = "https://slack.com/api/chat.postMessage"
MAX_TEXT = 2800                     # under Slack's 3000-character block limit, with room to spare
LOOPBACK_ENV = "INTENT_NOTIFY_ALLOW_LOOPBACK"   # tests only: http:// to 127.0.0.1 / localhost

REASON_TITLES = {
    "intent_acceptance": "An intent is waiting to be accepted",
    "needs_user": "A review finding needs a person's answer",
    "human_review": "A change is waiting for human review and sign-off",
    "escalation": "An escalation is waiting for an approver",
    "assembly_pr": "A register update is waiting to be merged",
    "stalled": "A change has stopped moving",
    "grant_expiring": "The autonomy grant is about to lapse",
    "delivery_failed": "Notices about a change could not be delivered",
    "digest": "Changes waiting on a person — daily digest",
}


# ---------------------------------------------------------------- escaping untrusted text

def slack_escape(s):
    """Slack mrkdwn: escape the three control characters, so no `<!channel>`, `<@U…>` or
    `<url|label>` can be smuggled in, and cap the length."""
    s = str(s if s is not None else "")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s[:MAX_TEXT]


_TEAMS_MD = re.compile(r"([\\`*_\[\]()#+!|~])")


def teams_escape(s):
    """Adaptive Card TextBlock markdown: no `<at>` mention, no `[label](url)` link, no emphasis
    games. Angle brackets become look-alikes (an `<at>` tag cannot be escaped, only removed)."""
    s = str(s if s is not None else "")
    s = s.replace("<", "‹").replace(">", "›")
    return _TEAMS_MD.sub(r"\\\1", s)[:MAX_TEXT]


def _safe_url(u):
    """Only an https link from the notice is rendered as a link; anything else is dropped."""
    u = str(u or "").strip()
    return u if re.fullmatch(r"https://[^\s<>|\]\[()\"']{1,500}", u) else ""


# ---------------------------------------------------------------- the message, service-neutral

def _hours(h):
    try:
        h = float(h)
    except (TypeError, ValueError):
        return ""
    return f"{int(h // 24)}d {int(h % 24)}h" if h >= 24 else f"{int(h)}h"


def lines_for(d):
    """(title, [(label, value)], recipients, link, tone) — what to say, before any markup.
    Values here are UNTRUSTED and are escaped by the renderer, never before."""
    kind = d.get("kind") or "merge"
    if kind == "merge":
        title = d.get("subject") or "Unattended merge notice"
        rows = [("", d.get("body") or ""), ("change", d.get("change")),
                ("commit", str(d.get("commit") or "")[:12]),
                ("request", f"#{d['pr']}" if d.get("pr") else ""), ("mode", d.get("mode")),
                ("accountable", d.get("policy_owner"))]
        return title, [r for r in rows if r[1]], [], _safe_url(d.get("link")), "info"
    tone = "escalation" if d.get("escalation") else ("reminder" if d.get("reminder") else "new")
    title = REASON_TITLES.get(d.get("reason"), "A change is waiting on a person")
    if tone == "escalation":
        title = "ESCALATION — " + title
    elif tone == "reminder":
        title = f"Reminder {d.get('reminder')} — " + title
    rows = [("change", d.get("change")), ("stage", d.get("stage")),
            ("what unblocks it", d.get("action")), ("why", d.get("detail")),
            ("waiting", _hours(d.get("waited_hours")))]
    for it in d.get("items") or []:
        if isinstance(it, dict):
            rows.append(("•", f"{it.get('change')} — {REASON_TITLES.get(it.get('reason'), it.get('reason'))}"
                              f" — {it.get('action') or ''}"))
    return title, [r for r in rows if r[1]], d.get("recipients") or [], _safe_url(d.get("link")), tone


# ---------------------------------------------------------------- Slack

def slack_payload(d, channel=None):
    title, rows, recipients, link, tone = lines_for(d)
    mentions = [f"<@{r['slack']}>" for r in recipients
                if isinstance(r, dict) and re.fullmatch(r"[UW][A-Z0-9]{2,20}", str(r.get("slack") or ""))]
    named = [slack_escape(r.get("name")) for r in recipients
             if isinstance(r, dict) and r.get("name") and not r.get("slack")]
    icon = {"escalation": ":rotating_light:", "reminder": ":alarm_clock:",
            "new": ":hourglass_flowing_sand:", "info": ":robot_face:"}[tone]
    body = "\n".join(f"*{slack_escape(k)}:* {slack_escape(v)}" if k not in ("", "•")
                     else (f"• {slack_escape(v)}" if k == "•" else slack_escape(v)) for k, v in rows)
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
              {"type": "section", "text": {"type": "mrkdwn", "text": body[:MAX_TEXT] or " "}}]
    who = " ".join(mentions + named)
    if who:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{icon} {who}"[:MAX_TEXT]}})
    if link:
        blocks.append({"type": "actions", "elements": [{"type": "button", "url": link,
                       "text": {"type": "plain_text", "text": "Open"}}]})
    # The fallback `text` IS parsed as mrkdwn (notifications, clients without blocks), and a merge
    # notice's title carries the branch-chosen change id — so it is escaped like any other field.
    out = {"text": f"{slack_escape(title)} — {slack_escape(d.get('change') or '')}"[:MAX_TEXT], "blocks": blocks}
    if channel:
        out["channel"] = channel
    return out


# ---------------------------------------------------------------- Teams

def teams_payload(d):
    title, rows, recipients, link, tone = lines_for(d)
    body = [{"type": "TextBlock", "text": teams_escape(title), "weight": "Bolder", "size": "Medium",
             "wrap": True, "color": "Attention" if tone == "escalation" else "Default"}]
    facts = [{"title": teams_escape(k), "value": teams_escape(v)} for k, v in rows if k not in ("", "•")]
    for k, v in rows:
        if k in ("", "•"):
            body.append({"type": "TextBlock", "text": ("• " if k == "•" else "") + teams_escape(v), "wrap": True})
    if facts:
        body.append({"type": "FactSet", "facts": facts})
    entities, tags = [], []
    for r in recipients:
        if not isinstance(r, dict):
            continue
        upn, name = str(r.get("teams") or ""), teams_escape(r.get("name") or r.get("teams") or "")
        if re.fullmatch(r"[^@\s<>]{1,64}@[^@\s<>]{1,190}", upn):
            tag = f"<at>{name}</at>"                     # built HERE, from the trusted people map
            tags.append(tag)
            entities.append({"type": "mention", "text": tag, "mentioned": {"id": upn, "name": name}})
        elif name:
            tags.append(name)
    if tags:
        body.append({"type": "TextBlock", "text": "Asked: " + ", ".join(tags), "wrap": True})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body}
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open", "url": link}]
    if entities:
        card["msteams"] = {"entities": entities}
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


# ---------------------------------------------------------------- delivery

def _allowed(url):
    u = url.strip().lower()
    if u.startswith("https://"):
        return True
    return bool(os.environ.get(LOOPBACK_ENV)) and re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?/", u)


def post(url, data, timeout, token=None):
    """(status, body) or (None, reason). Never raises."""
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "intent-loop-notifier/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), (r.read(4096) or b"").decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {((e.read(300) or b'').decode('utf-8', 'replace').strip() or e.reason)[:200]}"
    except urllib.error.URLError as e:
        return None, f"could not reach the service: {e.reason}"
    except (OSError, ValueError) as e:
        return None, f"the request failed: {e}"


def judge(service, mode, status, body, sent):
    """(token, None) when delivered, else (None, why). The token says what is actually known."""
    digest = hashlib.sha256(json.dumps(sent, sort_keys=True).encode()).hexdigest()[:12]
    if status is None:
        return None, body
    if not 200 <= status < 300:
        return None, f"HTTP {status}: {body[:200]}"
    if service == "slack" and mode == "bot":
        try:
            got = json.loads(body or "{}")
        except ValueError:
            return None, f"Slack answered something that is not JSON: {body[:120]}"
        if not got.get("ok") or not got.get("ts"):
            return None, f"Slack refused the message: {got.get('error') or 'no ts in the answer'}"
        return f"slack:{got.get('channel')}:{got.get('ts')}", None
    if service == "slack":
        return (f"slack-webhook:ok:{digest}", None) if body == "ok" else \
            (None, f"Slack's webhook answered {body[:120]!r}, not ok")
    # Teams Workflows: 202 Accepted with an empty body is the documented success answer. A 2xx whose
    # body is a page (a proxy, a captive portal, a sign-in wall) is somebody else answering.
    if status not in (200, 202) or (body or "").lstrip().startswith("<"):
        return None, f"Teams answered HTTP {status} {body[:120]!r}, not a Workflows acceptance"
    return f"teams-workflow:{status}:{digest}", None


def deliver(a, d):
    if a.service == "slack" and a.token_env:
        token = (os.environ.get(a.token_env) or "").strip()
        if not token or not a.channel:
            return None, f"bot mode needs ${a.token_env} set and --channel"
        payload = slack_payload(d, channel=a.channel)
        status, body = post(a.api_url or SLACK_API, payload, a.timeout, token=token)
        return judge("slack", "bot", status, body, payload)
    url = (os.environ.get(a.url_env or "") or "").strip()
    if not url:
        return None, (f"no webhook URL: ${a.url_env} is unset or empty. Put it in the CI secret store "
                      f"under that name; never in governance.yaml")
    if not _allowed(url):
        return None, "the webhook URL must be https://"
    payload = slack_payload(d) if a.service == "slack" else teams_payload(d)
    status, body = post(url, payload, a.timeout)
    return judge(a.service, "webhook", status, body, payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--service", choices=["slack", "teams"])
    ap.add_argument("--url-env", help="NAME of the environment variable holding the webhook URL")
    ap.add_argument("--token-env", help="Slack bot mode: NAME of the variable holding the bot token")
    ap.add_argument("--channel", help="Slack bot mode: channel id (C…) or user id (U…) for a DM")
    ap.add_argument("--api-url", help=argparse.SUPPRESS)          # tests: a loopback Slack API
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.service:
        ap.error("--service is required")
    try:
        d = json.load(sys.stdin)
    except (ValueError, OSError) as e:
        print(f"the notice could not be read: {e}", file=sys.stderr)
        return 1
    if not isinstance(d, dict) or d.get("contract") != CONTRACT:
        print(f"unexpected contract: {d.get('contract') if isinstance(d, dict) else type(d).__name__}",
              file=sys.stderr)
        return 1
    if a.api_url and not _allowed(a.api_url):
        print("--api-url must be https:// (or loopback in tests)", file=sys.stderr)
        return 1
    token, why = deliver(a, d)
    if token is None:
        print(why, file=sys.stderr)
        return 1
    print(token)
    return 0


# ---------------------------------------------------------------- self-test

def self_test():
    import threading, subprocess
    from http.server import BaseHTTPRequestHandler, HTTPServer
    ok = fail = 0
    seen = []

    def check(name, cond, detail=""):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
        if not cond:
            print(f"  FAIL  {name}  {detail}")

    class H(BaseHTTPRequestHandler):
        def do_POST(self):                                   # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n) or b"{}")))
            answers = {"/slack": (200, b"ok"), "/slack-bad": (200, b"invalid_payload"),
                       "/teams": (202, b""), "/deny": (403, b"no"),
                       "/api": (200, b'{"ok":true,"channel":"C1","ts":"1700000000.000100"}'),
                       "/api-err": (200, b'{"ok":false,"error":"channel_not_found"}')}
            code, out = answers.get(self.path, (404, b"?"))
            self.send_response(code); self.end_headers(); self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    me = os.path.abspath(__file__)
    hostile = "fix <!channel> <@U999> <https://evil.example|safe.example> & [x](https://evil) <at>CEO</at>"
    wait = {"contract": CONTRACT, "kind": "waiting", "reason": "needs_user", "change": "c-1",
            "stage": "fix", "action": "answer F-3", "detail": hostile, "waited_hours": 30,
            "recipients": [{"name": "Ada", "slack": "U0ADA1", "teams": "ada@fpt.example"},
                           {"name": "Bob <b>"}], "link": "https://git.example/pr/1"}

    def run(args, payload, env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("INTENT_")}
        e.update(env)
        return subprocess.run([sys.executable, me] + args, input=json.dumps(payload),
                              capture_output=True, text=True, env=e)

    # --- escaping: nothing a branch wrote becomes markup
    s = json.dumps(slack_payload(wait))
    check("slack: no <!channel> survives", "<!channel>" not in s and "&lt;!channel&gt;" in s)
    check("slack: no smuggled user mention", "<@U999>" not in s)
    check("slack: no disguised link", "<https://evil" not in s)
    check("slack: the trusted mention IS built", "<@U0ADA1>" in s)
    check("slack: a name without an id is plain text", "Bob &lt;b&gt;" in s)
    t = json.dumps(teams_payload(wait))
    check("teams: no smuggled <at> mention", "<at>CEO</at>" not in t)
    check("teams: no markdown link", "[x](https" not in t)
    check("teams: the trusted mention IS built", '"id": "ada@fpt.example"' in t and "<at>Ada</at>" in t)
    check("teams: an adaptive card in a message envelope",
          '"application/vnd.microsoft.card.adaptive"' in t and '"type": "message"' in t)
    check("an http link from the notice is not rendered",
          "http://" not in json.dumps(slack_payload(dict(wait, link="http://evil.example"))))
    check("an escalation says so", "ESCALATION" in json.dumps(slack_payload(dict(wait, escalation=True))))
    check("a reminder is numbered", "Reminder 2" in json.dumps(teams_payload(dict(wait, reminder=2))))
    check("a merge notice still renders", "CHG-9" in json.dumps(slack_payload(
        {"contract": CONTRACT, "change": "CHG-9", "commit": "f" * 40, "subject": "s", "body": "b"})))
    m = slack_payload({"contract": CONTRACT, "change": "<!channel>", "commit": "f" * 40,
                       "subject": "[hotl] <!channel> is about to merge", "body": "b"})
    check("slack: the fallback text escapes a branch-chosen title", "<!channel>" not in m["text"])
    check("teams: a 2xx page from somebody else is NOT delivered",
          judge("teams", "webhook", 200, "<html>sign in</html>", {})[0] is None
          and judge("teams", "webhook", 202, "", {})[0])
    check("slack bot: ok without a ts is NOT delivered", judge("slack", "bot", 200, '{"ok":true}', {})[0] is None)
    dig = dict(wait, items=[{"change": "c-2", "reason": "intent_acceptance", "action": "accept"}])
    check("a digest lists its items", "c-2" in json.dumps(slack_payload(dig)))

    # --- delivery judged by the answer, end to end through the CLI
    lb = {LOOPBACK_ENV: "1"}
    r = run(["--service", "slack", "--url-env", "W"], wait, dict(lb, W=base + "/slack"))
    check("slack webhook: ok is delivered", r.returncode == 0 and r.stdout.startswith("slack-webhook:ok:"), r.stderr)
    r = run(["--service", "slack", "--url-env", "W"], wait, dict(lb, W=base + "/slack-bad"))
    check("slack webhook: a 200 that is not ok is NOT delivered", r.returncode == 1, r.stdout)
    r = run(["--service", "teams", "--url-env", "W"], wait, dict(lb, W=base + "/teams"))
    check("teams: 202 accepted is delivered, and says accepted", r.returncode == 0
          and r.stdout.startswith("teams-workflow:202:"), r.stderr)
    r = run(["--service", "teams", "--url-env", "W"], wait, dict(lb, W=base + "/deny"))
    check("a 403 is not delivered", r.returncode == 1 and "403" in r.stderr)
    r = run(["--service", "slack", "--token-env", "T", "--channel", "C1", "--api-url", base + "/api"],
            wait, dict(lb, T="xoxb-test"))
    check("slack bot: the message ts is the receipt", r.stdout.strip() == "slack:C1:1700000000.000100", r.stderr)
    check("slack bot: the token goes in the header, not the body",
          seen and seen[-1][1] == "Bearer xoxb-test" and "xoxb" not in json.dumps(seen[-1][2]))
    r = run(["--service", "slack", "--token-env", "T", "--channel", "C1", "--api-url", base + "/api-err"],
            wait, dict(lb, T="xoxb-test"))
    check("slack bot: ok:false is NOT delivered, with the reason", r.returncode == 1
          and "channel_not_found" in r.stderr)
    r = run(["--service", "slack", "--url-env", "W"], wait, {"W": base + "/slack"})
    check("plain http is refused outside the loopback test switch", r.returncode == 1 and "https" in r.stderr)
    r = run(["--service", "slack", "--url-env", "UNSET_X"], wait, {})
    check("no URL is refused, naming the variable", r.returncode == 1 and "UNSET_X" in r.stderr)
    r = run(["--service", "slack", "--url-env", "W"], {"contract": "x/1"}, dict(lb, W=base + "/slack"))
    check("an unknown contract is refused and prints no token", r.returncode == 1 and not r.stdout.strip())
    srv.shutdown()
    print(f"[chat-notifier] self-test: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
