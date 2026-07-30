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
ORG=robogeosociety
# Kept user-owned in the 2026-07 org migration; still mirrored as part of the fleet.
EXTRA_REPOS='tommyroar/tommyroar.github.io'
# obsidian = vault; the mini has a dedicated git-writer clone for it — don't touch here.
EXCLUDE_RE='^(obsidian)$'
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

slug_of() { printf '%s' "$1" | sed -E 's#\.git$##; s#/$##; s#.*/##'; }

# Scratch state: the "slug<TAB>dir" table of on-disk clones, plus a failure ledger.
# The repo loop below runs in a subshell (right-hand side of a pipe), so failures are
# recorded to a FILE — a shell variable would not survive back out to the parent.
tmp=$(mktemp); fails=$(mktemp)
trap 'rm -f "$tmp" "$fails"' EXIT

# Build the on-disk table. dotglob matters: bash's `*` never matches a leading dot, so
# dotted repos (.github — the very repo this fleet standardizes FROM) were invisible
# here, and every run misfiled them as missing and re-cloned onto a non-empty path.
# The `-d "$d/.git"` guard is what keeps dotglob safe: it drops .DS_Store-style junk
# and linked worktrees alike (in a worktree, .git is a file, not a directory). Bash
# excludes `.` and `..` from dotglob expansion, so those need no special-casing.
shopt -s dotglob
for d in "$DEV"/*/; do
  [ -d "$d/.git" ] || continue
  case "$(basename "$d")" in _by|*.wt|*.clone) continue;; esac
  u=$(git -C "$d" remote get-url origin 2>/dev/null) || continue
  printf '%s\t%s\n' "$(slug_of "$u")" "$d" >> "$tmp"
done
shopt -u dotglob

{ gh repo list "$ORG" --no-archived --source --limit 200 --json name -q '.[].name' | sed "s#^#$ORG/#"; printf '%s\n' $EXTRA_REPOS; } | while read -r full; do
  o=${full%%/*}; r=${full##*/}
  [[ "$r" =~ $EXCLUDE_RE ]] && { echo "skip  $r (excluded)"; continue; }
  dir=$(awk -F'\t' -v s="$r" '$1==s{print $2; exit}' "$tmp")
  if [ -n "$dir" ]; then
    echo "fetch $r  ($dir)"
    [ $APPLY = 1 ] && { git -C "$dir" fetch --all --prune -q \
      || { echo "  FETCH FAILED  $r"; printf 'fetch\t%s\n' "$r" >> "$fails"; }; }
  else
    echo "CLONE $r  -> $DEV/$r"
    [ $APPLY = 1 ] && { gh repo clone "$o/$r" "$DEV/$r" -- -q \
      || { echo "  CLONE FAILED  $r"; printf 'clone\t%s\n' "$r" >> "$fails"; }; }
  fi
  :
done || true

# Refresh the category overlay (_by/) so newly-synced repos get linked.
[ -x "$(dirname "$0")/categorize.sh" ] && DEV_ROOT="$DEV" "$(dirname "$0")/categorize.sh" || true

# A swallowed clone/fetch is exactly how the .github staleness hid behind a green step
# for weeks. Surface the roster and fail the run so CI shows red instead.
if [ -s "$fails" ]; then
  echo
  echo "repo-sync: $(wc -l < "$fails" | tr -d ' ') repo(s) failed to sync:"
  sed 's/^/  /' "$fails"
  exit 1
fi
