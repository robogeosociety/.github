#!/usr/bin/env python3
"""Daily alarm audit: #ops pages that don't fit the org's established patterns.

#ops is the machine-page channel — uptime pages, CD heartbeats, babysitter
notices, dashboard panels. Every post there is an alarm or a pulse, and the
failure mode is never silence: it is an alarm that doesn't make SENSE. A page
about a bot the org retired. A "recovered" with no preceding failure. The same
heartbeat-stale notice firing for days while nothing acknowledges it. An
error-level page whose body is routine. Each of those means the monitoring has
drifted from the system it monitors — and each hides in plain sight, because a
channel of alarms trains its reader to skim.

This job reads a day of #ops and asks which pages don't fit. "Fit" is judged
against the ops bot's accumulated context, approximated by three inputs:
  * the window itself — repeats, contradictions, recoveries without failures
  * the dev vault over Vectorize (deploy-gate /search) — the org's own notes
    on what exists, what was retired, and what each alarm class means
  * the pattern digest below — the alarm classes the org has named

Same posture as dev_breach: report-only by default, `--post` sends the card
(to #dev — the operator-attention lane; #ops stays machines-only), `--issue`
refreshes ONE sticky `alarm-anomaly` issue, and only on findings. Reads ride
deploy-gate's /dev-log lane with the NOTIFY_SECRET already here; a gate that
predates the ops channel param answers with #dev's history, which the run
detects and treats as a skip, not a scan.

Usage:
  alarm_audit.py                  # report to stdout, write nothing
  alarm_audit.py --post           # also post the card to #dev
  alarm_audit.py --post --issue   # additionally open/refresh the anomaly issue
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from datetime import datetime

from project_review import (
    MAX_TURNS,
    NO_TOOLS,
    NOTIFY_SECRET,
    SYSTEM,
    _signed_post,
    _stats,
    sticky_issue,
    vault_context,
)

LOOKBACK_HOURS = int(os.environ.get("OPS_LOOKBACK_HOURS", "26"))
ALARM_LABEL = "alarm-anomaly"
# How much of the window the model actually reads. Env-tunable because the
# audit has two modes: the daily tick (a day fits in 120) and the retro sweep
# ("what should have been flagged this week"), where truncating to the newest
# 120 would silently audit Thursday and call it the week.
MAX_MESSAGES = int(os.environ.get("OPS_MAX_MESSAGES", "120"))
MAX_CHARS = 400
# Ask the gate for its own maximum; the server clamps. Requesting less would
# quietly narrow a sweep to whatever this constant said.
FETCH_LIMIT = 1000

# The anomaly classes, named so a finding lands as a diagnosis rather than a
# vibe. Distilled from the org's own incident history (host-drift, the two dead
# CD days, retired-bot cleanup in discobots run.sh); update the digest when the
# fleet changes shape, or the audit will police a stale fleet.
PATTERNS = """\
A1  zombie — a page about a service, bot, or host the org retired or moved.
    The monitoring outlived the monitored; the alarm can only ever be noise.
A2  orphan — a page from a source the org's notes don't know: no vault entry,
    no named alarm class, no obvious owner. Unowned alarms never get fixed.
A3  contradiction — a page that can't be true alongside its neighbours or the
    notes: "recovered" with no preceding failure, red for a system another
    post just called healthy, two sources disagreeing about the same fact.
A4  groundhog — the same page recurring through the window with no fix, no
    acknowledgment, no parked verdict. An alarm that fires forever is an
    alarm that has already been ignored.
A5  miscalibrated — severity that doesn't match content: an error-level pulse
    that reports normality, an info-level line reporting data loss, a page
    with no actionable content at all.
"""


def fetch_messages(hours: int = LOOKBACK_HOURS, post=_signed_post) -> list:
    """The lookback window of #ops via the gate, oldest first.

    The reply names the channel it actually read. A gate that predates the
    channel param silently serves #dev — auditing the wrong channel and calling
    it quiet is the exact failure class this job hunts, so that reply is a
    RuntimeError with a name, not a scan.
    """
    res = post("/dev-log", {"hours": hours, "limit": FETCH_LIMIT, "channel": "ops"})
    if not res.get("ok"):
        raise RuntimeError(f"/dev-log refused: {json.dumps(res)[:200]}")
    if res.get("channel") != "#ops":
        raise RuntimeError(
            f"gate served {res.get('channel')} instead of #ops — "
            "deploy the channel-aware /dev-log (discobots#211) first"
        )
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
    """Which pages don't fit. ([{ts, author, pattern, severity, excerpt, why}],
    stats); ([], {}) on any failure — a dead model call costs the findings,
    never the run."""
    if not messages:
        return [], {}
    if not shutil.which("claude"):
        print("claude not on PATH — skipping the judgment pass", file=sys.stderr)
        return [], {}
    # The vault is the accumulated context: what exists, what was retired, what
    # each alarm class means. Queried with the window's distinct page shapes.
    seen, queries = set(), []
    for m in messages:
        key = m["text"][:60]
        if key not in seen:
            seen.add(key)
            queries.append(m["text"][:100])
    context, rag = vault_context(queries[:4])

    lines = [f"[{m['ts']}] {m['author']}: {m['text']}" for m in messages[-MAX_MESSAGES:]]
    prompt = (
        "Below are the org's named alarm-anomaly patterns, context from its own "
        "notes, then one day of posts from #ops — its machine-page channel "
        "(uptime, CD heartbeats, babysitters, panels). Identify pages that DON'T "
        "MAKE SENSE against that context: match each finding to one pattern. "
        "Judge conservatively — a normal page behaving normally is not a "
        "finding, a loud day is not by itself an anomaly, and when in doubt it "
        "fits. The interesting output is monitoring DRIFT, not incident volume.\n\n"
        f"Patterns:\n{PATTERNS}\n"
        + (f"From the org's notes:\n{context}\n\n" if context else "")
        + "Posts:\n"
        + "\n".join(lines)
        + "\n\n"
        'Reply with JSON only: {"findings":[{"ts":"<ts>","author":"<who>",'
        '"pattern":"A1".."A5","severity":"high|low","excerpt":"<=15 words from '
        'the page","why":"<=15 words"}]} — an empty findings list is the '
        "expected answer on most days."
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
        stats.update(rag)
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
        if f.get("pattern") in {f"A{i}" for i in range(1, 6)}
        and f.get("severity") in ("high", "low")
    ]
    return valid, stats


def render(findings: list, scanned: int) -> tuple[str, str]:
    """(#dev card, issue body). One line on a day where every alarm fits."""
    if not findings:
        return (
            f"Scanned **{scanned}** #ops page(s) from the last {LOOKBACK_HOURS}h — "
            "every alarm fits the known patterns.",
            "",
        )
    rows = [
        f"- `{f['pattern']}`/{f['severity']} [{f['ts']}] {f['author']} — "
        f"“{f.get('excerpt', '')[:80]}” — {f.get('why', '')[:80]}"
        for f in findings
    ]
    card = (
        f"**{len(findings)}** #ops page(s) in the last {LOOKBACK_HOURS}h don't fit "
        f"the known alarm patterns (of {scanned} scanned):\n" + "\n".join(rows[:8])
    )
    body = (
        "> _Pages in #ops that don't make sense against the org's accumulated "
        "context — the daily alarm audit, judged with the vault and the pattern "
        "digest in `ops/alarm_audit.py`. A finding is a question, not a verdict: "
        "usually the fix is to retire the alarm, adopt it (name it, give it an "
        "owner), or correct its severity — and a refuted finding is a prompt to "
        "tighten the digest._\n\n"
        + "\n".join(rows)
        + "\n\n**Default:** re-reported while the page keeps firing; no alarm is "
        "silenced on your behalf.\n\n---\n"
        "-# Opened by `ops/alarm_audit.py` (daily). Close when every finding is "
        "resolved or refuted — the next findings reopen a fresh one."
    )
    return card, body


def post_issue(body: str) -> str:
    """The sticky anomaly issue (shared shape: project_review.sticky_issue)."""
    return sticky_issue(
        ALARM_LABEL,
        "FBCA04",
        "A #ops page doesn't fit the org's known alarm patterns",
        "Alarm anomalies — #ops pages that don't fit",
        body,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post", action="store_true", help="post the card to #dev")
    ap.add_argument(
        "--issue",
        action="store_true",
        help="open/refresh the sticky anomaly issue when there are findings",
    )
    args = ap.parse_args()

    if not NOTIFY_SECRET:
        print("NOTIFY_SECRET unset — alarm audit skipped", file=sys.stderr)
        return 0

    try:
        messages = fetch_messages()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            print(
                "deploy-gate has no /dev-log yet (merge discobots#209/#211) — skipped",
                file=sys.stderr,
            )
            return 0
        print(f"could not read #ops: {err}", file=sys.stderr)
        return 1
    except RuntimeError as err:
        if "instead of #ops" in str(err):
            # The lane exists but predates the channel param. Skip loudly in the
            # log, green in the schedule — same doctrine as the 404.
            print(f"{err} — skipped", file=sys.stderr)
            return 0
        print(f"could not read #ops: {err}", file=sys.stderr)
        return 1
    except urllib.error.URLError as err:
        print(f"could not read #ops: {err}", file=sys.stderr)
        return 1

    findings, stats = judge(messages)
    card, body = render(findings, len(messages))
    print(card)

    if args.issue and findings:
        url = post_issue(body)
        print(f"anomaly issue: {url}")
        card += f"\n-# Tracked in {url}"

    if args.post:
        _signed_post(
            "/notify",
            {
                "title": "Daily alarm audit (#ops)",
                "body": card,
                "level": "warn" if findings else "info",
                "telemetry": stats,
            },
        )
        print("posted to #dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
