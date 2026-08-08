# Copilot Instructions — robogeosociety org default

These instructions apply to all Copilot interactions across the org's repositories.

## PR descriptions

Every PR description follows the **newspaper / information-pyramid** framework
defined in `PR_FRAMEWORK.md`. When Copilot opens or updates a PR, it must
regenerate the body from the full diff — rebuilt from scratch, never appended to.

Key rules:
- One H1 headline (evocative but accurate — a real title, not "Update X")
- An italic dek (1–2 sentences setting the stakes)
- A `> [!NOTE]` masthead with area · type · risk · closes
- Pyramid order: Why → What → Flow → Screens → Verification → Risk
- Target length: 1–2 iPad-mini pages (up to 4 for genuinely complex code changes)
- Mermaid diagrams orient **TD** (top-down), never LR/RL
- Every image has alt text

## Code style

- Python: follow ruff defaults (see `standard/ruff.toml`)
- Shell: `set -euo pipefail`, quote variables
- Commits: conventional-commit prefix (`feat:`, `fix:`, `chore:`, `docs:`)

## Repo conventions

- The `.github` repo is the source of truth for workflow templates and org standards
- `scripts/sync.sh` distributes canonical workflows to all repos
- Never push directly to `main` — always open a PR
