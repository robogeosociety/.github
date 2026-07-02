# The proposal shape — plan docs that preview before merge and flip themselves live

A **proposal** is a plan document that earns a rendered preview on the dev wiki *before* its PR
merges, then tracks its own delivery: **draft → accepted → 🟢 live**, with no hand-maintained
status anywhere. The mechanism lives in `obsidian-automations` (`automations/dev_wiki.py`, the
proposals lane); this page is the author-facing contract any `tommyroar/*` repo follows to opt in.

Sibling of [PR_FRAMEWORK.md](PR_FRAMEWORK.md) — the newspaper body is how a PR *reads*; this is
how a plan doc *lives*.

## The shape

1. **The doc lives under `docs/`.** The wiki ingests `README*` + `docs/**/*.md` and nothing else —
   a plan file at the repo root is invisible to both the preview lane and the canonical render.
   Convention: `docs/<topic>/PLAN.md` for multi-doc topics, `docs/<topic>.md` for one-pagers.
   (Learned the hard way: tommybot#73 shipped two case files at the root and previewed empty.)
2. **Frontmatter is the manifest:**

   ```yaml
   ---
   type: proposal
   implemented_by: []   # PR numbers that ship this plan — fill as they open, in this repo
   tracking: 0          # optional: an issue that closes when the whole plan has shipped
   ---
   ```

   `implemented_by` is the **only** signal the live marker reads — keep it current as
   implementing PRs open. `tracking` guards against a premature flip when the list is
   still growing: live requires **all** listed PRs merged **and** the tracking issue closed.
3. **The PR carries the `proposal` label.** Labels are per-repo — create it once per repo:

   ```sh
   gh label create proposal --repo tommyroar/<repo> \
     --description "Render this open PR's docs as a draft-banded preview on the dev wiki (proposals lane)" \
     --color 5319E7
   ```

   Draft PRs count. Only the label gates the preview — no registration anywhere else.

## The lifecycle (all derived — never hand-set a status)

| State | Condition (auto-derived from git/GitHub) | Where it shows |
|---|---|---|
| **Draft** | doc on an open `proposal`-labeled PR (not on `main`) | draft-banded preview at `/<repo-slug>/proposals/pr-<n>/` (`DRAFT · PR #n · <sha7> · not merged`), the `/<repo-slug>/proposals/` index (Open), `/daily` |
| **Accepted** | merged to `main`; any `implemented_by` PR still open, or `tracking` open, or the list empty | canonical page at `/<repo-slug>/docs/…`, banner *accepted · not yet shipped* |
| **Live** | on `main`; **all** `implemented_by` PRs merged **and** `tracking` (if set) closed | banner **🟢 live since \<date\>**, the proposals index (Shipped), `/daily` |

A `gh` hiccup never regresses a state — the build keeps the last-known state rather than
flipping or un-shipping on a transient failure.

## Timing

- **Pushes to the PR branch** refresh the preview within ~2–3 minutes (the supervisor's
  repo-push edge fires on any `tommyroar/*` push).
- **Label-only changes** (and merges of *other* repos' PRs listed in `implemented_by`) ride the
  `:15/:45` build sweep — ≤30 minutes.
- The wiki is tailnet-only: `https://tommys-mac-mini.tail59a169.ts.net:5193/<repo-slug>/`.

## What the preview renders

The PR **branch's** full `README` + `docs/**` tree at its head SHA (markdown ingest only — the
build never executes anything from an unmerged branch), isolated under `proposals/pr-<n>/` with
its own nav, excluded from the canonical site graph, self-expiring when the PR merges or closes.
Relative `.md` links rewrite to wiki URLs; links outside `docs/` become GitHub blob links.

## Reference

- Mechanism + rollout: `obsidian-automations` → `docs/dev-wiki-proposals/PLAN.md`
- The living dogfood: obsidian-automations#164 (open draft, tracking issue #163, ten+
  `implemented_by` PRs and counting)
- PR body conventions: [PR_FRAMEWORK.md](PR_FRAMEWORK.md)
