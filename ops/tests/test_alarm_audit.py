"""Tests for the daily #ops alarm audit.

Most of the machinery is shared with dev_breach and tested there (the sticky
issue) or in the worker (paging, embeds). What is load-bearing HERE is the
channel contract: the audit must ask the gate for #ops, and must refuse to
scan anything else. A gate that predates the channel param silently serves
#dev — auditing the wrong channel and reporting "every alarm fits" is the
exact anomaly class this job exists to catch, so that reply must be a named
skip, never a scan.

No network. The gate POST and the CLI are stubbed throughout.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import alarm_audit as aa  # noqa: E402

# ── the channel contract ─────────────────────────────────────────────────────


def test_the_audit_asks_for_ops():
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        return {"ok": True, "channel": "#ops", "messages": []}

    aa.fetch_messages(hours=26, post=fake_post)
    assert calls == [("/dev-log", {"hours": 26, "limit": aa.FETCH_LIMIT, "channel": "ops"})]


def test_a_gate_serving_the_wrong_channel_is_refused():
    res = {"ok": True, "channel": "#dev", "messages": [{"ts": "x", "author": "a", "text": "t"}]}
    with pytest.raises(RuntimeError, match="instead of #ops"):
        aa.fetch_messages(post=lambda _p, _b: res)


def test_the_wrong_channel_skips_the_run_with_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(aa, "NOTIFY_SECRET", "s")
    monkeypatch.setattr(
        aa,
        "fetch_messages",
        lambda: (_ for _ in ()).throw(RuntimeError("gate served #dev instead of #ops — deploy")),
    )
    monkeypatch.setattr(sys, "argv", ["alarm_audit.py"])
    assert aa.main() == 0
    assert "skipped" in capsys.readouterr().err


def test_messages_reshape_like_the_breach_check():
    res = {
        "ok": True,
        "channel": "#ops",
        "messages": [{"ts": "2026-08-16T09:05:00Z", "author": "opsbot", "text": "y" * 5000}],
    }
    out = aa.fetch_messages(post=lambda _p, _b: res)
    assert out[0]["ts"] == "08-16 09:05" and len(out[0]["text"]) == aa.MAX_CHARS


# ── judgment fences ──────────────────────────────────────────────────────────


def test_findings_outside_the_pattern_set_are_dropped(monkeypatch):
    monkeypatch.setattr(aa.shutil, "which", lambda _n: "/bin/claude")
    monkeypatch.setattr(aa, "vault_context", lambda _q: ("", {}))

    class Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "result": json.dumps(
                    {
                        "findings": [
                            {"ts": "t", "author": "a", "pattern": "A4", "severity": "low"},
                            {"ts": "t", "author": "a", "pattern": "A9", "severity": "low"},
                            {"ts": "t", "author": "a", "pattern": "R1", "severity": "high"},
                        ]
                    }
                ),
                "usage": {},
            }
        )

    monkeypatch.setattr(aa.subprocess, "run", lambda *a, **k: Proc())
    findings, _ = aa.judge([{"ts": "t", "author": "a", "text": "x"}])
    assert [f["pattern"] for f in findings] == ["A4"]


def test_a_dead_vault_does_not_kill_the_audit(monkeypatch):
    # vault_context already degrades to ("", {}) on failure; the audit must
    # accept the empty context and still judge the window alone.
    monkeypatch.setattr(aa.shutil, "which", lambda _n: "/bin/claude")
    monkeypatch.setattr(aa, "vault_context", lambda _q: ("", {}))
    prompts = []

    class Proc:
        returncode = 0
        stdout = json.dumps({"result": json.dumps({"findings": []}), "usage": {}})

    def fake_run(*a, **k):
        prompts.append(k.get("input") or "")
        return Proc()

    monkeypatch.setattr(aa.subprocess, "run", fake_run)
    findings, _ = aa.judge([{"ts": "t", "author": "a", "text": "x"}])
    assert findings == []
    assert "From the org's notes" not in prompts[0]


# ── escalation posture ───────────────────────────────────────────────────────


def test_a_day_where_every_alarm_fits_opens_no_issue():
    card, body = aa.render([], scanned=44)
    assert "every alarm fits" in card and "44" in card
    assert body == ""


def test_findings_carry_pattern_and_default():
    f = {
        "ts": "08-16 09:00",
        "author": "opsbot",
        "pattern": "A1",
        "severity": "high",
        "excerpt": "dev-status :8077 unreachable",
        "why": "dev-status was retired 2026-07-22",
    }
    card, body = aa.render([f], scanned=44)
    assert "A1" in card
    assert "**Default:**" in body and "not a verdict" in body
