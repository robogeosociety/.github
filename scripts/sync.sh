#!/usr/bin/env bash
#
# sync.sh — standardize tommyroar repos from the canonical files in this repo.
# Source of truth: tommyroar/.github.
#
#   ./scripts/sync.sh            # DRY-RUN (read-only): print the per-repo delta
#   CLAUDE_CODE_OAUTH_TOKEN=... ./scripts/sync.sh --apply   # set secrets + open PRs
#
# Idempotent. Safe to re-run (weekly cron) so new repos self-heal into compliance.
# What it does per owned, non-fork, non-archived repo:
#   1. ensure the CLAUDE_CODE_OAUTH_TOKEN Actions secret is set
#   2. install/refresh .github/workflows/claude.yml            (canonical @claude)
#   3. install/refresh .github/workflows/pr-structure-gate.yml (hard PR-desc gate)
#   4. install/refresh .github/workflows/pr-style-review.yml   (agentic style gate)
#   5. remove .github/workflows/pr-newspaper.yml               (old newspaper retired)
#   6. Python repos: install .pre-commit-config.yaml, and lint.yml if ruff isn't already in CI
# File changes land on a branch + PR (never pushed straight to main).
set -euo pipefail

OWNER=tommyroar
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_SRC="$ROOT/.github/workflow-templates/claude.yml"
STRUCTURE_SRC="$ROOT/.github/workflow-templates/pr-structure-gate.yml"
STYLE_SRC="$ROOT/.github/workflow-templates/pr-style-review.yml"
PRECOMMIT_SRC="$ROOT/standard/.pre-commit-config.yaml"
LINT_SRC="$ROOT/standard/workflows/lint.yml"
BRANCH="chore/standardize-sync"

APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
MODE=$([ $APPLY = 1 ] && echo APPLY || echo DRY-RUN)

# Never touched by the loop. obsidian = vault data; the mini is its sole git writer,
# so the Air must never open PRs against it (handle from the mini if ever needed).
SKIP_RE='^(\.github|pr-newspaper|obsidian)$'
# Optional pilot: REPO_ONLY=tommybot ./scripts/sync.sh --apply  restricts to one repo.
ONLY="${REPO_ONLY:-}"
# Cloudflare / bespoke-deploy repos: standardize lint+@claude, DON'T touch deploys.
CF_RE='^(robot-geographical-society|maps)$'
# GitHub-Pages-all-in repo with its own bespoke per-PR preview deploy: leave deploy alone.
PAGES_EXC_RE='^(walksheds)$'
# Already run ruff inside an existing ci.yml -> keep pre-commit, skip the standalone lint.yml.
RUFF_IN_CI_RE='^(tommybot|obsidian-automations|home-weather-hub|tallest-tree)$'
# Pure-content repos -> no Python linting even if a requirements.txt exists.
NO_PYLINT_RE='^(walksheds-wiki|walksheds-dev-wiki)$'

echo "== sync.sh [$MODE]  owner=$OWNER =="
if [ $APPLY = 1 ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "ERROR: export CLAUDE_CODE_OAUTH_TOKEN before --apply (mint via: claude setup-token)"; exit 1
fi

api_exists() { gh api "repos/$OWNER/$1/contents/$2" >/dev/null 2>&1; }

repos=$(gh repo list "$OWNER" --no-archived --source --limit 200 --json name -q '.[].name' | sort)

for r in $repos; do
  [[ "$r" =~ $SKIP_RE ]] && continue
  [ -n "$ONLY" ] && [ "$r" != "$ONLY" ] && continue
  echo; echo "### $r"
  tags=""
  [[ "$r" =~ $CF_RE ]] && tags="$tags [cloudflare: deploys untouched]"
  [[ "$r" =~ $PAGES_EXC_RE ]] && tags="$tags [pages-exception: deploy untouched]"
  [ -n "$tags" ] && echo " $tags"

  # 1) OAuth secret
  if gh secret list --repo "$OWNER/$r" 2>/dev/null | grep -q '^CLAUDE_CODE_OAUTH_TOKEN'; then
    echo "  secret CLAUDE_CODE_OAUTH_TOKEN : present"
  else
    echo "  secret CLAUDE_CODE_OAUTH_TOKEN : MISSING -> set"
    [ $APPLY = 1 ] && gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$OWNER/$r" --body "$CLAUDE_CODE_OAUTH_TOKEN"
  fi

  # decide the file changes (collected, then applied once via one PR)
  changes=()
  if api_exists "$r" ".github/workflows/claude.yml"; then
    echo "  claude.yml       : present (refresh to canonical)"
  else
    echo "  claude.yml       : MISSING -> add"
  fi
  changes+=("put:.github/workflows/claude.yml:$CLAUDE_SRC")
  if [[ "$r" =~ $PAGES_EXC_RE ]]; then
    echo "  NOTE: walksheds has an embedded @claude job in ci.yml — remove it by hand to avoid double-trigger."
  fi

  # PR-description gates: the hard structural CI gate + the agentic style/length gate.
  if api_exists "$r" ".github/workflows/pr-structure-gate.yml"; then
    echo "  pr-structure-gate.yml : present (refresh to canonical)"
  else
    echo "  pr-structure-gate.yml : MISSING -> add"
  fi
  changes+=("put:.github/workflows/pr-structure-gate.yml:$STRUCTURE_SRC")
  if api_exists "$r" ".github/workflows/pr-style-review.yml"; then
    echo "  pr-style-review.yml   : present (refresh to canonical)"
  else
    echo "  pr-style-review.yml   : MISSING -> add"
  fi
  changes+=("put:.github/workflows/pr-style-review.yml:$STYLE_SRC")

  if api_exists "$r" ".github/workflows/pr-newspaper.yml"; then
    echo "  pr-newspaper.yml : present -> REMOVE"
    changes+=("del:.github/workflows/pr-newspaper.yml")
  fi

  if { api_exists "$r" "pyproject.toml" || api_exists "$r" "requirements.txt"; } && ! [[ "$r" =~ $NO_PYLINT_RE ]]; then
    if api_exists "$r" ".pre-commit-config.yaml"; then
      echo "  python           : yes | .pre-commit-config.yaml present"
    else
      echo "  python           : yes | .pre-commit-config.yaml MISSING -> add"
      changes+=("put:.pre-commit-config.yaml:$PRECOMMIT_SRC")
    fi
    if [[ "$r" =~ $RUFF_IN_CI_RE ]]; then
      echo "                     lint.yml skipped (ruff already runs in ci.yml)"
    elif api_exists "$r" ".github/workflows/lint.yml"; then
      echo "                     lint.yml present"
    else
      echo "                     lint.yml MISSING -> add"
      changes+=("put:.github/workflows/lint.yml:$LINT_SRC")
    fi
  fi

  if [ $APPLY = 1 ]; then
    tmp="$(mktemp -d)"
    gh repo clone "$OWNER/$r" "$tmp" -- --depth 1 -q
    ( cd "$tmp"
      git switch -c "$BRANCH" -q
      for c in "${changes[@]}"; do
        op="${c%%:*}"; rest="${c#*:}"
        if [ "$op" = put ]; then dst="${rest%%:*}"; src="${rest#*:}"; mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"; git add "$dst"
        elif [ "$op" = del ]; then git rm -q --ignore-unmatch "${rest}"; fi
      done
      if ! git diff --cached --quiet; then
        git commit -q -m "chore: standardize @claude / PR gates / lint (via tommyroar/.github sync)"
        git push -q -u --force origin "$BRANCH"
        gh pr view "$BRANCH" >/dev/null 2>&1 && { echo "  (PR already open)"; } || \
        gh pr create --head "$BRANCH" --title "Standardize: @claude + PR description gates + lint" \
          --body "Automated sync from tommyroar/.github. Canonical @claude workflow, the hard pr-structure-gate + agentic pr-style-review PR-description gates, pre-commit/ruff for Python, old newspaper workflow removed. 🤖 Generated with Claude Code" || true
      fi
    )
    rm -rf "$tmp"
  fi
done

echo; echo "== done [$MODE] =="
[ $APPLY = 0 ] && echo "Re-run with --apply (and CLAUDE_CODE_OAUTH_TOKEN exported) to set secrets and open PRs."
