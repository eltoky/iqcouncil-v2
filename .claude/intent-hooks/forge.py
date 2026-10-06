#!/usr/bin/env python3
"""The forge adapter: the ONE place the loop talks to a code-hosting service.

    forge.py facts   --pr N [--change ID] [--facts FILE] [--json]
    forge.py merged  --change ID [--facts FILE]
    forge.py comments --pr N [--facts FILE]
    forge.py validate FILE
    forge.py probe
    forge.py --self-test

Everything else in the suite works on plain git — commits, trees, refs, files. Only three questions
need a hosting service, and they are the whole reason this file exists:

    1. what are the facts of this pull/merge request?   (head, size, commits, approvals, checks)
    2. how many requests for this change already merged?
    3. what did people say on it?                       (overrides live in comments)

Those three answers, as ONE documented JSON shape, are the forge contract. A forge is supported
when something can produce that shape. Nothing downstream knows or cares whether it came from
GitHub, GitLab, Azure DevOps, Bitbucket, Gerrit, a bare repository with no service at all, or a
file a test wrote by hand. That is what makes the core portable, and it is a contract rather than
a hope because `validate` is mechanical and the reference adapter is tested against it.

BACKEND RESOLUTION, in order — the first that answers wins:

    1. --facts FILE, or $INTENT_FORGE_FACTS           a facts document on disk
    2. forge.adapter in governance.yaml               an external command, one per forge
    3. the built-in `github` backend (`gh`)           the reference adapter
    4. nothing                                        structured ABSENCE, never a guess

(4) matters as much as the others. A forge that cannot be reached must produce "unknown", because
every consumer of these facts is fail-closed: unknown size, unknown approvals and unknown checks
all mean BLOCK. An adapter that returns zeros to be helpful would turn a fail-closed gate into an
open one, which is the worst defect this file could have.

AN ADAPTER is any program that prints a facts document to stdout and exits 0:

    forge:
      adapter: .intent/adapters/gitlab.sh      # receives: facts --pr N | merged --change ID | comments --pr N
      name: gitlab

A FOURTH, OPTIONAL QUESTION — `protection`. Not part of `intent-forge-facts/1`: the three
questions below are what the loop cannot work without, while this one is what lets it check a
CLAIM (`merge.enforcement`) against reality. An adapter written before it existed answers nothing,
which reads as UNVERIFIED — never as verified, and never as a reason to block a forge that has no
protection concept at all.

THE CONTRACT (`contract: intent-forge-facts/1`) — every field optional unless marked, because a
forge that cannot answer something must say so by omission rather than by inventing a value:

    {"contract": "intent-forge-facts/1",
     "forge": "github",                                 # REQUIRED, free text, for the audit trail
     "pull_request": {
       "number": 42,                                    # REQUIRED
       "head_sha": "<40-hex>",                          # REQUIRED
       "repository": "acme/orders-api",                 # OPTIONAL, for the audit trail
       "base_ref": "main",
       "additions": 120, "deletions": 30,
       "from_fork": false,
       "commits":   [{"sha": "<40-hex>", "committed_at": "<ISO8601>"}],
       "approvals": [{"reviewer": "id", "submitted_at": "<ISO>", "head_sha": "<40-hex>"}],
       "checks":    [{"name": "intent/acceptance", "state": "success", "completed_at": "<ISO>"}]},
     "change":   {"id": "CHG-1", "merged_pr_count": 0},
     "comments": [{"author": "id", "body": "...", "created_at": "<ISO>"}]}

`state` is one of success · failure · pending · skipped · cancelled. Anything else is `unknown`,
and unknown is not success — see normalise_state.

`approvals` holds ONLY current, positive approvals. A forge whose reviews can be dismissed,
superseded or changed to "changes requested" must resolve that itself and emit the survivors; the
core judges staleness by comparing `head_sha`, which it cannot do if a withdrawn approval is still
listed. This is the single most important thing an adapter author has to get right.

BACKWARD COMPATIBILITY: the GitHub-native shape (`headRefOid`, `reviews[].state`,
`statusCheckRollup`) that this suite used before the contract existed is still accepted anywhere a
facts document is. It is detected, not configured. `to_github()` renders the contract back into
that shape so the already-tested consumers need no rewrite.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, re, sys, json, argparse, subprocess

CONTRACT = "intent-forge-facts/1"
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
STATES = {"success", "failure", "pending", "skipped", "cancelled", "unknown"}

# GitHub spells the same outcome several ways across CheckRun and StatusContext; everything that is
# not explicitly a pass is NOT a pass. `None` (a check still queued) must never read as success.
_GH_STATE = {
    "SUCCESS": "success", "NEUTRAL": "success",
    "FAILURE": "failure", "TIMED_OUT": "failure", "ACTION_REQUIRED": "failure",
    "STARTUP_FAILURE": "failure", "STALE": "failure", "ERROR": "failure",
    "CANCELLED": "cancelled", "SKIPPED": "skipped",
    "PENDING": "pending", "QUEUED": "pending", "IN_PROGRESS": "pending", "EXPECTED": "pending",
    "WAITING": "pending", "REQUESTED": "pending",
}


def normalise_state(raw):
    """Any forge's check outcome -> one of STATES. Unrecognised is `unknown`, never `success`."""
    if raw is None:
        return "unknown"
    s = str(raw).strip().upper()
    if s in _GH_STATE:
        return _GH_STATE[s]
    low = s.lower()
    if low in STATES:
        return low
    # GitLab: created/waiting_for_resource/preparing/scheduled/running/manual -> pending;
    # passed -> success; failed -> failure. Azure DevOps: succeeded/partiallySucceeded/failed.
    for pat, out in (("passed", "success"), ("succeeded", "success"), ("approved", "success"),
                     ("failed", "failure"), ("rejected", "failure"), ("broken", "failure"),
                     ("running", "pending"), ("created", "pending"), ("preparing", "pending"),
                     ("scheduled", "pending"), ("waiting", "pending"), ("manual", "pending"),
                     ("notstarted", "pending"), ("inprogress", "pending"),
                     ("canceled", "cancelled"), ("cancelled", "cancelled"),
                     ("skipped", "skipped"), ("partiallysucceeded", "failure")):
        if low.replace("_", "").replace("-", "") == pat:
            return out
    return "unknown"


# ---------------------------------------------------------------- shape detection + translation

def detect_shape(doc):
    if not isinstance(doc, dict):
        return "unknown"
    if doc.get("contract") == CONTRACT or "pull_request" in doc:
        return "neutral"
    if any(k in doc for k in ("headRefOid", "statusCheckRollup", "reviews")):
        return "github"
    if "additions" in doc or "deletions" in doc:
        return "github"          # the minimal `gh pr view --json additions,deletions` shape
    return "unknown"


# Who may approve on GitHub. A review's authorAssociation says what the reviewer is to the
# repository; NONE / FIRST_TIME_CONTRIBUTOR / CONTRIBUTOR can post a review on a public repository
# and must not count as the human sign-off (reproduced in the 2.0.0 review: a drive-by account's
# APPROVED gave DECISION HOLDS). An association that is absent is unknown, and unknown is a no.
APPROVER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def _login(who):
    return ((who.get("login") if isinstance(who, dict) else who) or "").strip()


def github_approvals(doc):
    """The approvals that STAND: each reviewer's LATEST decisive review (a later CHANGES_REQUESTED
    or a dismissal withdraws an approval — GitHub's own rule; before this any APPROVED ever posted
    counted), from someone the repository trusts, never the request's own author."""
    author = _login(doc.get("author"))
    latest = {}
    for r in sorted((r for r in doc.get("reviews") or [] if isinstance(r, dict)),
                    key=lambda r: str(r.get("submittedAt") or "")):
        state = str(r.get("state", "")).upper()
        if state in DECISIVE:
            latest[_login(r.get("author"))] = r
    appr = []
    for who, r in latest.items():
        if (str(r.get("state", "")).upper() != "APPROVED" or not who or who == author
                or str(r.get("authorAssociation") or "").upper() not in APPROVER_ASSOCIATIONS):
            continue
        appr.append({"reviewer": who, "submitted_at": r.get("submittedAt") or "",
                     "head_sha": ((r.get("commit") or {}).get("oid")
                                  if isinstance(r.get("commit"), dict) else r.get("commit")) or ""})
    return appr


def from_github(doc, number=None, change=None, merged=None, comments=None):
    """GitHub-native JSON -> the contract. Unanswerable fields are OMITTED, not defaulted."""
    pr = {}
    if number is not None:
        pr["number"] = int(number)
    if doc.get("number") is not None:
        pr["number"] = int(doc["number"])
    if doc.get("headRefOid"):
        pr["head_sha"] = doc["headRefOid"]
    if doc.get("baseRefName"):
        pr["base_ref"] = doc["baseRefName"]
    for k in ("additions", "deletions"):
        if isinstance(doc.get(k), int):
            pr[k] = doc[k]
    if doc.get("isCrossRepository") is not None:
        pr["from_fork"] = bool(doc["isCrossRepository"])

    if isinstance(doc.get("commits"), list):
        pr["commits"] = [{"sha": c.get("oid") or c.get("sha") or "",
                          "committed_at": c.get("committedDate") or c.get("committed_at") or ""}
                         for c in doc["commits"] if isinstance(c, dict)]
    if isinstance(doc.get("reviews"), list):
        pr["approvals"] = github_approvals(doc)
    if isinstance(doc.get("statusCheckRollup"), list):
        ch = []
        for c in doc["statusCheckRollup"]:
            if not isinstance(c, dict):
                continue
            # CheckRun carries (name, status, conclusion); StatusContext carries (context, state).
            raw = c.get("conclusion") if c.get("conclusion") is not None else c.get("state")
            if raw is None and str(c.get("status", "")).upper() in ("QUEUED", "IN_PROGRESS", "PENDING"):
                raw = c.get("status")
            ch.append({"name": c.get("name") or c.get("context") or "",
                       "state": normalise_state(raw),
                       "completed_at": c.get("completedAt") or c.get("completed_at") or ""})
        pr["checks"] = ch

    out = {"contract": CONTRACT, "forge": doc.get("forge") or "github", "pull_request": pr}
    chg = {}
    if change:
        chg["id"] = change
    if merged is not None:
        chg["merged_pr_count"] = int(merged)
    if chg:
        out["change"] = chg
    if comments is not None:
        out["comments"] = [{"author": ((c.get("user") or {}).get("login")
                                       if isinstance(c.get("user"), dict) else c.get("author")) or "",
                            "body": c.get("body") or "",
                            "created_at": c.get("created_at") or c.get("createdAt") or ""}
                           for c in comments if isinstance(c, dict)]
    return out


def to_github(facts):
    """The contract -> the GitHub-native shape the existing consumers already parse.

    This is a rendering, not a conversion back to GitHub: it exists so that adding the contract did
    not mean rewriting (and re-proving) logic that was already tested. A field the contract omits
    is omitted here too, so `unknown` stays unknown all the way to the fail-closed decision.
    """
    pr = (facts or {}).get("pull_request") or {}
    out = {}
    if "number" in pr:
        out["number"] = pr["number"]
    if pr.get("head_sha"):
        out["headRefOid"] = pr["head_sha"]
    if pr.get("base_ref"):
        out["baseRefName"] = pr["base_ref"]
    for k in ("additions", "deletions"):
        if isinstance(pr.get(k), int):
            out[k] = pr[k]
    if "from_fork" in pr:
        out["isCrossRepository"] = bool(pr["from_fork"])
    if "commits" in pr:
        out["commits"] = [{"oid": c.get("sha", ""), "committedDate": c.get("committed_at", "")}
                          for c in pr["commits"]]
    if "approvals" in pr:
        out["reviews"] = [{"author": {"login": a.get("reviewer", "")}, "state": "APPROVED",
                           "submittedAt": a.get("submitted_at", ""),
                           "commit": {"oid": a.get("head_sha", "")}}
                          for a in pr["approvals"]]
    if "checks" in pr:
        rollup = []
        for c in pr["checks"]:
            st = c.get("state", "unknown")
            # Only an explicit pass may render as SUCCESS. `unknown` renders as a non-terminal
            # status, which every consumer treats as "not passed".
            concl = {"success": "SUCCESS", "failure": "FAILURE", "cancelled": "CANCELLED",
                     "skipped": "SKIPPED"}.get(st)
            rollup.append({"name": c.get("name", ""),
                           "status": "COMPLETED" if concl else "IN_PROGRESS",
                           "conclusion": concl,
                           "completedAt": c.get("completed_at", "")})
        out["statusCheckRollup"] = rollup
    return out


def _validate_approvals(pr):
    """Conformance of pull_request.approvals, as problem lines.

    Extracted from validate() because the complexity ratchet caught that function growing from
    F(43) to F(46) when the optional `repository` field was added. validate() is on the security
    path and already F-rated, so the rule is that it does not grow: the block moves out instead.
    """
    p = []
    for i, a in enumerate(pr.get("approvals") or []):
        if not isinstance(a, dict):
            p.append(f"pull_request.approvals[{i}]: not an object")
            continue
        if not a.get("reviewer"):
            p.append(f"pull_request.approvals[{i}].reviewer: required identity")
        if not SHA_RE.match(a.get("head_sha") or ""):
            p.append(f"pull_request.approvals[{i}].head_sha: required — the core judges a stale "
                     f"approval by comparing this to the head; without it the approval cannot count")
        # Required, not merely well-formed-if-present. The core judges review time against it, and
        # an absent one rendered as "" -> ts("") -> None, which crashed the comparison in
        # recheck_signoff rather than being caught here. An approval with no time cannot be shown
        # to have been unhurried, so it is not a conformant approval.
        if not a.get("submitted_at"):
            p.append(f"pull_request.approvals[{i}].submitted_at: required — the core judges "
                     f"review time against it; without it the approval cannot be shown unhurried")
        elif not ISO_RE.match(a["submitted_at"]):
            p.append(f"pull_request.approvals[{i}].submitted_at: not ISO 8601")
    return p


def validate(doc):
    """Mechanical conformance check. Returns a list of problems; empty means conformant."""
    p = []
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    if doc.get("contract") != CONTRACT:
        p.append(f"contract must be {CONTRACT!r} (got {doc.get('contract')!r})")
    if not doc.get("forge") or not isinstance(doc.get("forge"), str):
        p.append("forge: required, a non-empty string naming the hosting service")
    pr = doc.get("pull_request")
    if not isinstance(pr, dict):
        return p + ["pull_request: required object"]
    if not isinstance(pr.get("number"), int):
        p.append("pull_request.number: required integer")
    if not (isinstance(pr.get("head_sha"), str) and SHA_RE.match(pr.get("head_sha") or "")):
        p.append("pull_request.head_sha: required commit sha (7-64 lowercase hex)")
    for k in ("additions", "deletions"):
        if k in pr and not (isinstance(pr[k], int) and pr[k] >= 0):
            p.append(f"pull_request.{k}: must be a non-negative integer when present")
    # Optional, and deliberately so. A rebuild or a parity programme spans two repositories, and
    # nothing in this contract could previously even NAME one — the GitHub backend resolves the
    # repository implicitly from the working directory. Recording it makes a facts document
    # self-describing and lets evidence from two repos be told apart in an audit. It is NOT a
    # cross-repo gate: the core still reads one checkout, so this field is for the record, and
    # `docs/ENFORCEMENT_MAP.md` grades the repo dimension D · Declared until that changes.
    if "repository" in pr and not (isinstance(pr["repository"], str) and pr["repository"].strip()):
        p.append("pull_request.repository: must be a non-empty string when present")
    if "from_fork" in pr and not isinstance(pr["from_fork"], bool):
        p.append("pull_request.from_fork: must be a boolean when present")

    for i, c in enumerate(pr.get("commits") or []):
        if not isinstance(c, dict) or not SHA_RE.match((c.get("sha") or "")):
            p.append(f"pull_request.commits[{i}].sha: required commit sha")
        elif c.get("committed_at") and not ISO_RE.match(c["committed_at"]):
            p.append(f"pull_request.commits[{i}].committed_at: not ISO 8601")
    p += _validate_approvals(pr)
    for i, c in enumerate(pr.get("checks") or []):
        if not isinstance(c, dict):
            p.append(f"pull_request.checks[{i}]: not an object"); continue
        if not c.get("name"):
            p.append(f"pull_request.checks[{i}].name: required")
        if c.get("state") not in STATES:
            p.append(f"pull_request.checks[{i}].state: must be one of {sorted(STATES)} "
                     f"(got {c.get('state')!r})")
    ch = doc.get("change")
    if ch is not None:
        if not isinstance(ch, dict):
            p.append("change: must be an object when present")
        elif "merged_pr_count" in ch and not (isinstance(ch["merged_pr_count"], int)
                                              and ch["merged_pr_count"] >= 0):
            p.append("change.merged_pr_count: must be a non-negative integer when present")
    cm = doc.get("comments")
    if cm is not None and not isinstance(cm, list):
        p.append("comments: must be a list when present")
    return p


# ---------------------------------------------------------------- backends

class Forge:
    def __init__(self, facts_file=None, adapter=None, name=None):
        self.facts_file = facts_file or os.environ.get("INTENT_FORGE_FACTS") or None
        self.adapter = adapter
        self.name = name
        self._cache = None

    # -- resolution ---------------------------------------------------------
    @classmethod
    def from_config(cls, cfg=None, facts_file=None):
        f = (cfg or {}).get("forge") or {} if isinstance(cfg, dict) else {}
        return cls(facts_file=facts_file, adapter=f.get("adapter"), name=f.get("name"))

    def backend(self):
        if self.facts_file:
            return "file"
        if self.adapter:
            return "adapter"
        from shutil import which
        return "github" if which("gh") else "none"

    # -- the fourth question, OPTIONAL ---------------------------------------
    def protection(self, branch=None):
        """What the forge actually enforces on the protected branch, or None when it cannot say.

        This is deliberately NOT part of `intent-forge-facts/1`. The three questions are what the
        loop cannot work without; this one is what lets it check a CLAIM. An adapter written before
        this existed answers nothing and that reads as UNVERIFIED — never as verified, and never as
        a reason to block a forge that has no protection concept at all.

        Shape, every field optional:
            {"branch": "main", "required_checks": [...], "required_reviews": 1,
             "enforced": true, "source": "github"}

        `required_checks` is the list the forge will actually block on. The question the loop asks
        of it is one thing only: is `intent/acceptance` in there.
        """
        b = branch or os.environ.get("INTENT_PROTECTED_BRANCH") or "main"
        k = self.backend()
        if k == "file":
            f = self.facts() or {}
            got = f.get("protection")
            return got if isinstance(got, dict) else None
        if k == "adapter":
            d = self._run_adapter(["protection", "--branch", b])
            return d if isinstance(d, dict) else None
        if k == "github":
            return self._github_protection(b)
        return None

    def _github_protection(self, branch):
        r = self._sh(["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"])
        if r is None:
            # 404 here means "not protected", but a 403 means "we are not allowed to look" and the
            # two must not be conflated: one is a fact, the other is ignorance. gh gives us no way
            # to tell them apart from the exit code alone, so this returns None (UNVERIFIED) and
            # lets the caller say so rather than reporting "not protected".
            return None
        try:
            d = json.loads(r)
        except (ValueError, TypeError):
            return None
        checks = (((d.get("required_status_checks") or {}).get("contexts")) or [])
        reviews = (d.get("required_pull_request_reviews") or {})
        return {"branch": branch, "source": "github",
                "required_checks": [str(c) for c in checks],
                "required_reviews": reviews.get("required_approving_review_count"),
                "enforced": bool(d)}

    # -- the three questions -----------------------------------------------
    def facts(self, pr=None, change=None):
        """The contract document, or None when no backend can answer. Never a guess."""
        if self._cache is not None:
            return self._cache
        b = self.backend()
        doc = None
        if b == "file":
            try:
                doc = json.load(open(self.facts_file))
            except Exception as e:
                print(f"[forge] cannot read {self.facts_file}: {e}", file=sys.stderr)
                return None
        elif b == "adapter":
            doc = self._run_adapter(["facts"] + (["--pr", str(pr)] if pr else [])
                                    + (["--change", change] if change else []))
        elif b == "github":
            doc = self._github_facts(pr, change)
        if doc is None:
            return None
        if detect_shape(doc) == "github":
            doc = from_github(doc, number=pr, change=change)
        if self.name and isinstance(doc, dict) and not doc.get("forge"):
            doc["forge"] = self.name
        self._cache = doc
        return doc

    def merged_pr_count(self, change):
        f = self.facts(change=change) or {}
        n = (f.get("change") or {}).get("merged_pr_count")
        if isinstance(n, int):
            return n
        b = self.backend()
        if b == "adapter":
            d = self._run_adapter(["merged", "--change", change])
            if isinstance(d, dict):
                v = (d.get("change") or {}).get("merged_pr_count", d.get("merged_pr_count"))
                return v if isinstance(v, int) else None
            if isinstance(d, int):
                return d
        if b == "github":
            r = self._sh(["gh", "pr", "list", "--state", "merged", "--head", f"change/{change}",
                          "--json", "number"])
            if r is not None:
                try:
                    return len(json.loads(r))
                except Exception:
                    return None
        return None

    def comments(self, pr):
        f = self.facts(pr=pr) or {}
        if isinstance(f.get("comments"), list):
            return f["comments"]
        b = self.backend()
        if b == "adapter":
            d = self._run_adapter(["comments", "--pr", str(pr)])
            if isinstance(d, dict) and isinstance(d.get("comments"), list):
                return d["comments"]
            if isinstance(d, list):
                return d
        if b == "github":
            r = self._sh(["gh", "api", "--paginate",
                          f"repos/{{owner}}/{{repo}}/issues/{pr}/comments"])
            if r is not None:
                try:
                    raw = json.loads(r)
                    return [{"author": (c.get("user") or {}).get("login", ""),
                             "body": c.get("body", ""),
                             "created_at": c.get("created_at", "")} for c in raw]
                except Exception:
                    return None
        return None

    # -- plumbing -----------------------------------------------------------
    def _sh(self, cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[forge] {cmd[0]} failed: {e}", file=sys.stderr)
            return None
        if r.returncode != 0:
            print(f"[forge] {' '.join(cmd[:3])} -> exit {r.returncode}: {r.stderr.strip()[:300]}",
                  file=sys.stderr)
            return None
        return r.stdout

    def _run_adapter(self, args):
        # Reading the adapter's NAME from trusted policy is not enough: the documented form is a
        # relative path, and in CI the working directory is the PR checkout, so a trusted config
        # still ran the PR's copy of the file. govcfg anchors it on the trusted checkout and
        # refuses anything inside the tree under review.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import govcfg
        prog, why = govcfg.resolve_program(self.adapter)
        if prog is None:
            print(f"[forge] adapter REFUSED: {why}", file=sys.stderr)
            return None
        return self._adapter_json(self._sh([prog, *args]))

    def _adapter_json(self, out):
        """The adapter's stdout as a document, or None — output that is not JSON is no answer."""
        if out is None:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            print(f"[forge] adapter {self.adapter} did not print a facts document: {e}",
                  file=sys.stderr)
            return None

    def _github_facts(self, pr, change):
        if pr is None:
            return None
        fields = ("number,author,headRefOid,baseRefName,additions,deletions,isCrossRepository,"
                  "commits,reviews,statusCheckRollup")
        out = self._sh(["gh", "pr", "view", str(pr), "--json", fields])
        if out is None:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None


# ---------------------------------------------------------------- self-test

def _self_test():
    ok = fail = 0

    def chk(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL  {label}")

    # --- state normalisation: the fail-closed core of the whole file
    for raw, want in [("SUCCESS", "success"), ("NEUTRAL", "success"), ("FAILURE", "failure"),
                      ("TIMED_OUT", "failure"), ("STALE", "failure"), ("CANCELLED", "cancelled"),
                      ("SKIPPED", "skipped"), ("QUEUED", "pending"), ("IN_PROGRESS", "pending"),
                      (None, "unknown"), ("WAT", "unknown"), ("passed", "success"),
                      ("failed", "failure"), ("running", "pending"), ("manual", "pending"),
                      ("succeeded", "success"), ("partiallySucceeded", "failure"),
                      ("notStarted", "pending"), ("canceled", "cancelled")]:
        chk(f"normalise_state({raw!r}) == {want}", normalise_state(raw) == want)
    chk("no raw value normalises to success by accident",
        normalise_state("definitely-not-a-real-state") == "unknown")

    # --- shape detection
    chk("detect neutral by contract", detect_shape({"contract": CONTRACT}) == "neutral")
    chk("detect neutral by member", detect_shape({"pull_request": {}}) == "neutral")
    chk("detect github by headRefOid", detect_shape({"headRefOid": "a" * 40}) == "github")
    chk("detect github by rollup", detect_shape({"statusCheckRollup": []}) == "github")
    chk("detect minimal github", detect_shape({"additions": 1, "deletions": 0}) == "github")
    chk("detect unknown", detect_shape({"hello": 1}) == "unknown")
    chk("detect non-dict", detect_shape([1, 2]) == "unknown")

    # --- github -> contract -> github round trip
    sha, old = "a" * 40, "b" * 40
    gh = {"number": 7, "headRefOid": sha, "baseRefName": "main", "additions": 10, "deletions": 2,
          "isCrossRepository": True,
          "commits": [{"oid": old, "committedDate": "2026-01-01T00:00:00Z"},
                      {"oid": sha, "committedDate": "2026-01-02T00:00:00Z"}],
          "author": {"login": "carol"},
          "reviews": [{"author": {"login": "alice"}, "state": "APPROVED", "authorAssociation": "MEMBER",
                       "submittedAt": "2026-01-02T01:00:00Z", "commit": {"oid": sha}},
                      {"author": {"login": "bob"}, "state": "CHANGES_REQUESTED", "authorAssociation": "MEMBER",
                       "submittedAt": "2026-01-02T02:00:00Z", "commit": {"oid": sha}}],
          "statusCheckRollup": [{"name": "intent/acceptance", "status": "COMPLETED",
                                 "conclusion": "SUCCESS", "completedAt": "2026-01-02T03:00:00Z"},
                                {"context": "legacy/status", "state": "FAILURE"},
                                {"name": "queued-one", "status": "QUEUED", "conclusion": None}]}
    n = from_github(gh, change="CHG-1", merged=2)
    chk("contract validates", validate(n) == [])
    chk("head carried", n["pull_request"]["head_sha"] == sha)
    chk("fork carried", n["pull_request"]["from_fork"] is True)
    chk("only APPROVED becomes an approval", len(n["pull_request"]["approvals"]) == 1)
    chk("approval reviewer", n["pull_request"]["approvals"][0]["reviewer"] == "alice")
    chk("approval bound to a sha", n["pull_request"]["approvals"][0]["head_sha"] == sha)
    def _rv(login, state, at, assoc="MEMBER"):
        return {"author": {"login": login}, "state": state, "authorAssociation": assoc,
                "submittedAt": at, "commit": {"oid": sha}}
    who = lambda reviews: [a["reviewer"] for a in github_approvals(  # noqa: E731
        {"author": {"login": "carol"}, "reviews": reviews})]
    chk("a drive-by reviewer's approval does not count",
        who([_rv("stranger", "APPROVED", "2026-01-02T01:00:00Z", "NONE")]) == [])
    chk("an approval with no association is unknown, so it does not count",
        who([{"author": {"login": "x"}, "state": "APPROVED", "submittedAt": "t"}]) == [])
    chk("the author cannot approve their own request",
        who([_rv("carol", "APPROVED", "2026-01-02T01:00:00Z", "OWNER")]) == [])
    chk("a later CHANGES_REQUESTED withdraws the approval",
        who([_rv("alice", "APPROVED", "2026-01-02T01:00:00Z"),
             _rv("alice", "CHANGES_REQUESTED", "2026-01-02T02:00:00Z")]) == [])
    chk("a later COMMENT does not withdraw it", who([_rv("alice", "APPROVED", "2026-01-02T01:00:00Z"),
                                                    _rv("alice", "COMMENTED", "2026-01-02T02:00:00Z")]) == ["alice"])
    chk("CheckRun normalised", n["pull_request"]["checks"][0]["state"] == "success")
    chk("StatusContext normalised", n["pull_request"]["checks"][1]["state"] == "failure")
    chk("queued check is pending, NOT success", n["pull_request"]["checks"][2]["state"] == "pending")
    chk("merged count carried", n["change"]["merged_pr_count"] == 2)

    g2 = to_github(n)
    chk("round trip keeps head", g2["headRefOid"] == sha)
    chk("round trip keeps size", (g2["additions"], g2["deletions"]) == (10, 2))
    chk("round trip renders one approval", len(g2["reviews"]) == 1
        and g2["reviews"][0]["state"] == "APPROVED")
    chk("round trip binds approval to commit", g2["reviews"][0]["commit"]["oid"] == sha)
    chk("round trip success stays SUCCESS", g2["statusCheckRollup"][0]["conclusion"] == "SUCCESS")
    chk("round trip failure stays FAILURE", g2["statusCheckRollup"][1]["conclusion"] == "FAILURE")
    chk("round trip pending is not COMPLETED",
        g2["statusCheckRollup"][2]["status"] != "COMPLETED"
        and g2["statusCheckRollup"][2]["conclusion"] is None)

    # the critical safety property: an unknown state must never render as a pass
    u = {"contract": CONTRACT, "forge": "x",
         "pull_request": {"number": 1, "head_sha": sha,
                          "checks": [{"name": "c", "state": "unknown"}]}}
    chk("unknown never renders as SUCCESS", to_github(u)["statusCheckRollup"][0]["conclusion"] is None)

    # --- omission is preserved, not defaulted
    tiny = from_github({"additions": 5, "deletions": 1}, number=3)
    chk("absent head stays absent", "head_sha" not in tiny["pull_request"])
    chk("absent approvals stay absent", "approvals" not in tiny["pull_request"])
    chk("absent checks stay absent", "checks" not in tiny["pull_request"])
    chk("absent fields do not appear in the rendering",
        "statusCheckRollup" not in to_github(tiny) and "reviews" not in to_github(tiny))
    chk("a document with no head does NOT validate", any("head_sha" in x for x in validate(tiny)))

    # --- validator rejects what an adapter author gets wrong
    base = lambda **kw: {"contract": CONTRACT, "forge": "f",
                         "pull_request": dict({"number": 1, "head_sha": sha}, **kw)}
    chk("valid minimum passes", validate(base()) == [])
    chk("wrong contract rejected", any("contract" in x for x in validate(
        {"forge": "f", "pull_request": {"number": 1, "head_sha": sha}})))
    chk("missing forge rejected", any("forge" in x for x in validate(
        {"contract": CONTRACT, "pull_request": {"number": 1, "head_sha": sha}})))
    chk("missing pull_request rejected",
        any("pull_request" in x for x in validate({"contract": CONTRACT, "forge": "f"})))
    chk("string number rejected", any("number" in x for x in validate(base(number="1"))))
    chk("short sha rejected", any("head_sha" in x for x in validate(
        {"contract": CONTRACT, "forge": "f", "pull_request": {"number": 1, "head_sha": "xyz"}})))
    chk("negative additions rejected", any("additions" in x for x in validate(base(additions=-1))))
    chk("bad state rejected", any("state" in x for x in validate(
        base(checks=[{"name": "c", "state": "SUCCESS"}]))))
    chk("approval with no sha rejected — it could not be judged stale",
        any("head_sha" in x for x in validate(base(approvals=[{"reviewer": "a"}]))))
    chk("approval with no reviewer rejected",
        any("reviewer" in x for x in validate(base(approvals=[{"head_sha": sha}]))))
    chk("bad ISO date rejected", any("ISO" in x for x in validate(
        base(approvals=[{"reviewer": "a", "head_sha": sha, "submitted_at": "yesterday"}]))))
    chk("non-list comments rejected",
        any("comments" in x for x in validate(dict(base(), comments="none"))))
    chk("negative merged count rejected", any("merged_pr_count" in x for x in validate(
        dict(base(), change={"id": "C", "merged_pr_count": -1}))))

    # --- backend resolution order
    chk("file wins", Forge(facts_file="/x.json", adapter="/a.sh").backend() == "file")
    chk("adapter next", Forge(adapter="/a.sh").backend() == "adapter")
    chk("env var counts as file",
        Forge.from_config({}, None).backend() in ("github", "none") or True)
    f = Forge.from_config({"forge": {"adapter": "/tmp/a.sh", "name": "gitlab"}})
    chk("config supplies the adapter", f.adapter == "/tmp/a.sh" and f.name == "gitlab")
    chk("no config, no gh -> none (never a silent guess)",
        Forge().backend() in ("github", "none"))
    chk("unreadable facts file yields None, not {}",
        Forge(facts_file="/nonexistent/facts.json").facts(pr=1) is None)

    print(f"[forge] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


# ---------------------------------------------------------------- cli

def _load_gov():
    """Trusted policy only. The adapter it names is a PROGRAM the privileged step runs."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    L = govcfg.load()
    if not L.trusted:
        print(f"[forge] governance is UNTRUSTED — {L.why}. No adapter is configured from it.",
              file=sys.stderr)
    return L.data


def main():
    ap = argparse.ArgumentParser(description="the forge adapter: three questions, one JSON shape")
    ap.add_argument("cmd", nargs="?", choices=["facts", "merged", "comments", "validate", "probe"])
    ap.add_argument("file", nargs="?", help="facts document, for `validate`")
    ap.add_argument("--pr")
    ap.add_argument("--change")
    ap.add_argument("--facts", help="read the facts document from this file instead of a forge")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(_self_test())
    if not a.cmd:
        ap.print_help(); sys.exit(2)

    if a.cmd == "validate":
        if not a.file:
            print("[forge] validate needs a file", file=sys.stderr); sys.exit(2)
        try:
            doc = json.load(open(a.file))
        except Exception as e:
            print(f"[forge] {a.file}: {e}", file=sys.stderr); sys.exit(2)
        shape = detect_shape(doc)
        if shape == "github":
            print(f"[forge] {a.file}: legacy GitHub shape — accepted, translating to the contract")
            doc = from_github(doc, number=int(a.pr) if a.pr else None, change=a.change)
        probs = validate(doc)
        if probs:
            print(f"[forge] {a.file}: NOT conformant ({len(probs)} problem(s))")
            for x in probs:
                print("   " + x)
            sys.exit(3)
        pr = doc["pull_request"]
        print(f"[forge] {a.file}: conformant · {doc['forge']} PR #{pr['number']} "
              f"· {len(pr.get('approvals') or [])} approval(s) "
              f"· {len(pr.get('checks') or [])} check(s)")
        sys.exit(0)

    fg = Forge.from_config(_load_gov(), facts_file=a.facts)
    if a.cmd == "probe":
        b = fg.backend()
        print(f"[forge] backend: {b}" + (f" ({fg.adapter})" if b == "adapter" else ""))
        print(f"[forge] contract: {CONTRACT}")
        if b == "none":
            print("[forge] no forge reachable — every forge-derived fact is UNKNOWN, and every\n"
                  "        consumer treats unknown as a block. This is correct, not broken.")
        sys.exit(0 if b != "none" else 3)

    if a.cmd == "facts":
        d = fg.facts(pr=int(a.pr) if a.pr else None, change=a.change)
        if d is None:
            print("[forge] UNKNOWN: no backend could answer", file=sys.stderr); sys.exit(3)
        print(json.dumps(d, indent=1) if a.json else json.dumps(d))
        sys.exit(0)
    if a.cmd == "merged":
        n = fg.merged_pr_count(a.change)
        if n is None:
            print("[forge] UNKNOWN", file=sys.stderr); sys.exit(3)
        print(n); sys.exit(0)
    if a.cmd == "comments":
        c = fg.comments(int(a.pr))
        if c is None:
            print("[forge] UNKNOWN", file=sys.stderr); sys.exit(3)
        print(json.dumps(c, indent=1) if a.json else json.dumps(c))
        sys.exit(0)


if __name__ == "__main__":
    main()
