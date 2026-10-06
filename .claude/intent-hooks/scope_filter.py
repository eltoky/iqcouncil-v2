#!/usr/bin/env python3
"""Shared scope filter for the intent suite: enumerate/keep only Git-in-scope paths.
Excludes anything matched by .gitignore, .git/info/exclude, global excludesfile, .claudeignore,
.graphifyignore, and the .git/ dir itself. Git is the source of truth when available.

CLI:  scope_filter.py list   [REPO]           -> print in-scope files (one per line)
      scope_filter.py check  PATH [PATH...]   -> print each path + IN/OUT
Lib:  from scope_filter import in_scope_files, is_ignored
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, subprocess, fnmatch

def _git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30)
    except Exception:
        return None

def _has_git(repo):
    r = _git(repo, "rev-parse", "--is-inside-work-tree")
    return bool(r and r.returncode == 0 and r.stdout.strip() == "true")

def in_scope_files(repo="."):
    """Union of tracked + untracked-not-ignored, per Git. Falls back to a manual walk."""
    if _has_git(repo):
        tracked = _git(repo, "ls-files")
        others = _git(repo, "ls-files", "--others", "--exclude-standard")
        files = set()
        for r in (tracked, others):
            if r and r.returncode == 0:
                files.update(f for f in r.stdout.splitlines() if f)
        return sorted(files)
    # No Git: manual walk honoring .claudeignore + basic ignores
    return _manual_walk(repo)

_EXTRA_IGNORE = None
def _load_extra(repo):
    global _EXTRA_IGNORE
    if _EXTRA_IGNORE is not None: return _EXTRA_IGNORE
    pats = ["node_modules/", "dist/", "build/", ".venv/", "venv/", "target/", "__pycache__/", ".git/"]
    for f in (".claudeignore", ".graphifyignore", ".gitignore"):
        p = os.path.join(repo, f)
        if os.path.exists(p):
            pats += [ln.strip() for ln in open(p) if ln.strip() and not ln.startswith("#")]
    _EXTRA_IGNORE = pats
    return pats

def is_ignored(path, repo="."):
    """True if Git (or fallback rules) would ignore path."""
    if _has_git(repo):
        r = _git(repo, "check-ignore", "-q", path)
        if r is not None:
            if r.returncode == 0: return True
            if r.returncode == 1: return "/.git/" in ("/" + path.replace(os.sep, "/") + "/")
    rel = os.path.relpath(path, repo).replace(os.sep, "/")
    if rel.startswith(".git/") or rel == ".git": return True
    for pat in _load_extra(repo):
        pp = pat.rstrip("/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pp) or rel.startswith(pp + "/") or ("/" + pp + "/") in ("/" + rel + "/"):
            return True
    return False

def _manual_walk(repo):
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d), repo)]
        for f in files:
            p = os.path.join(root, f)
            if not is_ignored(p, repo):
                out.append(os.path.relpath(p, repo).replace(os.sep, "/"))
    return sorted(out)

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        repo = sys.argv[2] if len(sys.argv) > 2 else "."
        print("\n".join(in_scope_files(repo)))
    elif len(sys.argv) >= 3 and sys.argv[1] == "check":
        repo = "."
        for p in sys.argv[2:]:
            print(f"{'OUT' if is_ignored(p, repo) else 'IN '}  {p}")
    else:
        print(__doc__); sys.exit(2)
