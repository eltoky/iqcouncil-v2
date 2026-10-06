#!/usr/bin/env python3
"""Run a notifier — the ONE implementation of calling an `intent-notice/1` program.

Used by notify.py (the human-on-the-loop merge notice) and intent_notify.py (the waiting-on-a-person
notices). Two copies of this would be two rules for what "delivered" means.

  in     a JSON object on the notifier's STDIN
  out    a non-empty delivery token on its STDOUT, exit 0
  fail   any non-zero exit, empty output, or no answer within the timeout

The command comes from TRUSTED policy only, is parsed with no shell, and every file it names —
program and arguments alike — is resolved by govcfg.resolve_argv against the trusted checkout.
Nothing from a pull request is ever placed in argv: it travels on stdin, as data.
"""
__suite_version__ = "2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
import os, sys, json, shlex, subprocess

MAX_TOKEN = 400

def notifier_argv(cmd):
    """(argv, why_not). Parsed with no shell, and the program resolved against the TRUSTED
    checkout, never the PR's — the same rule as the forge adapter, from the same function."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return None, f"notify.command could not be parsed as a command line: {e}"
    if not argv:
        return None, "notify.command is empty"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import govcfg
    # The WHOLE command line, not only argv[0]: `python3 notifiers/n.py` used to run the PR's copy
    # of the script with the token in scope. resolve_argv holds every file argument to the
    # program's rule.
    resolved, why = govcfg.resolve_argv(argv)
    if resolved is None:
        return None, f"the notifier was refused: {why}"
    return resolved, ""


def deliver(cmd, data, timeout):
    """Run the notifier. (ok, token, why_not).

    No shell, argv from the trusted configuration only, the request's own text on stdin as JSON.
    Exit 0 with empty output is NOT success — a notifier that returns no token has not shown that
    anything was delivered, and that is precisely the failure mode this module exists to catch.
    """
    argv, why = notifier_argv(cmd)
    if argv is None:
        return False, "", why
    try:
        p = subprocess.run(argv, input=json.dumps(data), capture_output=True, text=True,
                           timeout=timeout, shell=False)
    except FileNotFoundError:
        return False, "", f"the notifier does not exist: {argv[0]}"
    except PermissionError:
        return False, "", f"the notifier is not executable: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"the notifier did not answer within {timeout}s"
    except OSError as e:
        return False, "", f"the notifier could not be run: {e}"
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip().splitlines()
        return False, "", (f"the notifier exited {p.returncode}: "
                           f"{err[0][:200] if err else 'no output'}")
    token = (p.stdout or "").strip().splitlines()
    token = token[0].strip() if token else ""
    if not token:
        return False, "", ("the notifier exited 0 but printed no delivery token. Exit 0 alone is "
                           "not delivery — a notifier that returns nothing has not been shown to "
                           "have sent anything.")
    return True, token[:MAX_TOKEN], ""


