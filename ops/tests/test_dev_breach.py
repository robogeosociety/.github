"""Tests for the daily #dev breach check.

Three things carry the risk here and the rest is rendering.

The first is the bridge contract: #dev arrives through deploy-gate's /dev-log
lane (discobots#209, which owns and tests pagination, the cutoff walk and embed
folding). This side must pass the window through, reshape timestamps for the
prompt, cap per-message length, and treat a refusal as a refusal — never as a
quiet empty day.

The second is the escalation posture: no findings must mean no issue and an
info-level card; findings must reuse the one open breach issue rather than
opening a new one per day.

The third is that not-yet-configured states — no NOTIFY_SECRET, a gate that
predates its read lane — must skip with exit 0, not fail the schedule.

No network. The gate POST, `_gh` and the CLI are stubbed throughout.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dev_breach as db  # noqa: E402

# ── the bridge contract ──────────────────────────────────────────────────────


def test_the_window_is_passed_to_the_gate():
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        return {"ok": True, "messages": []}

    db.fetch_messages(hours=26, post=fake_post)
    assert calls == [("/dev-log", {"hours": 26, "limit": 300})]


def test_timestamps_reshape_and_long_posts_truncate():
    res = {
        "ok": True,
        "messages": [
            {"ts": "2026-08-16T09:05:00.123000+00:00", "author": "discobot", "text": "x" * 5000}
        ],
    }
    out = db.fetch_messages(post=lambda _p, _b: res)
    assert out[0]["ts"] == "08-16 09:05"
    assert out[0]["author"] == "discobot"
    assert len(out[0]["text"]) == db.MAX_CHARS


def test_a_zulu_timestamp_parses_too():
    # The worker relays Discord's timestamps verbatim; both +00:00 and Z forms
    # must land, or a Discord formatting change silently kills the run.
    res = {"ok": True, "messages": [{"ts": "2026-08-16T09:05:00Z", "author": "a", "text": "t"}]}
    assert db.fetch_messages(post=lambda _p, _b: res)[0]["ts"] == "08-16 09:05"


def test_a_refusal_raises_rather_than_scanning_nothing():
    # {"ok": false} treated as an empty day would report "no breaches" over a
    # day that was never read — the silent-undercount failure, again.
    with pytest.raises(RuntimeError):
        db.fetch_messages(post=lambda _p, _b: {"ok": False, "error": "boom"})


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


def test_a_missing_secret_skips_with_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(db, "NOTIFY_SECRET", "")
    monkeypatch.setattr(sys, "argv", ["dev_breach.py", "--post", "--issue"])
    assert db.main() == 0
    assert "skipped" in capsys.readouterr().err


def test_a_gate_without_the_lane_skips_with_exit_zero(monkeypatch, capsys):
    """The check ships ahead of the worker deploy. Until discobots#209 is live
    the gate answers 404 — a documented state, not a red morning."""
    import urllib.error

    monkeypatch.setattr(db, "NOTIFY_SECRET", "s")
    monkeypatch.setattr(
        db,
        "fetch_messages",
        lambda: (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 404, "not found", {}, None)
        ),
    )
    monkeypatch.setattr(sys, "argv", ["dev_breach.py"])
    assert db.main() == 0
    assert "discobots#209" in capsys.readouterr().err


def test_any_other_read_failure_is_a_real_failure(monkeypatch):
    import urllib.error

    monkeypatch.setattr(db, "NOTIFY_SECRET", "s")
    monkeypatch.setattr(
        db,
        "fetch_messages",
        lambda: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    monkeypatch.setattr(sys, "argv", ["dev_breach.py"])
    assert db.main() == 1
