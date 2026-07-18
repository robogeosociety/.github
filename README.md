# .github — shared PR "newspaper" framework (defaults)

Account-wide defaults for Tommy's repos. The PR description format is a
**newspaper / information pyramid** that reads top-to-bottom on an iPad-mini
portrait display. Full rules + voice: [`PR_FRAMEWORK.md`](PR_FRAMEWORK.md).

## What's here
- **`.github/pull_request_template.md`** — the newspaper skeleton. Auto-applies to
  public repos with no template of their own; private repos vendor their own copy.
- **`PR_FRAMEWORK.md`** — the canonical rules + voice (read before writing a body).
- **`.github/workflow-templates/pr-structure-gate.yml`** — the hard, deterministic
  CI gate on the PR description (below).
- **`.github/workflow-templates/pr-style-review.yml`** — the flexible, agentic
  length/style gate (below).
- **`.github/workflow-templates/awaiting-your-action.yml`** — turns the `human-task`
  / `blocked` labels into a personal work queue (below).

## Two gates, one framework
The old monolithic `pr-newspaper` validator failed on *everything* — structure **and**
subjective length/style — which churned. It's retired. Enforcement is now split into a
hard half and a soft half, each doing the job it's actually good at:

| Gate | File | What it does | Blocks merge? |
|------|------|--------------|:---:|
| **Structure gate** | `pr-structure-gate.yml` | Deterministic regex check that the body has the required newspaper spine — one `#` headline, an italic dek, a `> [!NOTE]` masthead, no heading-level skips, alt text on every image, no unfilled template placeholders. Objective only, so it doesn't churn. | ✅ yes |
| **Style review** | `pr-style-review.yml` | Agentic (Claude Code Action) judgment on the soft rules: length budget (with room to *double* it for genuinely complex code changes), Wired voice, whether mermaid diagrams actually make sense, and linking related open/draft issues. Posts one advisory comment. | ❌ no |

Both are self-contained workflow templates: one-click-selectable from the Actions
"New workflow" UI, and synced into every owned repo by [`scripts/sync.sh`](scripts/sync.sh).
The style review reuses the `CLAUDE_CODE_OAUTH_TOKEN` secret sync already sets.

**Escape hatches:** label a PR `skip-structure-gate` to bypass the hard gate, or
`skip-style-review` to skip the agentic one; bot authors (dependabot) are skipped
automatically. To make the structure gate a *required* check, mark it required in each
repo's branch-protection rules.

## Waiting-on-you queue — labels that ping you
`awaiting-your-action.yml` makes the `human-task` and `blocked` labels *actionable*.
Add either to an **issue or PR** and it assigns the item to you and posts a sticky
**⏳ Waiting on you** comment — so GitHub's own email + mobile notifications fire.
Remove the last such label and it un-assigns and flips the note to **✅ Cleared**.
One ping per state change, GitHub-native (no webhook, no secret), synced to every repo
by [`scripts/sync.sh`](scripts/sync.sh) alongside the gates. Your standing queue is then
just the GitHub search `assignee:@me label:human-task,blocked`.

## Generation vs enforcement
Generation happens **once**, by the agent that opens/updates a PR (it writes the body
from the full diff). There is no per-push `claude -p` regeneration. For a human push
with no agent, run `~/.claude/pr-framework/refresh_pr.sh <n>` locally. The two gates
above then check what was written — the hard gate deterministically, the style review
with judgment.

## Fleet automation — one scheduler manages every repo
Standardization doesn't wait for a human to remember to run it. The `fleet-sync.yml`
workflow — hosted in the private `supervisor` repo since the org runner group refuses
public repos, checking this repo out for the canonical files + scripts — is the CI/CD
that keeps the whole fleet in compliance, running on the **self-hosted runner on the
Mac mini** (a container with the `/Volumes/dev` checkout root mounted). Weekly — and
on demand — it:

1. **Mirrors** every owned repo into `/Volumes/dev` via
   [`scripts/repo-sync.sh`](scripts/repo-sync.sh) (clone missing, `git fetch --prune`
   the rest), so the local checkouts always track GitHub.
2. **Standardizes** each repo against the canonical files here via
   [`scripts/sync.sh`](scripts/sync.sh) — opening a PR per repo. This is the path by
   which the two PR-description gates above (and `@claude` / ruff) reach every repo.
3. **Reconciles labels** on every repo to the canonical taxonomy in
   [`scripts/labels.tsv`](scripts/labels.tsv) via
   [`scripts/labels.sh`](scripts/labels.sh) — pure GitHub API, no git. It's
   additive (creates missing labels, fixes drifted color/description) and never
   deletes a repo's bespoke labels; the untouched stock GitHub defaults are pruned
   only under the manual `--prune-stock` opt-in.

The canonical label set spans the three org-wide workflows: the **issue/task system**
(`human-task`, `machine-task`, `parked`), the **proposal + merge lane** (`proposal`,
`automerge`, and the `blocked` guard), and the **PR-gate bypasses** the framework
documents (`skip-structure-gate`, `skip-style-review`). Edit
`labels.tsv` to change the taxonomy; the next fleet-sync propagates it.

Scheduled runs apply automatically (new repos self-heal into compliance); manual
`workflow_dispatch` runs default to a read-only dry-run. See the header of
`fleet-sync.yml` for the one-time runner setup (labels, the `/Volumes/dev` mount, and
`gh` / `CLAUDE_CODE_OAUTH_TOKEN` auth).
