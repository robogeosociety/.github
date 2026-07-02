#!/usr/bin/env bash
# repo-sync.sh — keep the mini's /Volumes/dev a complete mirror of all owned repos.
# Matches existing clones by origin URL (mini dir names don't always equal the repo
# name, e.g. transit_tracker vs transit-tracker-tui), so it never duplicates.
# bash 3.2 safe (no associative arrays). Dry-run default; --apply to clone/fetch.
#
#   ./repo-sync.sh            # DRY-RUN: show what would be cloned/fetched
#   ./repo-sync.sh --apply    # clone missing, fetch present
set -euo pipefail
DEV=/Volumes/dev
OWNER=tommyroar
# obsidian = vault; the mini has a dedicated git-writer clone for it — don't touch here.
EXCLUDE_RE='^(obsidian)$'
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

slug_of() { printf '%s' "$1" | sed -E 's#\.git$##; s#/$##; s#.*/##'; }

# Build "slug<TAB>dir" table of what's already on disk.
tmp=$(mktemp)
for d in "$DEV"/*/; do
  [ -d "$d/.git" ] || continue
  case "$(basename "$d")" in *.wt|*.clone) continue;; esac
  u=$(git -C "$d" remote get-url origin 2>/dev/null) || continue
  printf '%s\t%s\n' "$(slug_of "$u")" "$d" >> "$tmp"
done

gh repo list "$OWNER" --no-archived --source --limit 200 --json name -q '.[].name' | while read -r r; do
  [[ "$r" =~ $EXCLUDE_RE ]] && { echo "skip  $r (excluded)"; continue; }
  dir=$(awk -F'\t' -v s="$r" '$1==s{print $2; exit}' "$tmp")
  if [ -n "$dir" ]; then
    echo "fetch $r  ($dir)"
    [ $APPLY = 1 ] && { git -C "$dir" fetch --all --prune -q || echo "  fetch failed"; }
  else
    echo "CLONE $r  -> $DEV/$r"
    [ $APPLY = 1 ] && { gh repo clone "$OWNER/$r" "$DEV/$r" -- -q || echo "  clone failed"; }
  fi
  :
done || true
rm -f "$tmp"
