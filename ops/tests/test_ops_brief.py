"""Tests for the weekly ops brief and the `parked` staleness exemption.

The brief is the inversion-of-control surface: the one issue where the org asks
its operator questions instead of feeding him a report. Three things matter and
are guarded here.

First, every decision must carry a default — a question without one is a nag,
and the whole contract is "silence applies the default". Second, a week with no
decisions must open NO issue: a brief that appears weekly to say "nothing needs
you" trains its reader to stop opening briefs. Third, the posting must be
idempotent within a week (a rerun edits, it does not re-notify) and supersede
across weeks (one open brief, ever).

`parked` is tested alongside because it is the brief's main lever: the exemption
is what makes "label it parked" actually move something.

No network. `gql` and `_gh` are stubbed throughout.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import project_review as pr  # noqa: E402


def open_item(number, days_idle=40, labels=(), draft=False, repo="discobots"):
    from datetime import UTC, datetime, timedelta

    when = (datetime.now(UTC) - timedelta(days=days_idle)).isoformat()
    return {
        "number": number,
        "title": f"t{number}",
        "url": "https://example.com/x",
        "updatedAt": when,
        "isDraft": draft,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "repository": {"name": repo},
    }


POLICY = {
    "staleness": {
        "pull_request_days": 14,
        "issue_days": 30,
        "include_draft_prs": False,
        "exempt_labels": ["parked"],
    }
}


# ── parked: the lever must move something ────────────────────────────────────


def test_a_parked_item_is_not_reported_stale(monkeypatch):
    items = {
        "pr": [open_item(1), open_item(2, labels=["parked"])],
        "issue": [open_item(3), open_item(4, labels=["parked"])],
    }
    monkeypatch.setattr(pr, "all_open", lambda kind: items[kind])
    stale = pr.find_stale(POLICY)
    assert [p["number"] for p in stale["prs"]] == [1]
    assert [i["number"] for i in stale["issues"]] == [3]
    # Totals still count parked work — exemption hides the nag, not the number.
    assert stale["totals"] == {"prs": 2, "issues": 2}


def test_a_parked_pr_is_not_nagged_for_review_either(monkeypatch):
    waiting = open_item(5, days_idle=10, labels=["parked"])
    waiting["reviewDecision"] = "REVIEW_REQUIRED"
    monkeypatch.setattr(pr, "all_open", lambda kind: [waiting] if kind == "pr" else [])
    assert pr.find_stale(POLICY)["awaiting_review"] == []


def test_no_exempt_labels_config_changes_nothing(monkeypatch):
    # Older policy files have no `exempt_labels`; they must keep working as-is.
    monkeypatch.setattr(
        pr, "all_open", lambda kind: [open_item(1, labels=["parked"])] if kind == "pr" else []
    )
    bare = {"staleness": {"pull_request_days": 14, "issue_days": 30}}
    assert [p["number"] for p in pr.find_stale(bare)["prs"]] == [1]


# ── the brief: decisions with defaults ───────────────────────────────────────

EMPTY_STALE = {
    "prs": [],
    "issues": [],
    "failing": [],
    "awaiting_review": [],
    "totals": {"prs": 0, "issues": 0},
}


def stale_with(prs=(), issues=()):
    return {**EMPTY_STALE, "prs": list(prs), "issues": list(issues)}


def test_every_decision_carries_a_default():
    stale = stale_with(prs=[open_item(1)])
    wip = [{"kind": "over-wip", "project": "the board", "column": "Proposals",
            "actual": 13, "limit": 12}]
    ranked = [{"key": "discobots#1", "priority": "P1", "why": "w"}]
    _, body, n = pr.render_brief(stale, wip, ranked)
    assert n == 3
    # The contract itself, stated up front.
    assert "applies its default" in body
    # One default per decision, no exceptions.
    assert body.count("**Default:**") == n


def test_a_quiet_week_opens_no_issue():
    _, _, n = pr.render_brief(EMPTY_STALE, [], [])
    assert n == 0


def test_fyi_items_are_not_decisions():
    waiting = open_item(9)
    waiting["reviewDecision"] = "REVIEW_REQUIRED"
    stale = {**EMPTY_STALE, "awaiting_review": [waiting]}
    _, body, n = pr.render_brief(stale, [], [])
    # Waiting-on-review is surfaced but costs no decision — and alone it is not
    # enough to open the issue.
    assert n == 0


def test_unmanaged_boards_are_a_decision():
    wip = [
        {"kind": "no-policy", "project": "mystery", "number": 99},
        {"kind": "dormant-revived", "project": "template", "count": 3},
    ]
    _, body, n = pr.render_brief(EMPTY_STALE, wip, [])
    assert n == 1
    assert "mystery" in body and "template" in body


def test_the_brief_names_its_levers():
    stale = stale_with(prs=[open_item(1)])
    _, body, _ = pr.render_brief(stale, [], [])
    # The levers are the org's existing machinery, named so they get used.
    assert "`parked`" in body and "`human-task`" in body and "@claude" in body


# ── posting: idempotent in-week, superseding across weeks ────────────────────


@pytest.fixture
def gh_calls(monkeypatch):
    calls = []

    def fake_gh(args, input=None):
        calls.append((args, input))
        if args[:2] == ["issue", "list"]:
            return json.dumps(fake_gh.open_briefs)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/robogeosociety/.github/issues/50\n"
        return ""

    fake_gh.open_briefs = []
    monkeypatch.setattr(pr, "_gh", fake_gh)
    return calls


def test_a_fresh_week_creates_one_labelled_issue(gh_calls):
    url = pr.post_brief("Ops brief — week of 2026-08-17", "body")
    assert url.endswith("/issues/50")
    create = next(a for a, _ in gh_calls if a[:2] == ["issue", "create"])
    assert "ops-brief" in create and "human-task" in create


def test_a_rerun_in_the_same_week_edits_instead_of_renotifying(gh_calls, monkeypatch):
    title = "Ops brief — week of 2026-08-17"
    pr._gh.open_briefs = [{"number": 42, "title": title}]
    url = pr.post_brief(title, "updated body")
    assert url.endswith("/issues/42")
    ops = [a[:2] for a, _ in gh_calls]
    assert ["issue", "edit"] in ops
    assert ["issue", "create"] not in ops and ["issue", "close"] not in ops


def test_a_new_week_supersedes_the_old_brief(gh_calls):
    pr._gh.open_briefs = [{"number": 42, "title": "Ops brief — week of 2026-08-10"}]
    pr.post_brief("Ops brief — week of 2026-08-17", "body")
    ops = [a[:2] for a, _ in gh_calls]
    assert ["issue", "close"] in ops and ["issue", "create"] in ops


def test_labels_are_ensured_before_the_issue_needs_them(gh_calls):
    pr.post_brief("Ops brief — week of 2026-08-17", "body")
    first_issue_op = next(i for i, (a, _) in enumerate(gh_calls) if a[0] == "issue")
    label_ops = [i for i, (a, _) in enumerate(gh_calls) if a[0] == "label"]
    assert label_ops and max(label_ops) < first_issue_op
