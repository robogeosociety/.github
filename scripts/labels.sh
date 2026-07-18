#!/usr/bin/env bash
#
# labels.sh — reconcile every robogeosociety repo to the canonical label taxonomy
# in scripts/labels.tsv. Source of truth: robogeosociety/.github.
#
#   ./scripts/labels.sh                 # DRY-RUN (read-only): print the per-repo delta
#   ./scripts/labels.sh --apply         # create missing / fix drifted labels
#   ./scripts/labels.sh --apply --prune-stock   # also delete the 9 untouched GitHub defaults
#
# Idempotent and additive: it creates canonical labels that are missing and edits
# ones whose color/description drifted, but never deletes a repo's bespoke labels.
# --prune-stock additionally removes the nine default GitHub labels (bug, duplicate,
# wontfix, …) — but ONLY when they still carry their stock color+description, so a
# repo that repurposed "bug" keeps it. bash 3.2 safe (no associative arrays).
#
# Pilot one repo:  REPO_ONLY=supervisor ./scripts/labels.sh --apply
set -euo pipefail

ORG=robogeosociety
# Kept user-owned in the 2026-07 org migration; still part of the standardized fleet.
EXTRA_REPOS='tommyroar/tommyroar.github.io'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT/scripts/labels.tsv"

# obsidian = vault data; the mini is its sole git writer. Labels are API state, not
# git, so it's safe to reconcile, but we keep it out of the loop for consistency
# with sync.sh / repo-sync.sh.
SKIP_RE='^(obsidian)$'
ONLY="${REPO_ONLY:-}"

# The nine stock GitHub labels, as "name<TAB>color<TAB>description", used to detect
# whether a default label is still untouched before --prune-stock removes it.
read -r -d '' STOCK <<'EOF' || true
bug	d73a4a	Something isn't working
documentation	0075ca	Improvements or additions to documentation
duplicate	cfd3d7	This issue or pull request already exists
enhancement	a2eeef	New feature or request
good first issue	7057ff	Good for newcomers
help wanted	008672	Extra attention is needed
invalid	e4e669	This doesn't seem right
question	d876e3	Further information is requested
wontfix	ffffff	This will not be worked on
EOF

APPLY=0; PRUNE=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1;;
    --prune-stock) PRUNE=1;;
    *) echo "unknown arg: $a" >&2; exit 2;;
  esac
done
MODE=$([ $APPLY = 1 ] && echo APPLY || echo DRY-RUN)

[ -f "$MANIFEST" ] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }

echo "== labels.sh [$MODE]  org=$ORG  prune-stock=$PRUNE =="

repos=$( { gh repo list "$ORG" --no-archived --source --limit 200 --json name -q '.[].name' | sed "s#^#$ORG/#"; printf '%s\n' $EXTRA_REPOS; } | sort)

for full in $repos; do
  OWNER=${full%%/*}; r=${full##*/}
  [[ "$r" =~ $SKIP_RE ]] && continue
  [ -n "$ONLY" ] && [ "$r" != "$ONLY" ] && continue
  echo; echo "### $r"

  # Snapshot current labels once: "name<TAB>color<TAB>description" per line.
  current=$(gh label list --repo "$OWNER/$r" --limit 200 \
    --json name,color,description -q '.[] | [.name, .color, (.description // "")] | @tsv' 2>/dev/null || true)

  # Reconcile each canonical label.
  while IFS=$'\t' read -r name color desc; do
    case "$name" in ''|\#*) continue;; esac
    line=$(printf '%s\n' "$current" | awk -F'\t' -v n="$name" '$1==n{print; exit}')
    if [ -z "$line" ]; then
      echo "  + create  $name"
      [ $APPLY = 1 ] && gh label create "$name" --repo "$OWNER/$r" --color "$color" --description "$desc" --force >/dev/null
    else
      cur_color=$(printf '%s' "$line" | cut -f2 | tr 'A-F' 'a-f')
      cur_desc=$(printf '%s' "$line" | cut -f3-)
      if [ "$cur_color" != "$(printf '%s' "$color" | tr 'A-F' 'a-f')" ] || [ "$cur_desc" != "$desc" ]; then
        echo "  ~ update  $name  (color/description drift)"
        [ $APPLY = 1 ] && gh label edit "$name" --repo "$OWNER/$r" --color "$color" --description "$desc" >/dev/null
      else
        echo "  = ok      $name"
      fi
    fi
  done < "$MANIFEST"

  # Optionally prune the untouched stock labels.
  if [ $PRUNE = 1 ]; then
    while IFS=$'\t' read -r sname scolor sdesc; do
      [ -z "$sname" ] && continue
      line=$(printf '%s\n' "$current" | awk -F'\t' -v n="$sname" '$1==n{print; exit}')
      [ -z "$line" ] && continue
      cur_color=$(printf '%s' "$line" | cut -f2 | tr 'A-F' 'a-f')
      cur_desc=$(printf '%s' "$line" | cut -f3-)
      if [ "$cur_color" = "$scolor" ] && [ "$cur_desc" = "$sdesc" ]; then
        echo "  - prune   $sname  (untouched stock label)"
        [ $APPLY = 1 ] && gh label delete "$sname" --repo "$OWNER/$r" --yes >/dev/null
      else
        echo "  · keep    $sname  (repurposed — not stock)"
      fi
    done <<< "$STOCK"
  fi
done

echo; echo "== done [$MODE] =="
[ $APPLY = 0 ] && echo "Re-run with --apply to create/fix labels (add --prune-stock to drop unused GitHub defaults)."
