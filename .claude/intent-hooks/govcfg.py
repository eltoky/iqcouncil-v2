#!/usr/bin/env python3
"""The ONE place the loop reads its policy, and the ONE place a policy-named program is resolved.

    govcfg.py --where                     which ref is trusted here, and why
    govcfg.py --show [--path FILE]        the trusted copy of a policy file, or why there is none
    govcfg.py --self-test

Why this module exists
----------------------
Before 2.0.0, fifteen files read `docs/intent/governance.yaml`, each with its own idea of trust:

  * two different refs were both called "the trust ref" — `origin/main` / `$INTENT_TRUST_REF` in
    five scripts, a locally pinned `refs/intent-trust/governance` in three others;
  * three different fallbacks when that ref was missing — refuse, warn-and-use-the-working-tree, and
    silently use the working tree;
  * and two scripts read the working tree unconditionally.

One of those two was the escalation check. In CI the privileged gate runs from inside the PR's own
checkout, so it loaded the PR's governance file and executed the `forge.adapter` the PR named, with
the CI token in its environment. Reproduced: a two-line script supplied by a PR ran with GH_TOKEN
set. That is the class CLAUDE.md rule 2 records as closed; it came back through a reader nobody
had routed through the rule, because the rule lived in each reader instead of in one.

So there are exactly two rules, and they live here:

  1. POLICY IS READ FROM THE TRUST REF, NEVER FROM THE WORKING TREE, for anything privileged.
     `load(trusted=True)` returns an EMPTY policy, flagged untrusted, when no trust ref carries the
     file. Callers fail closed on `.trusted is False`; an empty policy means the strict defaults.
     `load(trusted=False)` reads the working tree and says so — for hooks, which can only block,
     and for reporting.

  2. A PROGRAM NAMED BY POLICY RESOLVES AGAINST THE TRUSTED CHECKOUT, NEVER THE PR'S.
     Reading the adapter's NAME from trusted policy is not enough: the documented example is
     `adapter: .intent/adapters/gitlab.sh`, a relative path, and in CI the working directory is
     the PR checkout — so a trusted config still ran the PR's copy of that file. `resolve_program`
     anchors a relative path on `$INTENT_TRUSTED_ROOT` (set by the acceptance workflow to the
     protected-branch checkout) and REFUSES any program whose real path lies inside the untrusted
     working tree when the two differ.

The trust ref, resolved once
----------------------------
  $INTENT_TRUST_REF if set — and then ONLY it: an explicit pin that does not resolve is untrusted,
  never a reason to try something weaker. Otherwise the first of:
      origin/main · origin/master · refs/intent-trust/governance
  that actually carries the file. The pinned ref is last and exists for a repository with no
  remote; it is never preferred over the remote's protected branch, because it is pinned once and
  never advances. Local `main` / `master` are NOT candidates: an agent can commit to them.

This file is the single source. `tools/sync_shared.py` copies it into every skill that needs it,
byte for byte, and the build fails if a copy drifts.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, json, argparse, subprocess

GOV = "docs/intent/governance.yaml"
PINNED_REF = "refs/intent-trust/governance"
# FULLY QUALIFIED, always. `git show origin/main:<path>` resolves `origin/main` by git's DWIM order
# — refs/tags/origin/main and refs/heads/origin/main come BEFORE refs/remotes/origin/main — so an
# agent that could push a TAG named `origin/main` replaced the gate's whole policy (approver keys,
# mode, fences, notify.command) with git printing only "refname is ambiguous". Reproduced in the
# 2.0.0 review. A ref name the gate trusts is never one git has to guess.
CANDIDATES = ("refs/remotes/origin/main", "refs/remotes/origin/master", PINNED_REF)
MODULE_MARKER = "intent-loop/govcfg/1"


class Loaded:
    """A policy document and where it came from. `trusted` is the only field a gate may branch on."""

    __slots__ = ("data", "source", "trusted", "ref", "why")

    def __init__(self, data, source, trusted, ref=None, why=""):
        self.data, self.source, self.trusted, self.ref, self.why = data, source, trusted, ref, why

    def get(self, key, default=None):
        return self.data.get(key, default) if isinstance(self.data, dict) else default

    def __repr__(self):
        return f"Loaded(source={self.source!r}, trusted={self.trusted})"


def _git(*args, root=None):
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root or None,
                              stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(["git", *args], 128, "", str(e))


def qualify(name):
    """A ref name the gate may trust, fully qualified — or None. `refs/...` is taken as written;
    `<remote>/<branch>` means refs/remotes/<remote>/<branch>; anything else is ambiguous by
    construction and refused rather than guessed."""
    name = (name or "").strip()
    if name.startswith("refs/") and ".." not in name:
        return name
    if "/" in name and not name.startswith(("/", "-")) and ".." not in name and " " not in name:
        return "refs/remotes/" + name
    return None


def candidates():
    """The refs to try, in order. An explicit pin is the ONLY candidate when it is set — and a pin
    that cannot be qualified is no candidate at all (untrusted), never a fallback."""
    pinned = (os.environ.get("INTENT_TRUST_REF") or "").strip()
    return _pin(pinned) if pinned else CANDIDATES


def _pin(pinned):
    q = qualify(pinned)
    return (q,) if q else ()


def read_trusted(path=GOV, root=None):
    """(text, ref) from the first trusted ref that carries `path`, or (None, None)."""
    for ref in candidates():
        r = _git("show", f"{ref}:{path}", root=root)
        if r.returncode == 0:
            return r.stdout, ref
    return None, None


def resolve_ref(path=GOV, root=None):
    """The ref policy is read from here, or None when no trusted ref carries `path`."""
    return read_trusted(path, root)[1]


def _parse(text):
    import yaml
    d = yaml.safe_load(text) if text else {}
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ValueError(f"top level is {type(d).__name__}, not a mapping")
    return d


def load(path=GOV, root=None, trusted=True, override=None):
    """The policy at `path`.

    trusted=True   from the trust ref; EMPTY and untrusted when there is none. Never the working tree.
    trusted=False  from the working tree, labelled as such — for block-only hooks and reports.
    override=FILE  an explicit file, for tests and the documented `--config` flags. Treated as
                   trusted because whoever passes it controls the process anyway; it is never
                   reached from anything a branch supplies.
    An unparseable document is untrusted and empty: every unknown is a NO.
    """
    if override:
        try:
            with open(override, encoding="utf-8") as f:
                return Loaded(_parse(f.read()), f"file:{override}", True)
        except (OSError, ValueError, Exception) as e:          # noqa: BLE001
            return Loaded({}, f"file:{override}", False, why=f"unreadable: {e}")
    if trusted:
        text, ref = read_trusted(path, root)
        if text is None:
            tried = ", ".join(candidates())
            return Loaded({}, "UNTRUSTED", False,
                          why=f"{path} is on none of the trusted refs ({tried}); nothing from the "
                              f"working tree is used for a privileged decision")
        try:
            return Loaded(_parse(text), ref, True, ref=ref)
        except Exception as e:                                   # noqa: BLE001
            return Loaded({}, ref, False, ref=ref, why=f"{path} on {ref} does not parse: {e}")
    p = os.path.join(root or ".", path)
    try:
        with open(p, encoding="utf-8") as f:
            return Loaded(_parse(f.read()), "working-tree", False,
                          why="read from the working tree, which the branch under review controls")
    except FileNotFoundError:
        return Loaded({}, "working-tree", False, why=f"{p} does not exist")
    except Exception as e:                                       # noqa: BLE001
        return Loaded({}, "working-tree", False, why=f"{p} does not parse: {e}")


# ---------------------------------------------------------------- programs named by policy

def _toplevel(d):
    r = _git("rev-parse", "--show-toplevel", root=d)
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _inside(path, base):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(base)]) == \
            os.path.realpath(base)
    except ValueError:                       # different drives
        return False


def resolve_program(name, cwd=None):
    """(argv0, None) for a program a policy names, or (None, reason) when it must not run.

    A bare name (no slash) is looked up on PATH, and refused if PATH resolves it inside the
    untrusted tree. A relative path is anchored on $INTENT_TRUSTED_ROOT when that is set (the
    acceptance workflow sets it to the protected-branch checkout), else on the repository root.
    Whatever it resolves to is refused when it lies inside the untrusted working tree while a
    separate trusted root exists — which is exactly the CI arrangement.
    """
    if not name or not str(name).strip():
        return None, "no program is configured"
    name = str(name).strip()
    cwd = os.path.realpath(cwd or os.getcwd())
    untrusted = _toplevel(cwd) or cwd
    trusted_root = (os.environ.get("INTENT_TRUSTED_ROOT") or "").strip()
    trusted_root = os.path.realpath(trusted_root) if trusted_root else None
    separate = bool(trusted_root) and not _inside(trusted_root, untrusted) \
        and not _inside(untrusted, trusted_root)

    if os.sep not in name and "/" not in name:
        from shutil import which
        found = which(name)
        if not found:
            return None, f"{name} is not on PATH"
        if separate and _inside(found, untrusted):
            return None, (f"{name} resolves to {found}, inside the checkout under review — PATH "
                          f"often carries ./node_modules/.bin; refusing to run the branch's code")
        return found, None
    if os.path.isabs(name):
        cand = name
    else:
        base = trusted_root or untrusted
        cand = os.path.join(base, name)
    cand = os.path.realpath(cand)
    if separate and _inside(cand, untrusted):
        return None, (f"{name} resolves to {cand}, inside the checkout under review; a program the "
                      f"policy names must come from the trusted checkout ({trusted_root})")
    if not os.path.exists(cand):
        return None, f"{name} does not exist at {cand}"
    if not os.access(cand, os.X_OK):
        return None, f"{cand} is not executable"
    return cand, None


def resolve_argv(argv, cwd=None):
    """(argv, None) for a whole command line a policy names, or (None, reason).

    resolve_program covers argv[0] only, so `notify.command: python3 notifiers/n.py` resolved
    `python3` from PATH and then ran the PR's `notifiers/n.py`, with the token in the environment
    (reproduced in the 2.0.0 review). With a separate trusted root, every later argument that names
    a file is held to the same rule as the program: a relative path is taken from the TRUSTED
    checkout, and one that exists only in the tree under review — or an absolute path into it —
    refuses the whole command."""
    if not argv:
        return None, "no program is configured"
    prog, why = resolve_program(argv[0], cwd)
    if prog is None:
        return None, why
    cwd = os.path.realpath(cwd or os.getcwd())
    untrusted = _toplevel(cwd) or cwd
    trusted_root = (os.environ.get("INTENT_TRUSTED_ROOT") or "").strip()
    trusted_root = os.path.realpath(trusted_root) if trusted_root else None
    if not trusted_root or _inside(trusted_root, untrusted) or _inside(untrusted, trusted_root):
        return [prog] + list(argv[1:]), None
    out = [prog]
    for arg in argv[1:]:
        if arg.startswith("-") or ("/" not in arg and not os.path.exists(os.path.join(cwd, arg))):
            out.append(arg)
            continue
        real = os.path.realpath(arg if os.path.isabs(arg) else os.path.join(trusted_root, arg))
        if _inside(real, untrusted):
            return None, f"argument {arg} points into the checkout under review"
        if not os.path.isabs(arg) and not os.path.exists(real):
            return None, (f"argument {arg} is not in the trusted checkout ({trusted_root}); the copy "
                          f"under review is never used")
        out.append(real)
    return out, None


# ---------------------------------------------------------------- self-test

def _self_test():
    import tempfile, shutil
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    saved = {k: os.environ.get(k) for k in ("INTENT_TRUST_REF", "INTENT_TRUSTED_ROOT")}
    for k in saved:
        os.environ.pop(k, None)
    tmp = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "docs/intent"))
        g = lambda *a: _git(*a, root=repo)                       # noqa: E731
        g("init", "-q", ".")
        g("config", "user.email", "t@t"); g("config", "user.name", "t")
        g("config", "commit.gpgsign", "false")
        with open(os.path.join(repo, GOV), "w") as f:
            f.write("forge: {adapter: ./trusted.sh}\nmode: main\n")
        g("add", "-A"); g("commit", "-qm", "gov")

        # 1 · no trust ref at all: privileged load is EMPTY and untrusted — not the working tree.
        L = load(root=repo)
        check("no trust ref -> untrusted", L.trusted is False and L.data == {})
        check("no trust ref -> nothing from the working tree", L.get("forge") is None)
        check("the reason names the refs tried", "origin/main" in L.why)

        # 2 · local main is NOT a candidate: an agent can commit to it.
        check("local main is not trusted", "main" not in CANDIDATES and "HEAD" not in CANDIDATES)

        # 3 · origin/main carries it -> trusted, and a working-tree edit does not change the answer.
        g("update-ref", "refs/remotes/origin/main", "HEAD")
        with open(os.path.join(repo, GOV), "w") as f:
            f.write("forge: {adapter: ./evil.sh}\nmode: branch\n")
        L = load(root=repo)
        check("origin/main is trusted", L.trusted and L.ref == "refs/remotes/origin/main")
        check("the branch's edit is ignored", L.get("mode") == "main")
        W = load(root=repo, trusted=False)
        check("trusted=False reads the working tree, labelled", W.get("mode") == "branch"
              and W.source == "working-tree" and W.trusted is False)

        # 3b · a TAG or local BRANCH named `origin/main` must not be read as the trust ref. git's
        # DWIM resolves refs/tags/ and refs/heads/ before refs/remotes/ — the 2.0.0 review took over
        # the whole policy this way. The real origin/main still says mode: main.
        with open(os.path.join(repo, GOV), "w") as f:
            f.write("forge: {adapter: ./evil.sh}\nmode: tag\n")
        g("add", "-A"); g("commit", "-qm", "evil"); g("tag", "origin/main"); g("branch", "origin/master")
        g("reset", "-q", "--hard", "HEAD~1")
        L = load(root=repo)
        check("a tag named origin/main does not shadow the remote ref", L.get("mode") == "main")
        os.environ["INTENT_TRUST_REF"] = "origin/main"
        check("a short pin is qualified to refs/remotes/", candidates() == ("refs/remotes/origin/main",)
              and load(root=repo).get("mode") == "main")
        os.environ["INTENT_TRUST_REF"] = "main"
        check("an unqualifiable pin is no candidate at all", candidates() == ()
              and load(root=repo).trusted is False)
        os.environ.pop("INTENT_TRUST_REF")
        g("tag", "-d", "origin/main"); g("branch", "-D", "origin/master")

        # 4 · an explicit pin is the ONLY candidate; a pin that does not resolve is untrusted.
        os.environ["INTENT_TRUST_REF"] = "refs/does/not/exist"
        L = load(root=repo)
        check("an unresolvable pin is untrusted, not a fallback", L.trusted is False)
        os.environ.pop("INTENT_TRUST_REF")

        # 5 · the pinned ref is used only when no remote branch carries the file.
        g("update-ref", "-d", "refs/remotes/origin/main")
        g("update-ref", PINNED_REF, "HEAD")
        check("the pinned ref is the last resort", resolve_ref(root=repo) == PINNED_REF)

        # 6 · an unparseable policy is untrusted and empty.
        g("update-ref", "-d", PINNED_REF)
        with open(os.path.join(repo, GOV), "w") as f:
            f.write("forge: [unclosed\n")
        g("add", "-A"); g("commit", "-qm", "broken")
        g("update-ref", "refs/remotes/origin/main", "HEAD")
        L = load(root=repo)
        check("an unparseable policy is untrusted", L.trusted is False and L.data == {})

        # 7 · resolve_program: the CI arrangement — trusted checkout beside the PR checkout.
        trusted = os.path.join(tmp, "trusted")
        pr = os.path.join(tmp, "pr")
        for d in (trusted, pr):
            os.makedirs(os.path.join(d, ".intent"))
            _git("init", "-q", ".", root=d)
            p = os.path.join(d, ".intent", "adapter.sh")
            with open(p, "w") as f:
                f.write("#!/bin/sh\necho {}\n")
            os.chmod(p, 0o755)
        os.environ["INTENT_TRUSTED_ROOT"] = trusted
        prog, why = resolve_program(".intent/adapter.sh", cwd=pr)
        check("a relative program resolves into the TRUSTED checkout",
              prog == os.path.realpath(os.path.join(trusted, ".intent/adapter.sh")))
        prog, why = resolve_program(os.path.join(pr, ".intent/adapter.sh"), cwd=pr)
        check("an absolute path into the PR checkout is refused", prog is None and "under review" in why)
        prog, why = resolve_program("../pr/.intent/adapter.sh", cwd=pr)
        check("a relative escape into the PR checkout is refused", prog is None)
        # resolve_argv: an interpreter on PATH plus a SCRIPT argument — the script is held to the
        # same rule as a program (the review ran the PR's script this way, token in scope).
        for d in (trusted, pr):
            with open(os.path.join(d, ".intent", "n.py"), "w") as f:
                f.write("print('x')\n")
        av, why = resolve_argv(["python3", ".intent/n.py", "--flag"], cwd=pr)
        check("a script argument is taken from the TRUSTED checkout",
              av is not None and av[1] == os.path.realpath(os.path.join(trusted, ".intent/n.py"))
              and av[2] == "--flag")
        os.remove(os.path.join(trusted, ".intent", "n.py"))
        av, why = resolve_argv(["python3", ".intent/n.py"], cwd=pr)
        check("a script only the PR has is refused, never run from the PR", av is None)
        av, why = resolve_argv(["python3", os.path.join(pr, ".intent/n.py")], cwd=pr)
        check("an absolute script path into the PR is refused", av is None)
        os.environ.pop("INTENT_TRUSTED_ROOT")
        # NEGATIVE CONTROL: locally, with no separate trusted root, the repo's own program runs.
        prog, why = resolve_program(".intent/adapter.sh", cwd=pr)
        check("locally a repo program resolves", prog is not None, )
        prog, why = resolve_program("", cwd=pr)
        check("an empty program is refused", prog is None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[govcfg] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--where", action="store_true", help="which ref is trusted here")
    ap.add_argument("--show", action="store_true", help="print the trusted policy")
    ap.add_argument("--path", default=GOV)
    ap.add_argument("--root", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    L = load(a.path, root=a.root)
    if a.json:
        print(json.dumps({"source": L.source, "trusted": L.trusted, "ref": L.ref, "why": L.why,
                          "candidates": list(candidates()),
                          "data": L.data if a.show else None}, indent=2, default=str))
    elif a.show:
        import yaml
        print(f"# source: {L.source} · trusted: {L.trusted}" + (f" · {L.why}" if L.why else ""))
        print(yaml.safe_dump(L.data, sort_keys=False) if L.data else "# (empty)")
    else:
        print(f"[govcfg] {a.path}: " + (f"trusted, from {L.ref}" if L.trusted
                                         else f"UNTRUSTED — {L.why}"))
    return 0 if L.trusted else 1


if __name__ == "__main__":
    sys.exit(main())
