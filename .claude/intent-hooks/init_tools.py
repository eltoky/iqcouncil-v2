#!/usr/bin/env python3
"""Arm the TOOLING a governed repository needs — T0 through T3 — and never write its CONTENT.

The boundary, which is the whole design
---------------------------------------
**Tooling is initialized; content is authored.**

Everything in here can be derived mechanically from the repository as it already is: the trust ref
comes from the protected branch, a spec scaffold comes from the spec tool, a code graph comes from
the code. None of it requires a judgement about what the software is for.

The register and the trace map are the opposite. They are claims about intent, and `intent-init`
already refuses to invent them — "populating the register with invented claims to look complete" is
a named anti-pattern. So this script will not create them, and `--all` does not quietly do it. It
prints which skills author them and leaves the choice to the operator, because the first claim in a
repository decides what every later gate is checking against.

`intent_status.py` is what keeps that handoff from being forgotten: an UNINITIALIZED register is a
note while a human is deciding and a blocking finding once nothing human is in the loop.

Why third-party binaries are resolved so carefully
--------------------------------------------------
`openspec` and `graphify` are not ours, and PATH often contains `./node_modules/.bin`. A branch can
commit an executable at that name, so running "the openspec binary" in a repository can mean
running code the branch under review supplied — the same class of defect as a branch naming its own
forge adapter, which was a real RCE in this suite. So: a tool is refused when the binary resolves
to a path INSIDE the repository, and the refusal says why. Nothing is installed by this script
either; a tool that is absent stays absent and the gap is reported.

  init_tools.py --all                 everything available, asking before each third-party run
  init_tools.py --governance          add to governance.yaml the sections a newer release introduced
                                      (never changes an existing one)
  init_tools.py --trust-ref           create refs/intent-trust/governance from the protected branch
  init_tools.py --branch REF          pin the trust ref to REF instead of the usual candidates
  init_tools.py --openspec            openspec init   (only if the binary is outside the repo)
  init_tools.py --graph               build the code graph
  init_tools.py --coverage            place and validate docs/intent/coverage.yaml, and report
                                      which languages in this tree nothing claims
  init_tools.py --dry-run --all       print exactly what would run, change nothing
  init_tools.py --yes                 do not prompt (for a scripted install, not for a schedule)
  init_tools.py --self-test

PRIVILEGED FLAGS — both change what "trusted governance" means for every later privileged read,
so they are named here as the settings audit requires:

  --force    RE-POINTS an existing refs/intent-trust/governance. Without it an existing ref is
             left alone, which is the safe default: silently moving the trust ref would let an
             initialization decide that a different commit is now the trusted policy. Use it only
             to recover a ref that was pinned before governance.yaml was committed — the case this
             script reports as DEGRADED rather than fixing on its own.
  --branch   Pins the trust ref to a named ref instead of searching origin/main, origin/master,
             main, master, HEAD in that order. Whatever is named becomes trusted policy, so a
             branch name here is as privileged as the ref itself. The commit must still carry
             docs/intent/governance.yaml; a ref that cannot be read through is refused.

EXIT: 0 everything attempted succeeded or was cleanly skipped · 1 something failed · 2 usage.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
# The identity the coverage module must carry. Must equal intent_coverage.MODULE_MARKER;
# invariant 7d checks that they agree.
COVERAGE_MARKER = "intent-loop/coverage-denominator/1"
import argparse, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# A command that did not finish has no verdict, so it gets its own code rather than
# sharing one with "failed" — the reader has to be able to tell them apart.
TIMEOUT_RC = 124          # the shell convention, so the number is recognisable
TOOL_TIMEOUT = 300        # a third-party initializer; generous, finite
GOV = "docs/intent/governance.yaml"
# The ref this script PINS for a repository with no remote. It is govcfg's LAST candidate,
# after origin/main and origin/master — never preferred over the protected branch.
import govcfg                                        # same skill; installed alongside
TRUST_REF = govcfg.PINNED_REF                        # one spelling, defined once
# The skills that author content. Named here so the refusal is actionable rather than a scolding.
CONTENT_SKILLS = (
    ("docs/intent/INTENT_REGISTER.md", "intent-kickoff (greenfield) or intent-register-builder "
                                       "(from existing docs/)"),
    ("docs/intent/INTENT_TRACE.yaml", "intent-trace-map, after the register carries claims"),
)


def _dec(v):
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")


def _timed_out(e, argv, timeout):
    """A CompletedProcess standing in for a run that never finished.

    Extracted from sh(), which the ratchet took from A(1) to B(9) when this was inline. Partial
    output is kept: a tool that printed its question before blocking has told you what it wanted.
    """
    return subprocess.CompletedProcess(
        argv, TIMEOUT_RC, _dec(e.stdout),
        _dec(e.stderr) + f"\n[timeout] no result after {timeout}s: "
                         f"{' '.join(map(str, argv))}")


def sh(*a, cwd=None, timeout=None):
    """Run a command with NO inherited stdin and, for a third-party tool, a time limit.

    `stdin=DEVNULL` is the part that was missing and it is not a nicety. `openspec init` and
    `graphify build` are other people's programs, and a program that asks a question waits for an
    answer: with stdin inherited, initialization hung forever with no output at all. It passed
    everywhere it was tested because neither binary was installed there, so only the ABSENT path
    ever ran — the live path was unexercised by construction. Reproduced with a two-line script
    that calls `read`: exit 124 after the harness timeout, zero diagnostic.

    A hang is the worst failure shape this suite has: it is not fail-closed and it is not
    fail-open, it is no verdict at all, and in a scheduled session nobody is there to notice.
    """
    try:
        return subprocess.run(a, capture_output=True, text=True, cwd=cwd,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return _timed_out(e, a, timeout)


# ---------------------------------------------------------------- binary provenance (pure)

def binary_verdict(resolved, root):
    """("ok"|"absent"|"inside-repo", detail) for a resolved binary path.

    Pure, so the rule that matters most here is testable without installing anything. The rule:
    a tool whose executable lives inside the repository is the repository's code, and running it
    during initialization would execute whatever the current branch happens to contain. `PATH`
    with `./node_modules/.bin` on it makes that an ordinary accident rather than an attack.
    """
    if not resolved:
        return "absent", "not on PATH"
    real = os.path.realpath(resolved)
    rroot = os.path.realpath(root)
    # commonpath, not startswith: "/repo-backup" startswith "/repo" and is a different directory.
    try:
        inside = os.path.commonpath([real, rroot]) == rroot
    except ValueError:                      # different drives on Windows
        inside = False
    if inside:
        return "inside-repo", (f"{resolved} resolves to {real}, which is inside the repository — "
                               f"refusing to run a tool the branch under review can supply")
    return "ok", real


def find_tool(name, root):
    return binary_verdict(shutil.which(name), root)


# ---------------------------------------------------------------- trust ref (the privileged one)

def _pin_candidates(branch):
    """Fully qualified, as in govcfg: a tag or branch NAMED origin/main must not be what gets pinned."""
    if not branch:
        return ["refs/remotes/origin/main", "refs/remotes/origin/master",
                "refs/heads/main", "refs/heads/master", "HEAD"]
    return [branch if branch == "HEAD" else (govcfg.qualify(branch) or f"refs/heads/{branch}")]


def trust_ref_plan(root, branch=None):
    """(ok, source, reason) — what the trust ref would be pinned to, or why it cannot be.

    This is the privileged act in the whole script: whatever this ref points at becomes what
    "trusted governance" means for every later privileged read. Two guards, both of which exist
    because getting them wrong inverts the rule the ref is for:

      * it is pinned to a COMMITTED state on the protected branch, never to the working tree. An
        init that pinned the working tree would make the working tree trusted, which is precisely
        the bypass `model_route.py` and `recheck_signoff.py` read this ref to avoid.
      * the chosen commit must actually CARRY governance.yaml. A ref that exists but has no
        governance on it is worse than an absent one, because `intent_status.py` would have to call
        it armed while every read still falls back.
    """
    cands = _pin_candidates(branch)
    for c in cands:
        if not c:
            continue
        r = sh("git", "rev-parse", "--verify", "--quiet", c, cwd=root)
        if r.returncode != 0:
            continue
        if sh("git", "show", f"{c}:{GOV}", cwd=root).returncode != 0:
            continue
        return True, c, f"{c} carries {GOV}"
    tried = ", ".join(x for x in cands if x)
    return False, None, (f"no candidate branch carries {GOV} (tried {tried}). Commit "
                         f"{GOV} and push it to the protected branch first — the trust ref must "
                         f"point at a reviewed commit, never at the working tree")


def create_trust_ref(root, branch=None, dry=False, force=False):
    existing = sh("git", "rev-parse", "--verify", "--quiet", TRUST_REF, cwd=root)
    if existing.returncode == 0 and not force:
        have = existing.stdout.strip()[:10]
        if sh("git", "show", f"{TRUST_REF}:{GOV}", cwd=root).returncode == 0:
            return 0, f"[trust-ref] already at {have} and carries {GOV} — unchanged"
        return 1, (f"[trust-ref] {TRUST_REF} exists at {have} but does NOT carry {GOV}; "
                   f"privileged reads still fall back to the working tree. Re-point it with "
                   f"--trust-ref --force once governance is committed")
    ok, source, reason = trust_ref_plan(root, branch)
    if not ok:
        return 1, f"[trust-ref] REFUSED: {reason}"
    sha = sh("git", "rev-parse", source, cwd=root).stdout.strip()
    if dry:
        return 0, f"[trust-ref] would pin {TRUST_REF} -> {source} ({sha[:10]}) because {reason}"
    r = sh("git", "update-ref", TRUST_REF, sha, cwd=root)
    if r.returncode != 0:
        return 1, f"[trust-ref] could not create the ref: {r.stderr.strip()}"
    # Require the artifact, not the exit code: read governance back THROUGH the ref.
    if sh("git", "show", f"{TRUST_REF}:{GOV}", cwd=root).returncode != 0:
        return 1, (f"[trust-ref] the ref was written but {GOV} cannot be read through it — "
                   f"treat this as not armed")
    return 0, f"[trust-ref] pinned {TRUST_REF} -> {source} ({sha[:10]}); {GOV} reads through it"


# ---------------------------------------------------------------- third-party tools

def _tool_failed(label, cmd, r):
    """The message for a third-party run that did not succeed.

    Two shapes, and they are kept apart because the remedy differs. A hang is reported in those
    words so the next person does not go looking for a network problem: the tool is almost
    certainly waiting for an answer nobody can give it.
    """
    if r.returncode == TIMEOUT_RC:
        return (f"[{label}] NO RESULT after {TOOL_TIMEOUT}s: `{' '.join(cmd)}` did not finish and "
                f"was stopped. Its stdin is closed by this script, so a tool that insists on "
                f"asking a question cannot block here — run it yourself once, answer it, and "
                f"re-run this step, or accept the stage as manual.")
    # Never swallow a failing step's output.
    out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    tail = "\n    ".join(out[-6:]) if out else "(no output)"
    return f"[{label}] FAILED (exit {r.returncode}):\n    {tail}"


def _attempt(root, label, cmd, marker):
    """(ok, message) for ONE run: success is the artifact, never the exit code alone."""
    r = sh(*cmd, cwd=root, timeout=TOOL_TIMEOUT)
    if r.returncode != 0:
        return False, _tool_failed(label, cmd, r)
    if marker and not os.path.exists(os.path.join(root, marker)):
        return False, (f"[{label}] `{' '.join(cmd)}` exited 0 but {marker} was not created — "
                       f"require the artifact, not the exit code")
    return True, f"[{label}] initialized ({marker}) with `{' '.join(cmd)}`"


def _preflight(root, label, name, marker):
    """(rc, message) when the run must not happen — present, absent or refused — else (None, path)."""
    if marker and os.path.exists(os.path.join(root, marker)):
        return 0, f"[{label}] {marker} already present — unchanged"
    verdict, detail = find_tool(name, root)
    if verdict == "absent":
        return 0, (f"[{label}] SKIPPED: {name} is {detail}. Nothing is installed by this script; "
                   f"install it and re-run, or accept the stage as manual")
    if verdict == "inside-repo":
        return 1, f"[{label}] REFUSED: {detail}"
    return None, detail


def _first_success(root, label, cmds, marker):
    """Try each form until one leaves the artifact. Two outcomes stop the search, because trying on
    would credit the wrong run: a TIMEOUT (the command was accepted and hung — the next form will
    not do better, and three of them cost three time limits), and a failed attempt that nonetheless
    left the artifact behind (a later form's exit 0 would then be "proved" by debris)."""
    tried = []
    for cmd in cmds:
        ok, msg = _attempt(root, label, cmd, marker)
        if ok:
            return 0, msg
        tried.append(msg)
        if "NO RESULT after" in msg:
            break
        if marker and os.path.exists(os.path.join(root, marker)):
            tried.append(f"[{label}] `{' '.join(cmd)}` FAILED but left {marker} behind — not credited "
                         f"to anything; remove it and re-run once the tool is fixed")
            break
    return 1, "\n".join(tried)


def run_tool(root, label, name, argv, marker, dry=False, confirm=None):
    """Run a third-party initializer, having proved the binary is not the repository's own.

    `argv` is one argument list, or a LIST of them to try in order: other people's command lines
    change between versions (graphify 2.1 was called with `build`, which no graphify has, and the
    owner's Mac had one answering `update .`). Each candidate is judged by the artifact; the first
    that leaves it wins, and if none does every attempt's output is reported."""
    rc, detail = _preflight(root, label, name, marker)
    if rc is not None:
        return rc, detail
    cands = argv if argv and isinstance(argv[0], list) else [argv]
    cmds = [[detail] + c for c in cands]
    alts = "".join(f" (else `{' '.join(c)}`)" for c in cmds[1:])
    if dry:
        return 0, f"[{label}] would run: {' '.join(cmds[0])}{alts}"
    if confirm and not confirm(label, cmds[0] + ([f"(or {len(cmds) - 1} fallback form(s))"] if alts else [])):
        return 0, f"[{label}] SKIPPED by the operator"
    return _first_success(root, label, cmds, marker)


# Graph tools: the binary names each ships under, the commands to try in order, and the artifact.
# There is no `graphify build` in any graphify seen; versions answer `update .` or `.`/`. --update`.
# code-review-graph's binary is `code-review-graph` (2.1 looked for `crg`, so it was never found).
GRAPH_TOOLS = (
    (("graphify",), [["update", "."], [".", "--update"], ["."]], "graphify-out"),
    (("code-review-graph", "crg"), [["build"]], ".code-review-graph"),
)


def graph_target(root):
    """(binary name, candidate argv lists, marker) for the first graph tool present, or Nones."""
    for names, cands, marker in GRAPH_TOOLS:
        name = _present_name(names, root)
        if name:
            return name, cands, marker
    return None, None, None


def _present_name(names, root):
    return next((n for n in names if find_tool(n, root)[0] != "absent"), None)


# ---------------------------------------------------------------- self-test

def _self_test_graph(check):
    import tempfile
    # --- graph tools: the command the INSTALLED version accepts, judged by the artifact.
    # A shim that knows only `update .` (the owner's graphify) and one that knows only `.`; both
    # must build, and `build` — the 2.1 command — must never be what succeeds.
    for accepts in ("update .", "."):
        bindir, repo = tempfile.mkdtemp(), tempfile.mkdtemp()
        shim = os.path.join(bindir, "graphify")
        open(shim, "w").write('#!/bin/sh\nif [ "$*" = "%s" ]; then mkdir -p graphify-out; exit 0; fi\n'
                              'echo "error: unknown command $1" >&2; exit 2\n' % accepts)
        os.chmod(shim, 0o755)
        saved_path, os.environ["PATH"] = os.environ.get("PATH", ""), bindir + os.pathsep + os.environ.get("PATH", "")
        try:
            name, cands, marker = graph_target(repo)
            rc, msg = run_tool(repo, "graph", name, cands, marker)
        finally:
            os.environ["PATH"] = saved_path
        check(f"a graphify that accepts only `{accepts}` builds the graph",
              rc == 0 and os.path.isdir(os.path.join(repo, "graphify-out")) and f"{accepts}`" in msg)
    # A failed form that leaves the artifact must not let a later form's exit 0 be "proved" by it,
    # and a form that HANGS stops the search (three forms would cost three time limits).
    for script, expect in (('if [ "$*" = "update ." ]; then mkdir -p graphify-out; exit 1; fi\nexit 0\n',
                            "left graphify-out behind"),
                           ('sleep 5\n', "NO RESULT")):
        bindir, repo = tempfile.mkdtemp(), tempfile.mkdtemp()
        open(os.path.join(bindir, "graphify"), "w").write("#!/bin/sh\n" + script)
        os.chmod(os.path.join(bindir, "graphify"), 0o755)
        saved = (os.environ.get("PATH", ""), globals()["TOOL_TIMEOUT"])
        os.environ["PATH"], globals()["TOOL_TIMEOUT"] = bindir + os.pathsep + saved[0], 1
        try:
            name, cands, marker = graph_target(repo)
            rc, msg = run_tool(repo, "graph", name, cands, marker)
        finally:
            os.environ["PATH"], globals()["TOOL_TIMEOUT"] = saved
        check(f"the search stops at: {expect}", rc == 1 and msg.count(expect) == 1
              and "initialized" not in msg and len(msg.split("\n[graph]")) <= 2)
    check("no candidate is the nonexistent `graphify build`",
          all(c != ["build"] for c in GRAPH_TOOLS[0][1]))
    bindir, repo = tempfile.mkdtemp(), tempfile.mkdtemp()
    open(os.path.join(bindir, "code-review-graph"), "w").write("#!/bin/sh\nmkdir -p .code-review-graph\n")
    os.chmod(os.path.join(bindir, "code-review-graph"), 0o755)
    saved_path, os.environ["PATH"] = os.environ.get("PATH", ""), bindir
    try:
        found = graph_target(repo)[0]
    finally:
        os.environ["PATH"] = saved_path
    check("code-review-graph is found under its real name", found == "code-review-graph")



TEMPLATE_FIXTURE = ("# head\n\n# the merge\nmerge:\n  auto_merge: after_signoff\n\nautonomy:\n  mode: hitl\n\n"
                    "# what was asked\ninit_decisions:\n  notices: \"\"\n\npeople: {}\n#   lead: {name: x}\n")


def _gov_run(text, steps=1, link_out=False):
    """Run governance_step against `text` with a fixture template; [(rc, msg)], final file text."""
    import tempfile
    tree = tempfile.mkdtemp()                       # a skills tree: intent-init + intent-escalation
    os.makedirs(os.path.join(tree, "intent-init", "scripts"))
    os.makedirs(os.path.join(tree, "intent-escalation", "assets"))
    open(os.path.join(tree, "intent-escalation", "assets", "governance.yaml"), "w").write(TEMPLATE_FIXTURE)
    r = tempfile.mkdtemp()
    os.makedirs(os.path.join(r, "docs", "intent"))
    gov = os.path.join(r, GOV)
    target = gov
    if link_out:
        target = os.path.join(tempfile.mkdtemp(), "outside.yaml")
        os.symlink(target, gov)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    saved, globals()["HERE"] = globals()["HERE"], os.path.join(tree, "intent-init", "scripts")
    try:
        runs = [governance_step(r, dry=(i == 0 and steps > 2)) for i in range(steps)]
    finally:
        globals()["HERE"] = saved
    return runs, open(target, encoding="utf-8").read()


def _self_test_governance(check):
    import yaml
    (dry, first, second), text = _gov_run("# mine\nmerge:\n  auto_merge: \"off\"   # my choice\n", steps=3)
    check("a dry run names the missing sections", dry[0] == 0 and "autonomy, init_decisions, people" in dry[1])
    check("missing sections are added, comments and all",
          first[0] == 0 and "# what was asked\ninit_decisions:" in text and "#   lead: {name: x}" in text)
    check("an existing section is never changed", 'auto_merge: "off"   # my choice' in text
          and len(re.findall(r"(?m)^merge:", text)) == 1)
    check("a second run changes nothing", second[0] == 0 and "unchanged" in second[1])
    (bad,), btext = _gov_run("merge: [unclosed\n")
    check("a governance file that does not parse is refused, not rewritten",
          bad[0] == 1 and btext == "merge: [unclosed\n")
    # The review's attack: keys a text scan misses must still count as PRESENT, or the shipped
    # copy is appended and YAML's last-duplicate-wins replaces the repository's own value.
    for label, src in (("a byte-order mark", '\ufeffmerge: {auto_merge: "off"}\nautonomy: {mode: hotl}\n'),
                       ("quoted keys", '"merge": {auto_merge: "off"}\n\'autonomy\': {mode: hotl}\n'),
                       ("a space before the colon", 'merge : {auto_merge: "off"}\nautonomy : {mode: hotl}\n')):
        (res,), out = _gov_run(src)
        d = yaml.safe_load(out.lstrip("\ufeff"))
        check(f"{label}: the repository's merge and autonomy survive",
              res[0] == 0 and d["merge"]["auto_merge"] == "off" and d["autonomy"]["mode"] == "hotl"
              and "merge" not in res[1].split("added:")[-1].split("—")[0])
    # The second guard on its own: whatever the key scan decides, a result that would change an
    # existing section is refused rather than written.
    new, why = _merged('merge: {auto_merge: "off"}\n', [("merge", "merge:\n  auto_merge: unattended\n")])
    check("a merge that would replace an existing value is refused", new is None and "meaning" in why)
    (lnk,), _ = _gov_run("merge: {}\n", link_out=True)
    check("a governance.yaml that is a symlink out of the repository is refused", lnk[0] == 1)


def _self_test():
    import tempfile
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    root = tempfile.mkdtemp()
    # The provenance rule, which is the security-relevant part of this script.
    v, d = binary_verdict(None, root)
    check("an absent binary is 'absent'", v == "absent")
    v, d = binary_verdict("/usr/bin/git", root)
    check("a system binary is ok", v == "ok")
    inside = os.path.join(root, "node_modules", ".bin")
    os.makedirs(inside, exist_ok=True)
    fake = os.path.join(inside, "openspec")
    open(fake, "w").write("#!/bin/sh\n")
    v, d = binary_verdict(fake, root)
    check("a binary inside the repo is refused", v == "inside-repo")
    check("the refusal names the resolved path", fake in d or os.path.realpath(fake) in d)
    # A sibling directory sharing a prefix is NOT inside the repo. startswith() gets this wrong.
    sib = root + "-backup"
    os.makedirs(sib, exist_ok=True)
    other = os.path.join(sib, "openspec")
    open(other, "w").write("#!/bin/sh\n")
    v, d = binary_verdict(other, root)
    check("a sibling path with a shared prefix is not 'inside'", v == "ok")
    # A symlink from outside INTO the repo is still the repo's code.
    link = os.path.join(tempfile.mkdtemp(), "openspec")
    try:
        os.symlink(fake, link)
        v, d = binary_verdict(link, root)
        check("a symlink into the repo is refused", v == "inside-repo")
    except OSError:
        check("a symlink into the repo is refused (skipped: no symlink support)", True)

    # The trust-ref plan must refuse when nothing carries governance.
    r = tempfile.mkdtemp()
    sh("git", "init", "-q", ".", cwd=r)
    sh("git", "config", "user.email", "t@t", cwd=r)
    sh("git", "config", "user.name", "T", cwd=r)
    open(os.path.join(r, "f"), "w").write("x")
    sh("git", "add", "-A", cwd=r); sh("git", "commit", "-qm", "seed", cwd=r)
    okp, src, why = trust_ref_plan(r)
    check("no governance anywhere -> refused", not okp)
    check("the refusal says to commit governance first", "commit" in why.lower())
    rc, msg = create_trust_ref(r)
    check("create refuses without governance", rc == 1 and "REFUSED" in msg)
    check("nothing was written on refusal",
          sh("git", "rev-parse", "--verify", "--quiet", TRUST_REF, cwd=r).returncode != 0)

    # With governance committed, it pins and reads back through the ref.
    os.makedirs(os.path.join(r, "docs/intent"), exist_ok=True)
    open(os.path.join(r, GOV), "w").write("merge:\n  auto_merge: \"off\"\n")
    sh("git", "add", "-A", cwd=r); sh("git", "commit", "-qm", "gov", cwd=r)
    okp, src, why = trust_ref_plan(r)
    check("a committed governance file makes a plan", okp and src)
    rc, msg = create_trust_ref(r, dry=True)
    check("--dry-run does not write the ref",
          rc == 0 and sh("git", "rev-parse", "--verify", "--quiet", TRUST_REF,
                         cwd=r).returncode != 0)
    check("--dry-run says what it would do", "would pin" in msg)
    rc, msg = create_trust_ref(r)
    check("the ref is created", rc == 0)
    check("governance reads through the ref",
          sh("git", "show", f"{TRUST_REF}:{GOV}", cwd=r).returncode == 0)
    rc2, msg2 = create_trust_ref(r)
    check("re-running is idempotent", rc2 == 0 and "unchanged" in msg2)

    # A working-tree-only governance file must NOT be enough: that would make the working tree
    # trusted, which is the bypass the ref exists to prevent.
    r2 = tempfile.mkdtemp()
    sh("git", "init", "-q", ".", cwd=r2)
    sh("git", "config", "user.email", "t@t", cwd=r2)
    sh("git", "config", "user.name", "T", cwd=r2)
    open(os.path.join(r2, "x"), "w").write("1")
    sh("git", "add", "-A", cwd=r2); sh("git", "commit", "-qm", "seed", cwd=r2)
    os.makedirs(os.path.join(r2, "docs/intent"), exist_ok=True)
    open(os.path.join(r2, GOV), "w").write("merge: {}\n")      # written, never committed
    okp, src, why = trust_ref_plan(r2)
    check("an uncommitted governance file is refused", not okp)

    # run_tool: the marker is the artifact, not the exit code.
    r3 = tempfile.mkdtemp()
    # MANUFACTURE the absence (rule 23): on a machine with openspec installed — the owner's Mac —
    # this check used to find the real binary and fail, while every clean container passed it.
    saved_path, os.environ["PATH"] = os.environ.get("PATH", ""), tempfile.mkdtemp()
    try:
        rc, msg = run_tool(r3, "openspec", "openspec", ["init"], "openspec", dry=True)
    finally:
        os.environ["PATH"] = saved_path
    check("an absent tool is skipped, not failed", rc == 0 and "SKIPPED" in msg)
    os.makedirs(os.path.join(r3, "openspec"), exist_ok=True)
    rc, msg = run_tool(r3, "openspec", "openspec", ["init"], "openspec")
    check("an existing marker means unchanged", rc == 0 and "unchanged" in msg)
    # A tool that exits 0 and creates nothing must fail.
    r4 = tempfile.mkdtemp()
    rc, msg = run_tool(r4, "noop", "true", [], "never-created")
    check("exit 0 without the artifact is a failure", rc == 1 and "not created" in msg)

    _self_test_graph(check)
    _self_test_governance(check)

    # --- the coverage denominator -------------------------------------------------------------
    # Placing it is initialization; an INVALID one must be refused rather than accepted, because a
    # declaration nothing can parse makes every file unmeasurable, which is the failure the whole
    # denominator exists to prevent.
    r5 = tempfile.mkdtemp()
    sh("git", "init", "-q", cwd=r5)
    os.makedirs(os.path.join(r5, "setup"), exist_ok=True)
    tmpl = os.path.join(os.path.dirname(HERE), "..", "intent-smoke-tests", "assets",
                        "coverage.yaml")
    if os.path.exists(tmpl):
        # The validation is a CROSS-SKILL import that resolves only in the flat installed runtime,
        # so from a source checkout it degrades to "NOT VALIDATED here" — and a test that accepted
        # that would pass while proving nothing. Put the sibling's script directory on the path,
        # the way install_runtime's flat layout does, and then assert that validation really ran.
        sys.path.insert(0, os.path.normpath(
            os.path.join(os.path.dirname(HERE), "..", "intent-smoke-tests", "scripts")))
        shutil.copyfile(tmpl, os.path.join(r5, "setup", "coverage.yaml"))
        rc5, m5 = coverage_step(r5, dry=True)
        check("a dry run places nothing", rc5 == 0 and not os.path.exists(os.path.join(r5, COV)))
        rc5, m5 = coverage_step(r5)
        check("the declaration is placed and validates",
              rc5 == 0 and os.path.exists(os.path.join(r5, COV))
              and "NOT VALIDATED" not in m5)
        # NEGATIVE CONTROL: an unparseable declaration must FAIL, not be reported as armed.
        open(os.path.join(r5, COV), "w", encoding="utf-8").write("languages: [this is not a map\n")
        rc6, m6 = coverage_step(r5)
        check("an invalid declaration is refused", rc6 == 1)
        # An extension nothing claims must be NAMED, not counted silently.
        shutil.copyfile(tmpl, os.path.join(r5, COV))
        open(os.path.join(r5, "thing.zzq"), "w", encoding="utf-8").write("x\n")
        sh("git", "add", "-A", cwd=r5)
        rc7, m7 = coverage_step(r5)
        check("an unclaimed extension is named", rc7 == 0 and ".zzq" in m7)
    else:
        check("the coverage template is reachable from this script", False)

    # Content is never generated here.
    check("the content skills are named for the handoff", len(CONTENT_SKILLS) == 2
          and all(s for _, s in CONTENT_SKILLS))

    print(f"[init-tools] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


COV = "docs/intent/coverage.yaml"


def _tree_extensions(root, limit=20000):
    """The distinct file extensions tracked by git, as a set. Mechanical, so it belongs here.

    `git ls-files` rather than a walk: an untracked build directory is not what the gate will
    measure, and a 200k-file vendor tree should not decide the denominator.
    """
    r = sh("git", "ls-files", cwd=root)
    if r.returncode != 0:
        return None
    exts = set()
    for i, line in enumerate(r.stdout.splitlines()):
        if i >= limit:
            break
        b = os.path.basename(line)
        if "." in b[1:]:
            exts.add(os.path.splitext(b)[1].lower())
    return exts


def _place_coverage(root, dst, dry):
    """(rc, message, placed) — put the declaration in place, or say why it could not be.

    Extracted from coverage_step, which rated D(21) against this project's C(20) ceiling for new
    code. Three outcomes, and the caller stops on the first two.
    """
    if os.path.exists(dst):
        return 0, None, "already present"
    # The installed runtime carries the template beside this script; a source checkout has it
    # under the skill that owns it. Both are tried so this works before and after install.
    cands = [os.path.join(HERE, "coverage.template.yaml"),
             os.path.join(HERE, "..", "..", "intent-smoke-tests", "assets", "coverage.yaml"),
             os.path.join(root, "setup", "coverage.yaml")]
    src = next((c for c in cands if os.path.exists(c)), None)
    if src is None:
        return 1, (f"[coverage] FAILED: {COV} is absent and no template was found in any of "
                   f"{[os.path.normpath(c) for c in cands]}. Install the runtime first."), ""
    if dry:
        return 0, f"[coverage] would place {COV} from the shipped declaration, then validate it", ""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return 0, None, "placed from the shipped declaration"


def _ours():
    """The coverage module, or raise. Identity is checked, not assumed.

    Until 1.22.0 this module was named `coverage.py`, which shadows the PyPI `coverage` package —
    a dependency of pytest-cov and therefore present on a great many machines. `import coverage`
    then resolved to that package and the first call into it raised AttributeError. The rename is
    the fix; this check is the belt, and it lives in one place so the ratchet does not charge
    every caller for it.
    """
    try:
        import intent_coverage as m
    except Exception as exc:                                     # noqa: BLE001
        # Guarded AT THE IMPORT NODE, which is what invariant Z2 requires and what rule 20 is
        # about: a caller wrapping the CALL is not the same as guarding the import, and the
        # strict reading is the one that survives the next refactor.
        raise ImportError(f"intent_coverage is not importable here: {exc}") from exc
    # Checked from OUR side, against a constant the caller owns. The first version asked the
    # module `m.is_ours(m)` — but you cannot ask a stranger whether it is yours: an impostor has
    # no `is_ours`, so the check raised AttributeError instead of refusing. UAT-L12 found it.
    if getattr(m, "MODULE_MARKER", None) != COVERAGE_MARKER:
        raise ImportError(f"intent_coverage resolved to {getattr(m, '__file__', '?')}, which is "
                          f"not this suite's module")
    return m


def _validate_coverage(dst, placed):
    """(rc, message, cfg) — parse and validate, or report why validation did not happen.

    The import is CROSS-SKILL: intent_coverage.py ships in intent-smoke-tests and resolves only in the
    flat installed runtime. Guarded, and declared in invariant Z2 — an uninstalled checkout
    degrades to a report rather than a crash, and the merge gate's coverage_gate still validates.
    """
    try:
        import yaml
        _cov = _ours()
    except Exception as e:                                       # noqa: BLE001
        return 0, (f"[coverage] {COV} {placed}; NOT VALIDATED here ({e.__class__.__name__}: {e}) "
                   f"— the acceptance gate validates it on every run"), None
    try:
        cfg = yaml.safe_load(open(dst, encoding="utf-8")) or {}
    except Exception as e:                                       # noqa: BLE001
        return 1, f"[coverage] FAILED: {COV} does not parse: {e}", None
    probs = _cov.validate(cfg)
    if probs:
        # The % applies to the string, NOT to the tuple: an earlier version closed the paren after
        # `None`, so this branch returned two values where the caller unpacks three and raised
        # ValueError instead of refusing. It had never been executed, because the only negative
        # fixture was unparseable YAML and stopped one branch earlier. UAT-M12 now attacks both.
        return 1, ("[coverage] REFUSED: %s is not conformant, and an invalid denominator is worse "
                   "than none — every file would read as unmeasurable.\n    %s"
                   % (COV, "\n    ".join(probs))), None
    return 0, None, cfg


def _reason_lines(cfg):
    """The advisory block for axes declared unmeasurable with no reason — never blocking.

    Returns the formatted text rather than a list so that the caller does not carry a second
    comprehension: adding one pushed _unclaimed_report from B(10) to C(11), and this project
    extracts instead of re-baselining.

    Guarded for the same reason the validation is: intent_coverage.py is a cross-skill import that
    resolves only in the flat installed runtime.
    """
    try:
        return "".join("\n    ADVISORY: " + r for r in _ours().missing_reasons(cfg))
    except Exception:                                            # noqa: BLE001
        return ""


def _unclaimed_report(cfg, root, placed):
    """(rc, message) — name every extension in the tree that no language claims.

    Named rather than counted: the list IS the work, and a count tells the operator nothing they
    can act on. Reported, never auto-declared — declaring a language is a judgement about what can
    measure it, and inventing `tests: none` on the operator's behalf would manufacture the silent
    pass the denominator exists to end.
    """
    claimed = set()
    for spec in (cfg.get("languages") or {}).values():
        claimed |= {str(x).lower() for x in (spec.get("extensions") or [])}
    exts = _tree_extensions(root)
    if exts is None:
        return 0, f"[coverage] {COV} {placed} and validates; the tree could not be listed"
    unclaimed = sorted(e for e in (exts - claimed) if e)
    tail = _reason_lines(cfg)
    if not unclaimed:
        return 0, (f"[coverage] {COV} {placed} and validates; every extension here is claimed"
                   + tail)
    shown = ", ".join(unclaimed[:18]) + (" ..." if len(unclaimed) > 18 else "")
    return 0, (f"[coverage] {COV} {placed} and validates. {len(unclaimed)} extension(s) in this "
               f"tree are claimed by NOTHING: {shown}\n    Each is a COVERAGE-UNKNOWN finding the "
               f"first time a change touches it. Declare each one — `tests: none` with a reason is "
               f"an answer; silence is not. Most of these will be data, assets or documentation, "
               f"and a `docs`/`config` language with a note: is where those belong." + tail)


def coverage_step(root, dry=False):
    """(rc, message) — place the coverage denominator, validate it, and name what it misses.

    Declaring which tool claims which language is TOOLING, not content: the languages in a
    repository are a mechanical fact about its files, and the failure this prevents is a gate that
    reports an omission as zero. Authoring a CLAIM is the judgement, and that is still refused.

    This runs as part of `--all` rather than as an instruction in a setup guide. A denominator the
    operator has to remember to place is a denominator that is sometimes absent, and intent_status
    would then be reporting on their memory rather than on the repository.
    """
    dst = os.path.join(root, COV)
    rc, msg, placed = _place_coverage(root, dst, dry)
    if msg is not None:
        return rc, msg
    if dry:
        return 0, f"[coverage] would validate {COV} ({placed})"
    rc, msg, cfg = _validate_coverage(dst, placed)
    if msg is not None:
        return rc, msg
    return _unclaimed_report(cfg, root, placed)


# ---------------------------------------------------------------- governance sections (upgrade)

GOV_TEMPLATE = "governance.template.yaml"


def top_blocks(text):
    """[(key, text)] — each top-level key of a YAML file with its own lines, the comment lines
    directly above it (up to a blank line) included. Text, not a parse, so comments survive."""
    lines = text.splitlines(keepends=True)
    keys = [(i, m.group(1)) for i, ln in enumerate(lines)
            for m in [re.match(r"([A-Za-z_][\w-]*):", ln)] if m]
    starts = []
    for i, _k in keys:
        s = i
        while s > 0 and lines[s - 1].startswith("#"):
            s -= 1
        starts.append(s)
    return [(k, "".join(lines[starts[n]:(starts[n + 1] if n + 1 < len(starts) else len(lines))]))
            for n, (_i, k) in enumerate(keys)]


def _governance_template():
    """The shipped template, from the same place this script came from — never from the
    repository's own tree. Installed runtime: beside this script. A skills tree: the sibling
    intent-escalation skill, accepted only when this really is a skills tree."""
    beside = os.path.join(HERE, GOV_TEMPLATE)
    if os.path.exists(beside):
        return beside
    skills = os.path.normpath(os.path.join(HERE, "..", ".."))
    sib = os.path.join(skills, "intent-escalation", "assets", "governance.yaml")
    if os.path.isdir(os.path.join(skills, "intent-init")) and os.path.exists(sib):
        return sib
    return None


def _merged(have, missing):
    """(new text, None) or (None, why). The result must parse, keep EVERY existing top-level key
    with exactly its old value, and add exactly the missing ones — so a key the text scan did not
    recognise can never be appended twice and silently replaced (YAML keeps the LAST duplicate)."""
    import yaml
    old = yaml.safe_load(have.lstrip("\ufeff")) or {}
    new = have.rstrip("\n") + "\n\n" + "".join(b.rstrip("\n") + "\n\n" for _, b in missing)
    try:
        nd = yaml.safe_load(new.lstrip("\ufeff"))
    except yaml.YAMLError as e:
        return None, f"the result would not parse ({e})"
    if not isinstance(nd, dict) or any(nd.get(k) != v for k, v in old.items()) \
            or set(nd) != set(old) | {k for k, _ in missing}:
        return None, "the result would change an existing section's meaning"
    return new.rstrip("\n") + "\n", None


def _governance_inputs(root, dst):
    """(have, template_text, None) or (None, None, (rc, message)) — the checks before any write."""
    if not os.path.exists(dst):
        return None, None, (0, f"[governance] SKIPPED: {GOV} is absent — the runtime installer places it")
    if os.path.commonpath([os.path.realpath(dst), os.path.realpath(root)]) != os.path.realpath(root):
        return None, None, (1, f"[governance] REFUSED: {GOV} resolves outside the repository "
                               f"({os.path.realpath(dst)})")
    try:
        import yaml                                    # noqa: F401
    except ImportError:
        return None, None, (1, "[governance] BLOCKED: pyyaml is not installed (pip install pyyaml); "
                               f"{GOV} unchanged")
    tmpl = _governance_template()
    if tmpl is None:
        return None, None, (1, "[governance] FAILED: no governance template found beside this "
                               "script or in its skills tree — install the runtime first")
    return io_text(dst), io_text(tmpl), None


def governance_step(root, dry=False):
    """(rc, message) — ADD the top-level sections the shipped template has and this repository's
    governance.yaml lacks. Never changes a section that exists: configuration is the repository's.

    An upgrade used to depend on the operator noticing that a release added `loop_notices` or
    `people`; the docs said intent-init "adds" a new section and no code did. Each added section
    arrives exactly as shipped — switched off where it has a switch — and any question it raises is
    recorded as pending in init_decisions, which intent_status reports until it is answered.
    Which keys exist is decided by PARSING the file, never by a text scan: a byte-order mark or a
    quoted key once made `merge:` look absent, and the appended copy replaced the repository's own."""
    import yaml
    dst = os.path.join(root, GOV)
    have, tmpl, stop = _governance_inputs(root, dst)
    if stop:
        return stop
    try:
        old = yaml.safe_load(have.lstrip("\ufeff")) or {}
    except yaml.YAMLError as e:
        return 1, f"[governance] REFUSED: {GOV} does not parse ({e}); unchanged"
    if not isinstance(old, dict):
        return 1, f"[governance] REFUSED: {GOV} is not a mapping; unchanged"
    missing = [(k, b) for k, b in top_blocks(tmpl) if k not in {str(x) for x in old}]
    if not missing:
        return 0, "[governance] every shipped section is present — unchanged"
    names = ", ".join(k for k, _ in missing)
    if dry:
        return 0, f"[governance] would add: {names}"
    new, why = _merged(have, missing)
    if new is None:
        return 1, f"[governance] REFUSED: adding {names} — {why}; {GOV} unchanged"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(new)
    return 0, (f"[governance] added: {names} — as shipped; their questions are pending until "
               f"answered (intent_status.py --decisions)")


def io_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


STEPS = ("governance", "trust_ref", "coverage", "openspec", "graph")


def selected(a):
    """The step names chosen, as a set.

    So run_steps branches on membership rather than on `a.all or a.<flag>` four times over. Adding
    the coverage step pushed run_steps from B(8) to B(10) and main from B(10) to C(11), and this
    project extracts rather than re-baselining: a ceiling you raise when you hit it is not one.
    """
    return {n for n in STEPS if a.all or getattr(a, n, False)}


def run_steps(a, confirm):
    """(rc, messages) — the selected tooling steps, in a fixed order.

    Extracted from main() to keep it under the C(20) ceiling that this project applies to new code
    on the security path. The order is deliberate: the governance sections first, so a commit made
    to pin the trust ref carries them; then the trust ref, the one step whose absence means every
    later privileged read falls back to the working tree.
    """
    steps = {
        "governance": lambda: governance_step(a.root, dry=a.dry_run),
        "trust_ref": lambda: create_trust_ref(a.root, a.branch, dry=a.dry_run, force=a.force),
        "coverage": lambda: coverage_step(a.root, dry=a.dry_run),
        "openspec": lambda: run_tool(a.root, "openspec", "openspec", ["init"], "openspec",
                                     dry=a.dry_run, confirm=confirm),
        "graph": lambda: graph_step(a, confirm),
    }
    rc, results, sel = 0, [], selected(a)
    for name in STEPS:                            # the fixed order, whatever was selected
        if name in sel:
            code, msg = steps[name]()
            rc |= code
            results.append(msg)
    return rc, results


def graph_step(a, confirm):
    name, argv, marker = graph_target(a.root)
    if not name:
        return 0, ("[graph] SKIPPED: no graph tool on PATH \u2014 blast-radius review stays "
                   "manual, and the CLAUDE.md block should say so")
    return run_tool(a.root, "graph", name, argv, marker, dry=a.dry_run, confirm=confirm)


def print_handoff(root):
    """Name the skills that author content, every run.

    Printed unconditionally rather than only on failure, because the tooling/content split is the
    design and the content step is a choice someone has to make \u2014 not a box this script ticks.
    """
    missing = [(p, s) for p, s in CONTENT_SKILLS if not os.path.exists(os.path.join(root, p))]
    if missing:
        print("\n[init-tools] tooling only. These are CONTENT and are deliberately not generated "
              "here \u2014 choose the skill that authors them:")
        for p, s in missing:
            print(f"    {p:34s} -> {s}")
        print("    An invented claim is worse than an empty register: every later gate would be "
              "checking against it.")
    print(f"\n[init-tools] what is armed now:  python3 intent_status.py --root {root}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--all", action="store_true", help="every tooling step that is available")
    ap.add_argument("--governance", action="store_true",
                    help="add the sections a newer release introduced to docs/intent/governance.yaml")
    ap.add_argument("--trust-ref", action="store_true")
    ap.add_argument("--branch", help="pin the trust ref to this ref instead of the usual candidates")
    ap.add_argument("--force", action="store_true", help="re-point an existing trust ref")
    ap.add_argument("--openspec", action="store_true")
    ap.add_argument("--graph", action="store_true")
    ap.add_argument("--coverage", action="store_true",
                    help="place and validate docs/intent/coverage.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="do not prompt before a third-party run")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not selected(a):
        ap.print_help()
        return 2
    if sh("git", "rev-parse", "--git-dir", cwd=a.root).returncode != 0:
        print(f"[init-tools] not a git repository: {a.root}", file=sys.stderr)
        return 2

    attended = sys.stdin.isatty() and sys.stdout.isatty()

    def confirm(label, cmd):
        if a.yes:
            return True
        if not attended:
            # A third-party initializer writes into the repository. In a session with nobody
            # watching, that is not a decision to take silently — report and leave it undone.
            print(f"[{label}] NOT RUN: would execute `{' '.join(cmd)}`, and nothing is attended "
                  f"to approve it. Re-run with --yes if that is what you want.")
            return False
        try:
            return input(f"[{label}] run `{' '.join(cmd)}`? [y/N] ").strip().lower() == "y"
        except EOFError:
            return False

    rc, results = run_steps(a, confirm)
    for m in results:
        print(m)
    print_handoff(a.root)
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
