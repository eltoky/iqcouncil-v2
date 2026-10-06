#!/usr/bin/env python3
"""Run the intent smoke tests for given claim IDs, in any language the repository declares.

Usage: run_intent_tests.py [--manifest docs/intent/tests/INTENT_TESTS.yaml]
                           [--claims C-014,C-021] [--all] [--tripwires] [--differential]
                           [--allow-absent-runner]

Every runner comes from `docs/intent/coverage.yaml` (see `intent_coverage.py`), so adding a language is a
declaration rather than a code change. The shipped default claims twenty-odd languages including
`.sql` via dbt, `.R` via testthat, Go, Swift, Kotlin, Scala, Rust and notebooks.

WHAT CHANGED, AND WHY IT MATTERS
--------------------------------
This script used to dispatch on four hardcoded suffixes and return `NOT-RUNNABLE-HERE` for
everything else — **without setting `failed`**. Executed against the shipped build: a deliberately
failing Go unit test and a deliberately failing Swift NEG tripwire both reported
`NOT-RUNNABLE-HERE` and the process exited 0. Deleting both test files produced the same output and
the same exit code. A NEG tripwire is a BLOCK condition in the merge gate, so the gate read green
on a tripwire that had never run.

Three distinct states had been collapsed into one, and they are now separated:

  * **MISSING** — the manifest names a test file that does not exist. A gap, never a pass. This is
    a defect in the manifest and it fails.
  * **NO-RUNNER** — the file's language is not claimed by `coverage.yaml`, or is claimed with
    `tests: none`. By default this FAILS, because an undeclared language is the silent pass this
    rewrite exists to remove. `--allow-absent-runner` downgrades it to a reported skip for the one
    legitimate case: a declared language whose toolchain is absent from *this* runner, such as an
    iOS test on a Linux box. The downgrade is never silent — the summary names every skip.
  * **TOOL-ABSENT** — the runner is declared and the binary is not on PATH. Reported as a skip
    under `--allow-absent-runner`, a failure otherwise.

`NOT-RUNNABLE-HERE` is kept in the output vocabulary because the manifest schema uses it, but it is
now only ever produced for a deliberate, declared skip — never for an unknown.

EXIT: 0 every selected test ran and passed · 1 any failure, any missing file, or any unrunnable
test that was not explicitly allowed · 2 the manifest or coverage declaration is unusable.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import argparse, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run(cmd, timeout=1800):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as e:
        return False, f"could not execute {cmd[0]!r}: {e}"
    return r.returncode == 0, (r.stdout + r.stderr)[-800:]


def select(tests, want, all_, tripwires, differential):
    """The claims to run. A tier filter is additive to an explicit claim list."""
    sel = {}
    for cid, e in (tests or {}).items():
        if not isinstance(e, dict):
            continue
        tier = (e.get("tier") or "").upper()
        if (all_ or cid in want
                or (tripwires and tier == "TRIPWIRE")
                or (differential and tier in ("T5", "DIFFERENTIAL"))):
            sel[cid] = e
    return sel


def resolve(path, cfg, cov):
    """(state, argv_or_reason). State is 'run', 'missing', 'no-runner' or 'tool-absent'."""
    if not path:
        return "missing", "the manifest entry names no test file"
    if not os.path.exists(path):
        return "missing", f"{path} does not exist"
    tool, got = cov.runner_for(path, cfg)
    if tool is None:
        return "no-runner", got
    if not shutil.which(got[0]) and not os.path.exists(got[0]):
        return "tool-absent", f"runner {tool!r} needs {got[0]!r}, which is not on PATH"
    return "run", got


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default="docs/intent/tests/INTENT_TESTS.yaml")
    ap.add_argument("--root", default=".")
    ap.add_argument("--claims", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tripwires", action="store_true", help="also run all NEG tripwires")
    ap.add_argument("--differential", action="store_true",
                    help="also run all T5 DIFFERENTIAL tests (parity against a baseline)")
    ap.add_argument("--allow-absent-runner", action="store_true",
                    help="a DECLARED language whose toolchain is missing on this runner is a "
                         "reported skip rather than a failure — for a cross-platform build, never "
                         "to paper over an undeclared language")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        import yaml
    except ImportError:
        print("[intent-tests] pyyaml is required", file=sys.stderr)
        return 2
    try:
        m = yaml.safe_load(open(a.manifest)) or {}
    except Exception as e:
        print(f"[intent-tests] cannot read {a.manifest}: {e}", file=sys.stderr)
        return 2
    try:
        import intent_coverage as cov
        if getattr(cov, "MODULE_MARKER", None) != "intent-loop/coverage-denominator/1":
            raise ImportError("intent_coverage resolved to a module that is not "
                              "this suite's")
    except Exception as e:
        print(f"[intent-tests] the coverage declaration could not be loaded ({e}); without it no "
              f"runner can be resolved and nothing can be shown to have run", file=sys.stderr)
        return 2
    cfg, source = cov.load(a.root)
    probs = cov.validate(cfg)
    if probs:
        for p in probs[:6]:
            print(f"[intent-tests] coverage.yaml: {p}", file=sys.stderr)
        return 2

    sel = select(m.get("tests"), {c.strip() for c in a.claims.split(",") if c.strip()},
                 a.all, a.tripwires, a.differential)
    results, failed, skipped = {}, False, []
    for cid, e in sorted(sel.items()):
        state, got = resolve(e.get("test", ""), cfg, cov)
        if state == "run":
            ok, out = run(got)
            results[cid] = ("PASS", "") if ok else ("FAIL", out)
            failed = failed or not ok
            continue
        if state == "missing":
            # Never a pass. The manifest asserts a test exists; it does not.
            results[cid] = ("FAIL", f"test file missing — {got}")
            failed = True
            continue
        # no-runner / tool-absent
        if a.allow_absent_runner:
            results[cid] = ("NOT-RUNNABLE-HERE", got)
            skipped.append(f"{cid}: {got}")
        else:
            results[cid] = ("FAIL", f"{got}. Declare the language in docs/intent/coverage.yaml, or "
                                    f"pass --allow-absent-runner if this runner genuinely cannot "
                                    f"build it")
            failed = True

    for cid, (st, out) in sorted(results.items()):
        tier = (sel[cid].get("tier") or "?")
        print(f"{cid} [{tier}]: {st}")
        if st in ("FAIL", "NOT-RUNNABLE-HERE") and out:
            print("  " + out.replace("\n", "\n  ")[:600])
    if skipped:
        # A green run that skipped half the suite is the failure mode worth shouting about.
        print(f"[intent-tests] {len(skipped)} test(s) DID NOT RUN and were allowed to skip:")
        for s in skipped:
            print(f"    · {s}")
    print(f"[intent-tests] coverage from {source} · {len(results)} selected · "
          f"{sum(1 for _, (s, _) in results.items() if s == 'PASS')} passed · "
          f"{sum(1 for _, (s, _) in results.items() if s == 'FAIL')} failed · "
          f"{len(skipped)} not run")
    if a.json:
        print(json.dumps({c: s for c, (s, _) in results.items()}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
