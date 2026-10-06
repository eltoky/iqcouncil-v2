#!/usr/bin/env python3
"""The coverage denominator: which tool claims which file, declared rather than assumed.

Why this exists
---------------
Five of the suite's measuring gates measured one language and reported the omission as **zero**:

  * `check_complexity.py` skipped every non-`.py` file and printed `0 functions on the security
    path`. Renaming one file to `.go` removed 22 functions — the project's own worst `F(64)` among
    them — and the ratchet said "no function got worse".
  * `run_intent_tests.py` dispatched on four suffixes and returned `NOT-RUNNABLE-HERE` for anything
    else, without setting `failed`. A failing Go test and a failing Swift NEG tripwire both exited
    0, and so did a manifest entry pointing at a file that does not exist.
  * `gen_digests.py` had a hardcoded `CODE_EXT` with no `.kt`, `.swift`, `.R`, `.ipynb`, `.scala`,
    `.tf`, `.sas` or `.dtsx`, and printed "none detected mechanically" for a repository full of
    them.
  * `_graph_present()` returned true if any file existed under the graph directory, so a graph over
    the Python half of a repo satisfied the blast-radius fence while the rest was invisible.
  * the merge gate treated an empty check list as green in the attended path.

Every one is the same defect, and it is the suite's own first rule broken by the tooling that
enforces that rule: **omission is not zero**. The fix is one declared denominator. A file class is
either claimed by a named tool, or declared unclaimable with a reason, or it is a finding. There is
no fourth state, and "silently skipped" is not one of the three.

The REASON is a `note:` beside the axis, and `missing_reasons()` below is what makes that
sentence true rather than aspirational: until it existed, `tests: none` with no explanation
validated cleanly, so "declared unclaimable with a reason" described a convention nobody
checked. It is reported by the initializer at setup time — when the operator can still act
on it — and as a note by the merge gate; it is deliberately NOT a blocking validate()
problem, because an upgrade must not refuse every declaration written before this existed.

The trust split, which matters
------------------------------
`coverage.yaml` declares two very different things and they are read by different readers:

  * **classification** — which extensions belong to which language, and which tool claims each
    measurement. Data. Read by the gates (`recheck_signoff`, `intent_status`), from the pinned
    trust ref where one exists, because a branch must not be able to declare its own files
    unmeasurable and thereby exempt itself.
  * **runner commands** — argv lists to execute. Privilege. Read ONLY by `run_intent_tests.py`,
    which already executes the repository's own test files, in the untrusted CI job. The gates
    never execute anything from this file, and `validate()` refuses a command that is not an argv
    list so there is no shell to inject into.

A repository with no `coverage.yaml` gets `DEFAULT` below, which claims twenty-odd languages. That
is deliberately generous: the point is not to withhold coverage, it is to make an unclaimed file
visible.

  intent_coverage.py --validate              is the declaration conformant (refuses a shell command)
  intent_coverage.py --files changed.txt     what measured these files, and what did not
  intent_coverage.py --self-test

PRIVILEGED FLAG, named here as the settings audit requires:

  --untrusted   Reads the WORKING-TREE copy of coverage.yaml instead of the pinned trust ref.
                The gates must never use it. The whole reason the classification is read from the
                trust ref is that a branch which can declare its own files unmeasurable can
                exempt itself from a gate — `languages: {mine: {extensions: ['.py'], complexity:
                none, tests: none, graph: none}}` on a feature branch would otherwise turn off
                every measurement for Python in that one pull request. It exists for authoring:
                editing the declaration and checking it before committing, when by definition the
                trust ref does not have your change yet.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import argparse, fnmatch, json, os, re, subprocess, sys

CFG = "docs/intent/coverage.yaml"
# The trust ref is resolved in ONE place, govcfg.resolve_ref(). Two different refs were
# both called "the trust ref" before 2.0.0, so routing floors could lag merged policy.

# `{test}` is the ONLY substitution. Anything else would be an interpolation surface in a file the
# branch under review controls.
PLACEHOLDER = "{test}"

# The shipped default. `complexity`, `tests` and `graph` name the tool that CLAIMS that
# measurement for that language, or "none" meaning "declared unmeasurable here" — which is an
# honest answer and a note, as opposed to an absent entry, which is a finding.
DEFAULT = {
    "version": 1,
    "languages": {
        "python":     {"extensions": [".py"], "complexity": "radon", "tests": "pytest",
                       "graph": "graphify"},
        "javascript": {"extensions": [".js", ".jsx", ".mjs", ".cjs"], "complexity": "none",
                       "tests": "jest", "graph": "graphify"},
        "typescript": {"extensions": [".ts", ".tsx"], "complexity": "none", "tests": "vitest",
                       "graph": "graphify"},
        "go":         {"extensions": [".go"], "complexity": "none", "tests": "gotest",
                       "graph": "none"},
        "rust":       {"extensions": [".rs"], "complexity": "none", "tests": "cargo",
                       "graph": "none"},
        "java":       {"extensions": [".java"], "complexity": "none", "tests": "maven",
                       "graph": "none"},
        "kotlin":     {"extensions": [".kt", ".kts"], "complexity": "none", "tests": "gradle",
                       "graph": "none"},
        "swift":      {"extensions": [".swift"], "complexity": "none", "tests": "swiftpm",
                       "graph": "none"},
        "objc":       {"extensions": [".m", ".mm", ".h"], "complexity": "none", "tests": "xcodebuild",
                       "graph": "none"},
        "csharp":     {"extensions": [".cs"], "complexity": "none", "tests": "dotnet",
                       "graph": "none"},
        "ruby":       {"extensions": [".rb"], "complexity": "none", "tests": "rspec",
                       "graph": "none"},
        "php":        {"extensions": [".php"], "complexity": "none", "tests": "phpunit",
                       "graph": "none"},
        "scala":      {"extensions": [".scala", ".sc"], "complexity": "none", "tests": "sbt",
                       "graph": "none"},
        "dart":       {"extensions": [".dart"], "complexity": "none", "tests": "flutter",
                       "graph": "none"},
        # Data work. `.sql` and `.R` were the two languages whose tests silently passed.
        "sql":        {"extensions": [".sql"], "complexity": "none", "tests": "dbt",
                       "graph": "none"},
        "r":          {"extensions": [".R", ".r", ".Rmd"], "complexity": "none",
                       "tests": "testthat", "graph": "none"},
        "notebook":   {"extensions": [".ipynb"], "complexity": "none", "tests": "nbval",
                       "graph": "none"},
        # The legacy SOURCES of a pipeline modernization. Claimed so they register as code at all;
        # declared unmeasurable because there is no runner for them, which is the honest state.
        "legacy_etl": {"extensions": [".sas", ".dtsx", ".ktr", ".kjb", ".xml.map"],
                       "complexity": "none", "tests": "none", "graph": "none",
                       "note": "migration source — no runner exists; parity is proved by a "
                               "DIFFERENTIAL test against it, not by running it"},
        "shell":      {"extensions": [".sh", ".bash", ".zsh"], "complexity": "none", "tests": "bats",
                       "graph": "none"},
        "infra":      {"extensions": [".tf", ".tfvars", ".hcl"], "complexity": "none",
                       "tests": "terraform", "graph": "none"},
        "platform":   {"extensions": [".plist", ".entitlements", ".pbxproj", ".xcconfig",
                                      ".storyboard", ".xib", ".gradle", ".csproj", ".podspec"],
                       "complexity": "none", "tests": "none", "graph": "none",
                       "note": "platform and build artifacts — no runner; fence them with "
                               "merge.unattended.arch_contract_paths instead"},
        "config":     {"extensions": [".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
                                      ".properties", ".env.example"],
                       "complexity": "none", "tests": "none", "graph": "none"},
        "docs":       {"extensions": [".md", ".rst", ".txt", ".adoc", ".drawio", ".svg", ".png",
                                      ".jpg", ".pdf", ".csv"],
                       "complexity": "none", "tests": "none", "graph": "none"},
    },
    # argv lists. `{test}` is substituted with the test path. No shell, ever.
    "runners": {
        "pytest":     {"command": ["pytest", PLACEHOLDER, "-q", "--no-header"]},
        "jest":       {"command": ["npx", "jest", PLACEHOLDER]},
        "vitest":     {"command": ["npx", "vitest", "run", PLACEHOLDER]},
        "playwright": {"command": ["npx", "playwright", "test", PLACEHOLDER]},
        "k6":         {"command": ["k6", "run", "--quiet", PLACEHOLDER]},
        "gotest":     {"command": ["go", "test", "-run", ".", PLACEHOLDER]},
        "cargo":      {"command": ["cargo", "test"]},
        "maven":      {"command": ["mvn", "-q", "-Dtest=" + PLACEHOLDER, "test"]},
        "gradle":     {"command": ["./gradlew", "test", "--tests", PLACEHOLDER]},
        "swiftpm":    {"command": ["swift", "test", "--filter", PLACEHOLDER]},
        "xcodebuild": {"command": ["xcodebuild", "test", "-scheme", PLACEHOLDER]},
        "dotnet":     {"command": ["dotnet", "test", "--filter", PLACEHOLDER]},
        "rspec":      {"command": ["bundle", "exec", "rspec", PLACEHOLDER]},
        "phpunit":    {"command": ["vendor/bin/phpunit", PLACEHOLDER]},
        "sbt":        {"command": ["sbt", "testOnly " + PLACEHOLDER]},
        "flutter":    {"command": ["flutter", "test", PLACEHOLDER]},
        "dbt":        {"command": ["dbt", "test", "--select", PLACEHOLDER]},
        "testthat":   {"command": ["Rscript", "-e",
                                   "testthat::test_file('" + PLACEHOLDER + "')"]},
        "nbval":      {"command": ["pytest", "--nbval-lax", PLACEHOLDER]},
        "bats":       {"command": ["bats", PLACEHOLDER]},
        "terraform":  {"command": ["terraform", "test", "-filter=" + PLACEHOLDER]},
        # A differential test compares two systems, so its command takes the claim's baseline too.
        # `{test}` is the harness; the harness reads the claim's `baseline` and `input_domain`.
        "differential": {"command": ["pytest", PLACEHOLDER, "-q", "--no-header"]},
    },
}


def sh(*a, cwd=None):
    return subprocess.run(a, capture_output=True, text=True, cwd=cwd)


# ---------------------------------------------------------------- loading

def load(root=".", trusted=True):
    """(cfg, source) through govcfg. `trusted` reads ONLY the trust ref: a branch must not be able
    to declare its own files unmeasurable and exempt itself from a gate. With no trusted copy the
    BUILT-IN declaration applies — never the working tree. Before 2.0.0 this read a locally pinned
    ref that does not exist on a CI clone, so in CI it silently used the PR's own copy.
    trusted=False reads the working tree, for authoring and validation only."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    if trusted:
        text, ref = govcfg.read_trusted(CFG, root=root)
        if text is not None:
            cfg = _parse(text)
            if cfg is not None:
                return cfg, ref
        return DEFAULT, "default"
    p = os.path.join(root, CFG)
    if os.path.exists(p):
        try:
            cfg = _parse(open(p).read())
        except OSError:
            cfg = None
        if cfg is not None:
            return cfg, "working-tree"
    return DEFAULT, "default"


def _parse(text):
    try:
        import yaml
        d = yaml.safe_load(text)
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("languages") else None


# An unmistakable marker, so a caller can tell OUR module from somebody else's. This module was
# named `coverage.py` until 1.22.0, which shadows `coverage` — one of the most widely installed
# packages in Python (pytest-cov depends on it). On any machine that had it, `import coverage` in
# the merge gate and in the initializer resolved to the PyPI package, and the first call into it
# raised AttributeError. Reproduced by installing it: the initializer tracebacked. The rename is
# the real fix; this marker is the belt, because the next collision will be with something else.
MODULE_MARKER = "intent-loop/coverage-denominator/1"


def is_ours(mod):
    """True only for THIS module. Rule 1: a module we cannot identify is not a module we trust.

    Callers must NOT reach this through the module they are trying to identify — an impostor has
    no `is_ours` at all. They compare the marker themselves; this exists for this module's tests.
    """
    return getattr(mod, "MODULE_MARKER", None) == MODULE_MARKER


def missing_reasons(cfg):
    """Languages that declare an axis unmeasurable without saying why, as lines.

    `complexity: none` is an answer; `complexity: none` with no reason is the same silence the
    denominator exists to remove, one level up. Separate from validate() on purpose: this is
    advisory, so a declaration written before this check existed still loads.
    """
    out = []
    for name, spec in (cfg.get("languages") or {}).items():
        if not isinstance(spec, dict):
            continue
        if (spec.get("note") or "").strip():
            continue
        bare = [a for a in ("complexity", "tests", "graph") if spec.get(a) == "none"]
        if bare:
            out.append(f"languages.{name}: {', '.join(bare)} declared unmeasurable with no "
                       f"note: saying why")
    return out


def validate(cfg):
    """Problems with a coverage file, as lines. Empty means conformant.

    The security-relevant rules are the last two: a command must be an argv LIST, so there is no
    shell, and the only substitution is `{test}`.
    """
    p = []
    langs = cfg.get("languages")
    if not isinstance(langs, dict) or not langs:
        return ["languages: required, and must be a mapping of name -> {extensions, ...}"]
    runners = cfg.get("runners") or {}
    seen = {}
    for name, spec in langs.items():
        if not isinstance(spec, dict):
            p.append(f"languages.{name}: not a mapping"); continue
        exts = spec.get("extensions")
        if not isinstance(exts, list) or not exts:
            p.append(f"languages.{name}.extensions: required, a non-empty list")
            continue
        for e in exts:
            if not isinstance(e, str) or not e.startswith("."):
                p.append(f"languages.{name}.extensions: {e!r} is not an extension like '.py'")
            elif e.lower() in seen and seen[e.lower()] != name:
                p.append(f"extension {e} is claimed by both {seen[e.lower()]} and {name}; one "
                         f"owner per extension or the verdict depends on dict order")
            else:
                seen[e.lower()] = name
        for axis in ("complexity", "tests", "graph"):
            tool = spec.get(axis)
            if tool is None:
                p.append(f"languages.{name}.{axis}: required — name the tool, or 'none' to declare "
                         f"it unmeasurable. An absent entry is the silent skip this file exists to "
                         f"remove")
            elif axis == "tests" and tool != "none" and tool not in runners:
                p.append(f"languages.{name}.tests names runner {tool!r}, which runners does not "
                         f"define — no test for this language could ever run")
    for name, spec in (runners or {}).items():
        cmd = (spec or {}).get("command") if isinstance(spec, dict) else None
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            p.append(f"runners.{name}.command: must be a list of strings (argv). A string would "
                     f"be a shell, and this file is controlled by the branch under review")
            continue
        for part in cmd:
            for m in re.findall(r"\{([a-z_]*)\}", part):
                if m != "test":
                    p.append(f"runners.{name}.command: unknown substitution {{{m}}}; only "
                             f"{{test}} is substituted")
    return p


# ---------------------------------------------------------------- classification (pure)

def _ext(path):
    """The longest declared-looking extension, so `.env.example` and `.xml.map` work."""
    base = os.path.basename(path)
    if "." not in base:
        return ""
    parts = base.split(".")
    if len(parts) >= 3:
        two = "." + ".".join(parts[-2:])
        return two
    return "." + parts[-1]


def language_of(path, cfg):
    """(language name, spec) or (None, None). Two-part extensions win over one-part."""
    langs = cfg.get("languages") or {}
    two = _ext(path).lower()
    one = ("." + path.rsplit(".", 1)[-1]).lower() if "." in os.path.basename(path) else ""
    for want in (two, one):
        if not want:
            continue
        for name, spec in langs.items():
            if not isinstance(spec, dict):
                continue
            if want in [str(e).lower() for e in (spec.get("extensions") or [])]:
                return name, spec
    return None, None


def classify(files, cfg, ignore=()):
    """{by_language, unclaimed, measured} for a list of paths. Pure, so it is testable."""
    by_language, unclaimed = {}, []
    for f in files or []:
        if not f or any(fnmatch.fnmatch(f, g) for g in ignore):
            continue
        name, spec = language_of(f, cfg)
        if name is None:
            unclaimed.append(f)
            continue
        by_language.setdefault(name, []).append(f)
    measured = {axis: sorted(n for n in by_language
                             if ((cfg.get("languages") or {}).get(n) or {}).get(axis, "none")
                             not in ("none", None))
                for axis in ("complexity", "tests", "graph")}
    return {"by_language": by_language, "unclaimed": sorted(unclaimed), "measured": measured}


def assess(files, cfg, autonomous=False, ignore=()):
    """(findings, notes) for the files in a change.

    An UNCLAIMED file is a finding: no tool measures it and nobody said so, which is the state that
    produced five silent passes. A file whose language is claimed but declares `tests: none` is a
    NOTE under a human and a FINDING under an autonomous mode — the same asymmetry the rest of the
    suite uses, because a human merging can see an unmeasured language and nobody is looking under
    hotl/hootl.
    """
    c = classify(files, cfg, ignore)
    findings, notes = [], []
    if c["unclaimed"]:
        shown = ", ".join(c["unclaimed"][:6]) + (" …" if len(c["unclaimed"]) > 6 else "")
        findings.append(("COVERAGE-UNKNOWN",
                         f"{len(c['unclaimed'])} changed file(s) belong to no language this "
                         f"repository has declared, so no gate measured them: {shown}. Declare "
                         f"them in {CFG} — with 'none' if nothing can measure them, which is an "
                         f"answer; silence is not"))
    langs = cfg.get("languages") or {}
    for axis, label in (("tests", "no test runner"), ("complexity", "no complexity measurement")):
        bare = sorted(n for n in c["by_language"]
                      if (langs.get(n) or {}).get(axis, "none") in ("none", None)
                      and n not in ("docs", "config"))
        if not bare:
            continue
        line = (f"{label} is declared for: {', '.join(bare)} — changes in those languages are "
                f"unmeasured on this axis by declaration")
        (findings if autonomous else notes).append((f"COVERAGE-{axis.upper()}-NONE", line))
    return findings, notes


# ---------------------------------------------------------------- runners (privileged read)

def runner_for(path, cfg):
    """(name, argv) for a test path, or (None, reason). Only the test job calls this."""
    name, spec = language_of(path, cfg)
    if name is None:
        return None, (f"no language in {CFG} claims {os.path.basename(path)} — declare its "
                      f"extension, with tests: none if nothing can run it")
    tool = (spec or {}).get("tests") or "none"
    if tool == "none":
        return None, (f"{name} declares tests: none, so this test cannot run here. That is a "
                      f"declared gap, not a pass")
    runner = (cfg.get("runners") or {}).get(tool) or {}
    cmd = runner.get("command")
    if not isinstance(cmd, list) or not cmd:
        return None, f"runner {tool!r} for {name} has no argv command"
    return tool, [part.replace(PLACEHOLDER, path) for part in cmd]


# ---------------------------------------------------------------- self-test

def _self_test():
    ok = fail = 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL {label}")

    check("the default map validates", validate(DEFAULT) == [])
    # Every language that names a runner must have that runner defined — the check that would have
    # caught `.sql` and `.R` naming nothing.
    for lang in ("sql", "r", "go", "swift", "kotlin", "notebook", "scala"):
        check(f"{lang} is claimed by default", lang in DEFAULT["languages"])
    check("sql names a real runner", runner_for("models/fct.sql", DEFAULT)[0] == "dbt")
    check("R names a real runner", runner_for("tests/test_x.R", DEFAULT)[0] == "testthat")
    check("go names a real runner", runner_for("x_test.go", DEFAULT)[0] == "gotest")
    check("swift names a real runner", runner_for("XTests.swift", DEFAULT)[0] == "swiftpm")
    check("the runner argv carries the test path",
          "models/fct.sql" in (runner_for("models/fct.sql", DEFAULT)[1] or []))

    # An unclaimed extension is a finding, not a skip.
    f, n = assess(["src/thing.zig"], DEFAULT)
    check("an unclaimed extension is a finding", "COVERAGE-UNKNOWN" in {c for c, _ in f})
    f, n = assess(["src/a.py"], DEFAULT)
    check("a claimed, measured language is clean", not f)

    # A declared `tests: none` language is a note under a human, a finding under autonomy.
    f1, n1 = assess(["ios/App.plist"], DEFAULT, autonomous=False)
    f2, n2 = assess(["ios/App.plist"], DEFAULT, autonomous=True)
    check("tests:none is a note under hitl", "COVERAGE-TESTS-NONE" in {c for c, _ in n1})
    check("tests:none is a finding under autonomy", "COVERAGE-TESTS-NONE" in {c for c, _ in f2})
    # docs and config are exempt from that nag: nobody expects a unit test for a .md file.
    f3, n3 = assess(["README.md", "config/app.yml"], DEFAULT, autonomous=True)
    check("docs and config do not nag about tests", not f3)

    # The legacy sources of a pipeline modernization register as code with an honest reason.
    name, spec = language_of("etl/legacy/load_customers.sas", DEFAULT)
    check(".sas is claimed", name == "legacy_etl")
    check(".sas declares why it has no runner", "note" in (spec or {}))
    check(".dtsx is claimed", language_of("etl/pkg.dtsx", DEFAULT)[0] == "legacy_etl")

    # Two-part extensions.
    check("a two-part extension resolves", language_of("x/.env.example", DEFAULT)[0] == "config")
    check("a one-part extension still resolves", language_of("a/b/c.py", DEFAULT)[0] == "python")
    check("a file with no extension is unclaimed", language_of("Makefile", DEFAULT)[0] is None)
    check("case is ignored", language_of("X.PY", DEFAULT)[0] == "python")

    # validate() refuses the dangerous shapes.
    bad = {"languages": {"x": {"extensions": [".x"], "complexity": "none", "tests": "sh",
                               "graph": "none"}},
           "runners": {"sh": {"command": "rm -rf /"}}}
    probs = validate(bad)
    check("a string command is refused", any("argv" in p for p in probs))
    bad2 = {"languages": {"x": {"extensions": [".x"], "complexity": "none", "tests": "r1",
                                "graph": "none"}},
            "runners": {"r1": {"command": ["echo", "{evil}"]}}}
    check("an unknown substitution is refused",
          any("only {test}" in p for p in validate(bad2)))
    bad3 = {"languages": {"x": {"extensions": [".x"], "complexity": "none", "tests": "nope",
                                "graph": "none"}}, "runners": {}}
    check("a language naming an undefined runner is refused",
          any("runners does not" in p for p in validate(bad3)))
    bad4 = {"languages": {"x": {"extensions": [".x"], "tests": "none", "graph": "none"}},
            "runners": {}}
    check("a missing axis is refused", any("complexity" in p for p in validate(bad4)))
    bad5 = {"languages": {"a": {"extensions": [".q"], "complexity": "none", "tests": "none",
                                "graph": "none"},
                          "b": {"extensions": [".q"], "complexity": "none", "tests": "none",
                                "graph": "none"}}, "runners": {}}
    check("two languages claiming one extension is refused",
          any("claimed by both" in p for p in validate(bad5)))

    # classify() groups and reports measured axes.
    c = classify(["a.py", "b.go", "c.sql", "d.zig"], DEFAULT)
    check("classify groups by language", set(c["by_language"]) == {"python", "go", "sql"})
    check("classify reports the unclaimed", c["unclaimed"] == ["d.zig"])
    check("complexity is measured only for python here", c["measured"]["complexity"] == ["python"])
    check("tests are measured for all three", c["measured"]["tests"] == ["go", "python", "sql"])
    check("ignore globs are honoured",
          classify(["vendor/x.py"], DEFAULT, ignore=("vendor/*",))["by_language"] == {})

    # missing_reasons: the check that makes "unclaimable WITH A REASON" true.
    check("a bare 'none' with no note is reported",
          any("zz" in m for m in missing_reasons(
              {"languages": {"zz": {"extensions": [".zz"], "complexity": "none",
                                    "tests": "none", "graph": "none"}}})))
    check("a note satisfies it",
          not missing_reasons({"languages": {"zz": {"extensions": [".zz"], "complexity": "none",
                                                    "tests": "none", "graph": "none",
                                                    "note": "generated; nothing measures it"}}}))
    check("a measured language is never reported",
          not missing_reasons({"languages": {"py": {"extensions": [".py"], "complexity": "radon",
                                                    "tests": "pytest", "graph": "crg"}}}))
    # NEGATIVE CONTROL: it must stay advisory, never a validate() problem, or an upgrade would
    # refuse every declaration written before this check existed.
    check("a missing note does not break validation",
          not validate({"languages": {"zz": {"extensions": [".zz"], "complexity": "none",
                                             "tests": "none", "graph": "none"}}}))
    print(f"[coverage] self-test: {ok} passed, {fail} failed")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--files", help="file with one changed path per line")
    ap.add_argument("--autonomous", action="store_true")
    ap.add_argument("--untrusted", action="store_true",
                    help="read the working-tree copy, not the pinned trust ref")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    cfg, source = load(a.root, trusted=not a.untrusted)
    if a.validate:
        probs = validate(cfg)
        for p in probs:
            print(f"[coverage] {p}")
        print(f"[coverage] {CFG} from {source}: "
              f"{'conformant' if not probs else str(len(probs)) + ' problem(s)'}")
        return 1 if probs else 0
    files = []
    if a.files:
        try:
            files = [l.strip() for l in open(a.files) if l.strip()]
        except OSError as e:
            print(f"[coverage] cannot read {a.files}: {e}", file=sys.stderr)
            return 2
    findings, notes = assess(files, cfg, autonomous=a.autonomous)
    if a.json:
        print(json.dumps({"source": source, "findings": findings, "notes": notes,
                          "classified": classify(files, cfg)}, indent=2))
        return 1 if findings else 0
    c = classify(files, cfg)
    print(f"[coverage] {CFG} from {source} · {len(c['by_language'])} language(s) in this change")
    for code, detail in findings:
        print(f"  BLOCKING  {code}: {detail}")
    for code, detail in notes:
        print(f"  note      {code}: {detail}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
