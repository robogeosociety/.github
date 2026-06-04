# .github — shared PR "newspaper" framework (defaults)

Account-wide defaults for Tommy's repos. The PR description format is a
**newspaper / information pyramid** that reads top-to-bottom on an iPad-mini
portrait display. Full rules + voice: [`PR_FRAMEWORK.md`](PR_FRAMEWORK.md).

## What's here
- **`.github/pull_request_template.md`** — the newspaper skeleton. Auto-applies to
  public repos with no template of their own; private repos vendor their own copy.
- **`PR_FRAMEWORK.md`** — the canonical rules + voice (read before writing a body).

## Enforcement lives next door
CI validation (the reusable `pr-newspaper` workflow + the stdlib validator) lives in
[`tommyroar/pr-newspaper`](https://github.com/tommyroar/pr-newspaper) — reusable
workflows can't be served from a `.github` repo, so they get their own. Repos opt in
with a small caller workflow (see that repo's README).

## Generation vs enforcement
Generation happens **once**, by the agent that opens/updates a PR (it writes the body
from the full diff). There is no per-push `claude -p` regeneration. For a human push
with no agent, run `~/.claude/pr-framework/refresh_pr.sh <n>` locally.
