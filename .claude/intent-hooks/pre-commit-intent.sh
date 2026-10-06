#!/usr/bin/env bash
# Install as .git/hooks/pre-commit (or chain from an existing one).
# pre-commit framework users: add a local repo hook with entry: .claude/intent-hooks/pre-commit-intent.sh, language: script, pass_filenames: false
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
FILES=$(git diff --cached --name-only)
exec "$(git rev-parse --show-toplevel)/.claude/intent-hooks/commit_guard.sh" "$FILES"
