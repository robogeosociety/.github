# @Claude enablement + PR gates org-wide

## Problem Statement

Four org-wide agent/CI workflows are documented as canonical and "synced into every
owned repo" — `claude.yml`, `pr-structure-gate.yml`, `pr-style-review.yml`, and
`awaiting-your-action.yml`. In practice only `claude.yml` is actually installed
everywhere. The three others exist **only** as `.github/workflow-templates/` — one-
click-selectable, but not active in a single managed repo. The master Idea
(robot-geographical-society#163) wants full **@Claude** on issues/PRs plus the PR
gates enforced fleet-wide; today's reality is a documentation/deployment gap.

**Live inventory (2026-07, `gh api …/contents/.github/workflows/<wf>`):**

| Workflow | Deployed in repos | Status |
|---|---|---|
| `claude.yml` | maps, robot-geographical-society, discobots, obsidian-automations, tommybot, walksheds, observability-config, … | **present fleet-wide** (except `.github` itself) |
| `pr-structure-gate.yml` | *(none)* | template only |
| `pr-style-review.yml` | *(none)* | template only |
| `awaiting-your-action.yml` | *(none)* | template only |

So `PR_FRAMEWORK.md`'s promised hard gate, soft review, and waiting-on-you queue are
inert everywhere. The `sync.sh` reconciler already knows how to install them — it
just hasn't been run to apply, and no scheduled runner guarantees it.

## Requirements

- All four workflows **active** in every managed repo (Cloudflare/bespoke-deploy repos
  keep their deploy carve-outs, per `sync.sh`'s existing `CF_RE`/`PAGES_EXC_RE`).
- The `CLAUDE_CODE_OAUTH_TOKEN` secret present wherever `claude.yml`/`pr-style-review`
  need it (`sync.sh` step 1 already handles this).
- A **scheduled** guarantee, not a one-shot: fleet-sync runs weekly + on demand so new
  repos self-heal.
- A living **inventory** of which repo has which workflow, regenerated each run.
- GitHub built-ins first; reuse the existing `sync.sh`/`fleet-sync.yml` machinery.

## Solution

**1. Add `awaiting-your-action.yml` to `sync.sh`'s synced set.** `sync.sh` currently
installs `claude.yml` + both PR gates but references `AWAIT_SRC` without syncing it in
the loop; wire it in beside the gates so all four land together.

**2. Run `fleet-sync.yml --apply`** (the self-hosted mini runner, PR #13's weekly
scheduler) to open the per-repo standardization PRs that install the three missing
workflows. Because `sync.sh` lands file changes on a `chore/standardize-sync` branch +
PR (never straight to main), rollout is reviewable per repo.

**3. Add an inventory step** to `fleet-sync.yml`'s job summary: for each managed repo,
probe the four workflow paths and emit a present/absent matrix to
`$GITHUB_STEP_SUMMARY` (and optionally a committed `docs/fleet-inventory.md`). This
turns "are the gates really on?" into a glance instead of 27 `gh api` calls.

**4. Enable @Claude fully** — `claude.yml` already fires on `@claude` in issue/PR/review
comments and on `issues: [opened, assigned]`. Confirm each repo carries the secret and
that the responder has `contents/pull-requests/issues: write` (it does in the template),
so @Claude can push edits/enhancements to proposals — the master Idea's requirement.

No new runtime code — this is a **deployment** proposal: existing canonical workflows,
actually switched on, plus an inventory. Python-first only where the inventory probe
needs logic (a small `scripts/inventory.sh`/`.py` reusing `gh api`).

## Alternatives

- **One-click install per repo** from the Actions "New workflow" UI — works, but manual
  and drift-prone (exactly why `sync.sh` exists). Rejected as the primary path.
- **A GitHub Teams plan** with **org rulesets / required workflows** could *mandate* the
  gates across all repos centrally (required workflows are an org feature), removing
  per-repo file installs entirely. This is the cleanest long-term shape — noted as the
  paid simplification.
- **Mark gates required in branch protection now** — good hardening, but pointless
  until the workflow files exist in each repo; sequenced after install.
- **Reuse `discobots`** to report install status to Discord — nice-to-have
  notification layer; the authoritative inventory stays in the fleet-sync summary.

## Tasks

- [ ] Wire `awaiting-your-action.yml` into `sync.sh`'s per-repo install loop.
- [ ] Add the four-workflow present/absent inventory to `fleet-sync.yml`'s job summary.
- [ ] Dry-run `sync.sh` (default) to confirm the three-missing-per-repo delta.
- [ ] `fleet-sync.yml --apply`: land the per-repo PRs installing the gates + awaiting queue.
- [ ] Confirm `CLAUDE_CODE_OAUTH_TOKEN` present in every repo (sync.sh step 1).
- [ ] Verify: open a test PR in one repo → structure gate + style review both run; label `human-task` → awaiting-your-action assigns + pings.
- [ ] Merge the per-repo PRs; then mark `pr-structure-gate` a required check where wanted.

## Further Reading

- Master Idea — robot-geographical-society#163
- Fleet-sync + labels — robogeosociety/.github#13
- The two gates + awaiting queue — `robogeosociety/.github/README.md`, `PR_FRAMEWORK.md`
- Standardizer — `scripts/sync.sh`, `.github/workflow-templates/`
