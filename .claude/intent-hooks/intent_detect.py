#!/usr/bin/env python3
"""Single source of truth for "is X installed".

Why this file exists: check_prerequisites.py and detect_capabilities.py each grew their own
detection logic, and they drifted. detect_capabilities.py looked ONLY at repo-local
`<repo>/.claude/skills/<name>`, so every globally installed PLUGIN — the four review lanes and
Superpowers — was reported missing on a machine where they were all present. The prerequisites
check said 25/25 and 5/5; the capability detector said 1/5 and wrote that into CLAUDE.md, which
then told every future session the loop was degraded. Two implementations of one question is the
defect; one importable module is the fix.

Three things live in three different places, and conflating them is the whole bug:
  plugins        -> ~/.claude/plugins/            (installed_plugins.json, marketplaces/, cache/)
  skills         -> ~/.claude/skills/<name>/SKILL.md, and ~/.agents/skills/<name> for the
                    universal `skills` CLI, which symlinks into ~/.claude/skills
  MCP servers    -> ~/.claude.json  (mcpServers), written by `claude mcp add`
Plus a fourth, separate question: is the TOOL on PATH (a CLI), which is not the same as whether
this repository has been initialised to use it.

Run directly for a quick dump:  python3 intent_detect.py [repo] [name ...]
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, json, glob, shutil, subprocess

HOME = os.path.expanduser("~")


def skill_dirs(repo="."):
    """Every place a bare skill can legitimately live, global first.

    ~/.agents/skills is the universal store used by `npx skills add <repo> -g`; it symlinks the
    skill into ~/.claude/skills. A user who cloned by hand has only the ~/.claude path, and a
    user whose harness the CLI does not support may have only the ~/.agents path, so both count.
    """
    return [os.path.join(HOME, ".claude", "skills"),
            os.path.join(HOME, ".agents", "skills"),
            os.path.join(repo, ".claude", "skills"),
            os.path.join(repo, ".agents", "skills")]


def skill_present(name, dirs=None, repo="."):
    """True when <dir>/<name>/SKILL.md resolves. isfile() follows symlinks, which is what the
    `skills` CLI creates — so a DANGLING symlink is correctly reported absent rather than as a
    healthy install."""
    for d in (dirs or skill_dirs(repo)):
        if os.path.isfile(os.path.join(d, name, "SKILL.md")):
            return True
    return False


def plugin_present(name):
    """Claude Code keeps PLUGINS under ~/.claude/plugins, never under ~/.claude/skills."""
    base = os.path.join(HOME, ".claude", "plugins")
    reg = os.path.join(base, "installed_plugins.json")
    try:
        blob = json.dumps(json.load(open(reg)))
        for key in (f'"{name}@', f'"{name}"'):
            if key in blob:
                return True
    except Exception:
        pass
    # a plugin can be on disk while the registry is out of step (upstream bug 51806)
    return bool(glob.glob(os.path.join(base, "cache", "*", name))
                or glob.glob(os.path.join(base, "marketplaces", "*", name))
                or glob.glob(os.path.join(base, "marketplaces", "*", "plugins", name)))


def mcp_present(name):
    """MCP servers live in ~/.claude.json (user scope) — what `claude mcp add` writes."""
    for p in (os.path.join(HOME, ".claude.json"), os.path.join(HOME, ".claude", "settings.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        servers = d.get("mcpServers") or {}
        if name in servers or name in json.dumps(servers):
            return True
    return False


def cli_present(name):
    """On PATH. Separate question from 'initialised in this repo' — do not conflate them."""
    return shutil.which(name) is not None


def npm_global(pkg):
    try:
        r = subprocess.run(["npm", "ls", "-g", pkg, "--depth=0"],
                           capture_output=True, text=True, timeout=20)
        return pkg in r.stdout
    except Exception:
        return False


def py_module(mod):
    try:
        subprocess.run([sys.executable, "-c", f"import {mod}"], capture_output=True, timeout=20, check=True)
        return True
    except Exception:
        return False


def have(name, repo=".", dirs=None):
    """Installed as a plugin OR as a bare skill — either counts. This is the function the
    callers want in almost every case."""
    return plugin_present(name) or skill_present(name, dirs, repo)


def report(repo=".", names=()):
    out = {}
    for n in names:
        out[n] = {"plugin": plugin_present(n), "skill": skill_present(n, repo=repo),
                  "cli": cli_present(n), "mcp": mcp_present(n)}
    return out


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    names = sys.argv[2:] or ["code-review", "security-guidance", "pr-review-toolkit",
                             "comprehensive-review", "superpowers", "ponytail",
                             "impeccable", "drawio-skill"]
    print(f"repo={os.path.abspath(repo)}")
    print("skill dirs searched:")
    for d in skill_dirs(repo):
        print(f"  {'[x]' if os.path.isdir(d) else '[ ]'} {d}")
    for n, v in report(repo, names).items():
        marks = ",".join(k for k, hit in v.items() if hit) or "-"
        print(f"  {'OK ' if (v['plugin'] or v['skill']) else '-- '} {n:24s} ({marks})")
