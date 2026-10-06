#!/usr/bin/env bash
# Per-change git worktrees. Isolation of checkouts — NOT coordination (see SKILL.md).
# Usage: worktree.sh add <change-id> | prune <change-id> | list
SUITE_VERSION="2.2.1"   # intent-loop-suite release — stamped by tools/stamp_version.py
set -uo pipefail
CFG="docs/intent/concurrency.config"
# `tr -d " "` deleted interior spaces too, so WORKTREE_ROOT="/Users/Frank Bignone/wt" silently
# became "/Users/FrankBignone/wt" — a directory the user never configured, reported as success.
get(){ v=$(grep -m1 "^$1=" "$CFG" 2>/dev/null \
           | sed 's/^[^=]*=//; s/[[:space:]]*#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//'); echo "${v:-$2}"; }
ROOT=$(get WORKTREE_ROOT ..); AUTO=$(get WORKTREE_AUTO true)
REPO=$(git rev-parse --show-toplevel)
case "${1:-}" in
  add)
    [ "$AUTO" = "true" ] || { echo "[worktree] WORKTREE_AUTO=false — skipping"; exit 0; }
    ID="${2:?change-id required}"; WT="$ROOT/wt-$ID"
    [ -d "$WT" ] && { echo "[worktree] exists: $WT"; exit 0; }
    git worktree add "$WT" -b "change/$ID" 2>/dev/null || git worktree add "$WT" "change/$ID"
    # hooks and skills must fire in the new checkout
    [ -d "$REPO/.claude" ] && ln -sfn "$REPO/.claude" "$WT/.claude"
    echo "[worktree] $WT on branch change/$ID (.claude linked)"
    echo "[worktree] note: CRG/Graphify caches are per-checkout — point them at a shared cache dir or expect a rebuild"
    ;;
  prune)
    ID="${2:?change-id required}"; WT="$ROOT/wt-$ID"
    git worktree remove "$WT" --force 2>/dev/null && echo "[worktree] removed $WT" || echo "[worktree] nothing to remove at $WT"
    git worktree prune
    ;;
  list) git worktree list ;;
  *) echo "usage: worktree.sh add|prune <change-id> | list"; exit 2 ;;
esac
