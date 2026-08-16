"""Tests for the daily #dev breach check.

Three things carry the risk here and the rest is rendering.

The first is the fetch window: Discord pages newest-first, so a paging bug
either re-reads history forever or — worse — quietly scans only the first page
and reports "no breaches" over a day it mostly never saw. Silent undercoverage
is the same failure the weekly review guards against, so the walk is pinned.

The second is that most of #dev's signal lives in bot EMBEDS with empty
`content`. A reader that only looks at content would scan a near-empty channel
and honestly report nothing — tested so it can't regress to that.

The third is the escalation posture: no findings must mean no issue and an
info-level card; findings must reuse the one open breach issue rather than
opening a new one per day. And an unconfigured environment must skip cleanly,
not fail the schedule.

No network. The Discord GET, `_gh` and the CLI are stubbed throughout.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dev_breach as db  # noqa: E402


def msg(mid, hours_ago=1.0, content="", embeds=(), author="discobot"):
    ts = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {
        "id": str(mid),
        "timestamp": ts.isoformat(),
        "content": content,
        "embeds": list(embeds),
        "author": {"username": author},
    }


# ── the fetch window ─────────────────────────────────────────────────────────


def test_pagination_walks_past_the_first_page():
    # 100 fresh messages, then a second page with more — a single-page reader
    # would scan the first hundred and honestly report a day it never saw.
    pages = [
        [msg(i, hours_ago=1, content=f"m{i}") for i in range(100)],
        [msg(100, hours_ago=2, content="older-but-in-window")],
    ]
    calls = []

    def fake_get(url):
        calls.append(url)
        return pages[len(calls) - 1]

    out = db.fetch_messages(hours=26, get=fake_get)
    assert len(out) == 101
    assert "before=99" in calls[1], "did not page with the last message id"


def test_the_cutoff_ends_the_walk():
    pages = [[msg(1, hours_ago=1, content="fresh"), msg(2, hours_ago=50, content="ancient")]]
    calls = []

    def fake_get(url):
        calls.append(url)
        return pages[0]

    out = db.fetch_messages(hours=26, get=fake_get)
    assert [m["text"] for m in out] == ["fresh"]
    assert len(calls) == 1, "kept paging past the cutoff"


def test_messages_come_back_oldest_first():
    page = [msg(1, hours_ago=1, content="newest"), msg(2, hours_ago=3, content="oldest")]
    out = db.fetch_messages(hours=26, get=lambda _u: page)
    assert [m["text"] for m in out] == ["oldest", "newest"]


# ── embeds are the signal ────────────────────────────────────────────────────


def test_bot_embeds_are_read_not_skipped():
    page = [
        msg(1, embeds=[{"title": "Deploy approved", "description": "outside the gate"}]),
        msg(2),  # truly empty: no content, no embeds — carries nothing
    ]
    out = db.fetch_messages(hours=26, get=lambda _u: page)
    assert len(out) == 1
    assert "Deploy approved" in out[0]["text"] and "outside the gate" in out[0]["text"]


def test_long_posts_are_truncated_not_forwarded_whole():
    page = [msg(1, content="x" * 5000)]
    out = db.fetch_messages(hours=26, get=lambda _u: page)
    assert len(out[0]["text"]) == db.MAX_CHARS


# ── judgment fences ──────────────────────────────────────────────────────────


def test_a_missing_cli_skips_judgment_rather_than_failing(monkeypatch):
    monkeypatch.setattr(db.shutil, "which", lambda _n: None)
    called = []
    monkeypatch.setattr(db.subprocess, "run", lambda *a, **k: called.append(a))
    findings, stats = db.judge([{"ts": "t", "author": "a", "text": "x"}])
    assert findings == [] and stats == {} and not called


def test_findings_with_unknown_rules_or_severities_are_dropped(monkeypatch):
    monkeypatch.setattr(db.shutil, "which", lambda _n: "/bin/claude")

    class Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "result": json.dumps(
                    {
                        "findings": [
                            {"ts": "t", "author": "a", "rule": "R2", "severity": "high"},
                            {"ts": "t", "author": "a", "rule": "R9", "severity": "high"},
                            {"ts": "t", "author": "a", "rule": "R1", "severity": "medium"},
                        ]
                    }
                ),
                "usage": {},
            }
        )

    monkeypatch.setattr(db.subprocess, "run", lambda *a, **k: Proc())
    findings, _ = db.judge([{"ts": "t", "author": "a", "text": "x"}])
    assert [f["rule"] for f in findings] == ["R2"]


# ── escalation posture ───────────────────────────────────────────────────────


def test_a_quiet_day_renders_a_one_liner_and_no_issue_body():
    card, body = db.render([], scanned=57)
    assert "no workflow breaches" in card and "57" in card
    assert body == ""


FINDING = {
    "ts": "08-16 09:00",
    "author": "discobot",
    "rule": "R2",
    "severity": "high",
    "excerpt": "waiting on Tommy for the CF token",
    "why": "pending-operator with no issue",
}


def test_findings_carry_rule_and_default_in_the_issue_body():
    card, body = db.render([FINDING], scanned=57)
    assert "R2" in card
    assert "**Default:**" in body, "a breach issue without a default is a nag"
    assert "not a verdict" in body


@pytest.fixture
def gh_calls(monkeypatch):
    calls = []

    def fake_gh(args, input=None):
        calls.append((args, input))
        if args[:2] == ["issue", "list"]:
            return json.dumps(fake_gh.open_issues)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/robogeosociety/.github/issues/60\n"
        return ""

    fake_gh.open_issues = []
    monkeypatch.setattr(db, "_gh", fake_gh)
    return calls


def test_first_findings_open_one_labelled_issue(gh_calls):
    url = db.post_issue("body")
    assert url.endswith("/issues/60")
    create = next(a for a, _ in gh_calls if a[:2] == ["issue", "create"])
    assert "workflow-breach" in create and "human-task" in create


def test_later_findings_refresh_the_open_issue_not_a_new_one(gh_calls):
    db._gh.open_issues = [{"number": 58}]
    url = db.post_issue("body")
    assert url.endswith("/issues/58")
    ops = [a[:2] for a, _ in gh_calls]
    assert ["issue", "edit"] in ops and ["issue", "create"] not in ops


# ── unconfigured is a state, not a failure ───────────────────────────────────


def test_missing_credentials_skip_with_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(db, "BOT_TOKEN", "")
    monkeypatch.setattr(db, "CHANNEL_ID", "")
    monkeypatch.setattr(sys, "argv", ["dev_breach.py", "--post", "--issue"])
    assert db.main() == 0
    assert "skipped" in capsys.readouterr().err
