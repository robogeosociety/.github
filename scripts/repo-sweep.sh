#!/usr/bin/env bash
# repo-sweep.sh — weekly disk sweep for the mini's /Volumes/dev mirror.
# Repos with no commit in >TTL_DAYS are converted to a BLOBLESS partial clone
# (repack --filter=blob:none) + gc --prune, reclaiming blob storage while keeping
# the working tree and full commit graph. Rehydrate anytime with:  git fetch --refetch
#
#   ./repo-sweep.sh            # DRY-RUN: show what would be slimmed
#   ./repo-sweep.sh --apply    # actually slim
#
# Safety: never slims a repo that is dirty, has unpushed commits, or has stashes
# (blob loss would be unrecoverable). Hard-pins repos with local-only state/secrets.
set -euo pipefail
DEV=/Volumes/dev
TTL_DAYS=${TTL_DAYS:-30}
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
NOW=$(date +%s)

# Local-only Terraform state+secrets (cloudflare-tfvend, infra) and the live-written
# vault (obsidian) must never be slimmed. Also honor a manual .git/KEEP marker.
PIN_RE='^(cloudflare-tfvend|infra|obsidian)$'

printf '%-30s %-8s %s\n' REPO STATE NOTE
for d in "$DEV"/*/; do
  r=$(basename "$d")
  case "$r" in *.wt|*.clone) continue;; esac
  [ -d "$d/.git" ] || continue

  if [[ "$r" =~ $PIN_RE ]] || [ -f "$d/.git/KEEP" ]; then
    printf '%-30s %-8s %s\n' "$r" PIN "local-state/vault/marker"; continue
  fi
  if [ "$(git -C "$d" worktree list 2>/dev/null | wc -l | tr -d ' ')" -gt 1 ]; then
    printf '%-30s %-8s %s\n' "$r" SKIP "has linked worktrees"; continue
  fi

  last=$(git -C "$d" for-each-ref --sort=-committerdate --format='%(committerdate:unix)' 2>/dev/null | head -1)
  [ -z "$last" ] && { printf '%-30s %-8s %s\n' "$r" SKIP "no commits"; continue; }
  age=$(( (NOW - last) / 86400 ))

  if [ "$(git -C "$d" config --get remote.origin.promisor 2>/dev/null)" = "true" ]; then
    printf '%-30s %-8s %s\n' "$r" DONE "${age}d, already blobless"; continue
  fi
  if [ "$age" -lt "$TTL_DAYS" ]; then
    printf '%-30s %-8s %s\n' "$r" ACTIVE "${age}d"; continue
  fi

  dirty=$(git -C "$d" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  unpushed=$(git -C "$d" log --branches --not --remotes --oneline 2>/dev/null | wc -l | tr -d ' ')
  stashes=$(git -C "$d" stash list 2>/dev/null | wc -l | tr -d ' ')
  if [ "$dirty" != 0 ] || [ "$unpushed" != 0 ] || [ "$stashes" != 0 ]; then
    printf '%-30s %-8s %s\n' "$r" HOLD "${age}d dirty=$dirty unpushed=$unpushed stash=$stashes"; continue
  fi

  before=$(du -sm "$d" 2>/dev/null | cut -f1)
  if [ $APPLY = 1 ]; then
    git -C "$d" config remote.origin.promisor true
    git -C "$d" config remote.origin.partialclonefilter blob:none
    git -C "$d" repack -ad --filter=blob:none 2>/dev/null || git -C "$d" gc --aggressive --prune=now >/dev/null 2>&1
    git -C "$d" gc --prune=now >/dev/null 2>&1 || true
    after=$(du -sm "$d" 2>/dev/null | cut -f1)
    printf '%-30s %-8s %s\n' "$r" SLIMMED "${age}d ${before}->${after}MB (-$((before-after))MB)"
  else
    printf '%-30s %-8s %s\n' "$r" "WOULD" "${age}d, ${before}MB -> blobless+gc"
  fi
done
