#!/usr/bin/env python3
"""Daily breach check: #dev posts that indicate the workflow was bypassed.

#dev is where the org's machinery confesses. A deploy approved outside the gate,
a "pending Tommy" notice that never became a human-task issue, a decision made in
chat that never landed on a board, the same CI failure notice repeating for days
with no fix or parked verdict — each shows up in the channel long before any
GitHub query would notice. This job reads the last day of #dev and asks whether
any post is evidence that the documented workflow (OPERATIONS.md, PR_FRAMEWORK.md,
the deploy gate, the label taxonomy) was stepped around.

Same honesty posture as project_review: the default run REPORTS to stdout and
mutates nothing. `--post` sends a card to #dev; `--issue` opens/refreshes ONE
sticky `workflow-breach` issue (labelled `human-task`, so the waiting-on-you
queue pings) — and only when there are findings. A quiet day posts a one-line
"nothing found" card and opens no issue: an empty breach report every morning
trains its reader to stop reading breach reports.

Reads #dev through deploy-gate's `/dev-log` read lane (discobots#209) with the
NOTIFY_SECRET this repo already holds — the same signed bridge the card and the
weekly review already use to post. No Discord credential enters this repo, and
the paging/embed-folding lives (and is tested) once, in the worker. Until the
lane is deployed, a 404 from the gate is a documented skip, not a red morning.

Env:
  NOTIFY_SECRET       the deploy-gate HMAC secret (absent → skip)
  DEV_LOOKBACK_HOURS  window, default 26 (daily cadence + an hour of slack;
                      the gate caps at 48)

Usage:
  dev_breach.py                  # report to stdout, write nothing
  dev_breach.py --post           # also post the card to #dev
  dev_breach.py --post --issue   # additionally open/refresh the breach issue
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from datetime import datetime

# Shares the fences, the signed /notify path and the gh wrapper with the weekly
# review — one posture, defined once.
from project_review import (
    MAX_TURNS,
    NO_TOOLS,
    NOTIFY_SECRET,
    SYSTEM,
    _gh,
    _signed_post,
    _stats,
)

LOOKBACK_HOURS = int(os.environ.get("DEV_LOOKBACK_HOURS", "26"))
BREACH_REPO = os.environ.get("BRIEF_REPO", "robogeosociety/.github")
BREACH_LABEL = "workflow-breach"

# Everything the model sees per message / in total. #dev is mostly bot embeds;
# 120 messages of 400 chars is a full day with room to spare, and a hard cap
# keeps a pathological day (someone pastes a log) from blowing the prompt.
MAX_MESSAGES = 120
MAX_CHARS = 400

# The rules, stated compactly enough to hold in one prompt. Distilled from
# OPERATIONS.md / PR_FRAMEWORK.md / the deploy-gate README — update this digest
# when those documents change, or the check will police a stale constitution.
RULES = """\
R1  Deploys go through the deploy-gate (Approve/Reject card in #dev) or are
    auto-approved workflow_dispatch by the operator. Talk of deploying around
    the gate, SSH-ing to prod, or approving one's own card is a breach.
R2  Anything pending Tommy — credential ceremonies, approvals, blocked chains —
    must exist as a human-task labelled issue/PR, not only as a chat message.
    A "waiting on Tommy" post with no issue link is a breach of that directive.
R3  Decisions (priorities, parking, closing, WIP limits) belong on boards,
    labels, or policy PRs. A decision announced in chat with no GitHub artifact
    is a breach; discussion without a decision is not.
R4  Merges ride PRs with the gates. Posts describing direct pushes to a default
    branch, merging with red checks, or disabling/skipping a gate to get green
    are breaches (a `skip-*` label used and named openly is the documented
    escape hatch, not a breach).
R5  The same CI-failure notice recurring across days with no fix, no `parked`,
    and no human-task issue is a breach of the drive-to-green posture.
R6  Agents must not widen credentials or copy secrets between homes. Posts
    describing a token pasted somewhere new, a secret in a log, or auth worked
    around are breaches — and severity high.
"""


def fetch_messages(hours: int = LOOKBACK_HOURS, post=_signed_post) -> list:
    """The lookback window of #dev via the gate, oldest first: [{ts, author, text}].

    The worker owns pagination, the cutoff walk and embed folding (and tests
    them); this side only reshapes timestamps for the prompt and enforces its
    own per-message character cap — the model's budget is this script's concern,
    not the bridge's.
    """
    res = post("/dev-log", {"hours": hours, "limit": 300})
    if not res.get("ok"):
        raise RuntimeError(f"/dev-log refused: {json.dumps(res)[:200]}")
    out = []
    for m in res.get("messages") or []:
        ts = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
        out.append(
            {
                "ts": ts.strftime("%m-%d %H:%M"),
                "author": m.get("author") or "?",
                "text": (m.get("text") or "")[:MAX_CHARS],
            }
        )
    return out


def judge(messages: list) -> tuple[list, dict]:
    """Ask which posts evidence a breach. Returns ([{ts, author, rule,
    severity, excerpt, why}], stats); ([], {}) on any failure — a failed model
    call must not fail the run, it just means no findings today and the honest
    telemetry says why.
    """
    if not messages:
        return [], {}
    if not shutil.which("claude"):
        print("claude not on PATH — skipping the judgment pass", file=sys.stderr)
        return [], {}
    lines = [f"[{m['ts']}] {m['author']}: {m['text']}" for m in messages[-MAX_MESSAGES:]]
    prompt = (
        "Below are the org's workflow rules, then one day of posts from its #dev "
        "channel (a mix of bot notices and humans). Identify posts that are "
        "EVIDENCE a rule was breached — something already done or decided outside "
        "the workflow, not merely discussed. Judge conservatively: routine bot "
        "notices, gate cards being used normally, and open discussion are not "
        "breaches. When in doubt, it is not a breach.\n\n"
        f"Rules:\n{RULES}\n"
        "Posts:\n" + "\n".join(lines) + "\n\n"
        'Reply with JSON only: {"findings":[{"ts":"<ts>","author":"<who>",'
        '"rule":"R1".."R6","severity":"high|low","excerpt":"<=15 words from the '
        'post","why":"<=15 words"}]} — an empty findings list is the expected '
        "answer on most days."
    )
    proc = subprocess.run(
        [
            "claude",
            "-p",
            "--model",
            os.environ.get("REVIEW_MODEL", "haiku"),
            "--output-format",
            "json",
            "--max-turns",
            str(MAX_TURNS),
            "--disallowed-tools",
            *NO_TOOLS,
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--append-system-prompt",
            SYSTEM,
        ],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        print(f"judgment failed rc={proc.returncode}: {proc.stdout[-500:]}", file=sys.stderr)
        return [], {}
    try:
        payload = json.loads(proc.stdout)
        text = payload.get("result") or ""
        stats = _stats(payload)
    except json.JSONDecodeError:
        return [], {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return [], stats
    try:
        found = json.loads(text[start : end + 1]).get("findings") or []
    except json.JSONDecodeError:
        return [], stats
    valid = [
        f
        for f in found
        if f.get("rule") in {f"R{i}" for i in range(1, 7)}
        and f.get("severity") in ("high", "low")
    ]
    return valid, stats


def render(findings: list, scanned: int) -> tuple[str, str]:
    """(#dev card, issue body). The card is one line on a quiet day."""
    if not findings:
        return (
            f"Scanned **{scanned}** #dev post(s) from the last {LOOKBACK_HOURS}h — "
            "no workflow breaches found.",
            "",
        )
    rows = [
        f"- `{f['rule']}`/{f['severity']} [{f['ts']}] {f['author']} — "
        f"“{f.get('excerpt', '')[:80]}” — {f.get('why', '')[:80]}"
        for f in findings
    ]
    card = (
        f"**{len(findings)}** post(s) in the last {LOOKBACK_HOURS}h look like workflow "
        f"breaches (of {scanned} scanned):\n" + "\n".join(rows[:8])
    )
    body = (
        "> _Posts in #dev that look like the workflow was stepped around — found by "
        "the daily breach check, judged against the rules digest in "
        "`ops/dev_breach.py`. A finding is a question, not a verdict: confirm it, "
        "fix the artifact it's missing (issue, label, board card, gate), or say "
        "here why it's a false positive so the rules digest can be tightened._\n\n"
        + "\n".join(rows)
        + "\n\n**Default:** re-reported while the evidence keeps appearing; nothing "
        "is changed on your behalf.\n\n---\n"
        "-# Opened by `ops/dev_breach.py` (daily). Close when every finding is "
        "resolved or refuted — the next findings reopen a fresh one."
    )
    return card, body


def post_issue(body: str) -> str:
    """One open breach issue at a time, refreshed in place. Returns its URL.

    Unlike the ops brief there is no weekly identity: findings accumulate into
    whichever breach issue is open, and closing it is the human's statement that
    the slate is clear.
    """
    repo = BREACH_REPO
    for name, color, desc in [
        (BREACH_LABEL, "B60205", "A #dev post suggests the workflow was bypassed"),
        ("human-task", "D93F0B", "Needs Tommy's hands — operator runbook (gh-task-human)"),
    ]:
        _gh(["label", "create", name, "--repo", repo, "--color", color,
             "--description", desc, "--force"])
    existing = json.loads(
        _gh(["issue", "list", "--repo", repo, "--label", BREACH_LABEL,
             "--state", "open", "--json", "number"])
    )
    if existing:
        n = existing[0]["number"]
        _gh(["issue", "edit", str(n), "--repo", repo, "--body-file", "-"], input=body)
        return f"https://github.com/{repo}/issues/{n}"
    out = _gh(["issue", "create", "--repo", repo,
               "--title", "Workflow breaches — spotted in #dev",
               "--label", BREACH_LABEL, "--label", "human-task",
               "--body-file", "-"], input=body)
    return out.strip().splitlines()[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post", action="store_true", help="post the card to #dev")
    ap.add_argument(
        "--issue",
        action="store_true",
        help="open/refresh the sticky breach issue when there are findings",
    )
    args = ap.parse_args()

    if not NOTIFY_SECRET:
        print("NOTIFY_SECRET unset — breach check skipped", file=sys.stderr)
        return 0

    try:
        messages = fetch_messages()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            # The gate predates its read lane. A documented state, not an error:
            # the check ships ahead of the worker deploy (discobots#209) and the
            # schedule stays green rather than crying wolf every morning.
            print(
                "deploy-gate has no /dev-log yet (merge discobots#209) — skipped",
                file=sys.stderr,
            )
            return 0
        print(f"could not read #dev: {err}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, RuntimeError) as err:
        print(f"could not read #dev: {err}", file=sys.stderr)
        return 1

    findings, stats = judge(messages)
    card, body = render(findings, len(messages))
    print(card)

    if args.issue and findings:
        url = post_issue(body)
        print(f"breach issue: {url}")
        card += f"\n-# Tracked in {url}"

    if args.post:
        _signed_post(
            "/notify",
            {
                "title": "Daily workflow-breach check",
                "body": card,
                "level": "warn" if findings else "info",
                "telemetry": stats,
            },
        )
        print("posted to #dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
