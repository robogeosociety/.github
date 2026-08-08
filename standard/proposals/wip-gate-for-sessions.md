# Proposal — a WIP gate for Claude sessions

**Status:** draft · **Depends on:** `standard/project-policy.yml`, `ops/project_review.py`

Make a Claude session declare which project it is working on, and make starting new
work in an already-overloaded workstream a deliberate act rather than an accident.

---

## The problem this actually solves

`project-policy.yml` gave the org WIP limits, and the Monday review reports breaches.
Reporting is a lagging signal: by the time `the board · Proposals — 13/12` appears in
#dev, the thirteenth thing has been open for up to a week and the work is done.

Worse, the survey that produced those limits found **51 open items on no board at
all** — obsidian-automations 14, discobots 14, supervisor 10, infra 7. Work that was
never placed cannot be over a limit, so the WIP numbers understate the real load by
whatever the unboarded pile happens to be. A limit nothing is measured against is
decoration.

Both problems have the same root: nothing asks *"which workstream is this?"* at the
moment the answer is cheap — before the work starts.

---

## Disagreement with the literal ask, stated up front

The request was for a **pre-commit hook**. I do not think pre-commit should be the
enforcement point, and the reason is worth having on the record before anyone builds
it.

| point | when it fires | can it block? | bypass | reaches agent sessions on the mini / in Actions |
| --- | --- | --- | --- | --- |
| **SessionStart hook** | before any work | yes | close and reopen | no — local Claude Code only |
| **pre-commit hook** | after the work, before the commit | yes | `--no-verify`, trivially | no — and not installed unless someone runs a setup step |
| **CI check on the PR** | after push | yes, and it holds | changing the workflow | yes |

Pre-commit is the weakest of the three: it fires **after** the work it is meant to
prevent, `--no-verify` defeats it silently, and it does not exist in a fresh clone
until something installs it. A gate that is late *and* optional trains people to
route around it.

**Recommendation: SessionStart for the prompt, CI for the enforcement.** The hook
makes choosing a project the path of least resistance while it is still free; CI is
what makes it true. Pre-commit adds a third place to maintain the same rule and
catches nothing the other two miss.

---

## Design

### 1. A session declares its workstream

At SessionStart the hook reads the current branch and worktree, asks the board which
project the repo's work usually lands on, and writes:

```
.claude/session.json     { "project": 7, "status": "Drafts", "started": "…", "override": null }
```

Untracked, one per worktree. It is a *declaration*, not a lock — nothing stops the
session doing something else, and the file is the thing CI later checks against.

### 2. The WIP check

Read `standard/project-policy.yml` for the limit, count the target column live, and
compare. Three outcomes:

- **under limit** — write the file, say nothing, continue
- **at limit** — continue, but say so: *"`the board · Drafts` is 8/8. This will be the ninth."*
- **over limit** — require an override before continuing

`wip_exempt` projects (References) and `dormant` ones skip the check entirely, exactly
as the Monday review already treats them.

### 3. The override is recorded, not just permitted

An override that leaves no trace is a bypass with extra steps. It costs a reason:

```
CLAUDE_WIP_OVERRIDE="hotfix — deploy-gate is down" claude
```

…which lands in `.claude/session.json`, and from there into the commit as a trailer:

```
Wip-Override: hotfix — deploy-gate is down (the board · Drafts 9/8)
```

The trailer is the durable artifact. CI reads it; the Monday review counts it. **A
week with four overrides is not a discipline problem, it is a limit that is set
wrong** — and the review should say so rather than leaving someone to feel bad about
a number nobody revisits.

### 4. The CI check

One job on every PR:

- the branch's commits carry a project declaration, or a `Wip-Override:` trailer → pass
- neither, and the target column is over limit → fail, with the column and the count
- neither, but the column is fine → **pass with a warning**, and place the PR on the board

That last row matters. The failure mode to avoid is a gate that blocks 51 existing
unboarded items on day one; the check should place work, not reject it, whenever the
workstream has room.

---

## Rollout, in the order that keeps it survivable

1. **CI check in warn-only mode** for two weeks. It annotates and never fails. This
   measures how often the rule *would* have fired before anyone is blocked by it —
   the same reason the WIP numbers themselves were set near current occupancy rather
   than at an aspiration.
2. **SessionStart hook**, opt-in per machine. The Air first; the mini's unattended
   agents keep running unchecked, since a headless session has nobody to answer a
   prompt.
3. **CI check enforcing**, once the warn-only data shows the limits are roughly right.
4. **Override counts in the Monday review**, from the start — the number is only
   useful as a trend.

---

## What could go wrong

- **Headless sessions cannot answer a prompt.** The mini's agents and `claude -p` in
  Actions must default to "declare nothing, warn, continue". A hook that blocks an
  unattended session is an outage.
- **The board lookup needs a token the hook may not have.** `PROJECT_SYNC_TOKEN`
  lives on the `.github` repo and — as the first weekly review discovered — currently
  sees only 17 of 31 repos. A hook that cannot read the board must fail *open* and
  say so, never guess.
- **A stale `session.json`** in a long-lived worktree will claim the wrong project.
  Expire it on branch change.
- **This adds friction to the fastest path in the fleet** — open a session, fix a
  thing, push. If the prompt costs more than the WIP limit saves, it should be
  deleted rather than tuned.

---

## Open questions

1. Should the declaration be per **session** or per **branch**? Branch survives a
   restart and matches how PRs are reviewed; session is simpler and matches the ask.
2. Does an override need approval, or is recording it enough? Recording is the lighter
   choice and the one this proposal assumes.
3. Should the CI check auto-place unboarded PRs, or only report? Auto-placing fixes
   the 51-item gap quickly and writes to boards on every PR — a wider blast radius
   than the weekly job's capped 25.
