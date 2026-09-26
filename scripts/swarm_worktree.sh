#!/usr/bin/env bash
# swarm_worktree.sh <repo> <commit> <dest>
# Create (or recreate) a detached git worktree of <repo> at <commit> in <dest>, for an isolated
# reviewer. Prints the worktree path. The reviewer never sees the implementer's live tree.
set -eu
repo=$1; commit=$2; dest=$3
mkdir -p "$(dirname "$dest")"
if [ -e "$dest" ]; then
  git -C "$repo" worktree remove --force "$dest"
fi
git -C "$repo" worktree prune
git -C "$repo" worktree add --detach "$dest" "$commit" >&2
readlink -f "$dest"
