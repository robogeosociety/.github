"""Tests for the weekly project review.

Two things are worth guarding here and the rest is rendering.

The first is that the report must not UNDERCOUNT. A WIP report that reads the
first page of a board and says "0 breaches" is worse than no report, because it
actively asserts the column is fine. That is not hypothetical: the first version
of load_projects used `items(first: 100)` against a 353-item board and reported
zero breaches; paging it found `the board · Proposals — 13/12` immediately.

The second is the write path. It is the only thing in the script that mutates, it
points at a 353-item board, and Projects v2 has no bulk undo — so every fence
around it is tested: dry-run by default, a per-run cap, and never overwriting a
priority a human already set.

No network. `gql` is stubbed throughout.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import project_review as pr  # noqa: E402


def item(number, repo="discobots", status=None, priority=None, item_id=None):
    fvs = []
    if status:
        fvs.append({"name": status, "field": {"name": "Status"}})
    if priority:
        fvs.append({"name": priority, "field": {"name": "Priority"}})
    return {
        "id": item_id or f"item-{repo}-{number}",
        "fieldValues": {"nodes": fvs},
        "content": {
            "number": number,
            "title": f"t{number}",
            "url": "u",
            "updatedAt": "2026-08-01T00:00:00Z",
            "repository": {"name": repo},
        },
    }


def project(number, title, items, fields=None):
    return {
        "id": f"proj-{number}",
        "number": number,
        "title": title,
        "fields": {"nodes": fields if fields is not None else [{"id": "f1", "name": "Status"}]},
        "items": {"totalCount": len(items), "nodes": items},
    }


POLICY = {
    "staleness": {"pull_request_days": 14, "issue_days": 30, "include_draft_prs": False},
    "projects": [
        {"number": 7, "name": "the board", "limits": {"Proposals": 12}},
        {"number": 8, "name": "References", "limits": {}, "wip_exempt": True},
    ],
    "dormant": [{"number": 5, "name": "template"}],
    "coverage": {"report_unboarded": True, "exempt_repos": []},
}


# ── the undercount ───────────────────────────────────────────────────────────


def test_wip_breach_is_found_when_the_column_is_over():
    items = [item(i, status="Proposals") for i in range(13)]
    breaches = pr.find_wip_breaches([project(7, "the board", items)], POLICY)
    over = [b for b in breaches if b["kind"] == "over-wip"]
    assert over and over[0]["actual"] == 13 and over[0]["limit"] == 12


def test_a_column_at_its_limit_is_not_a_breach():
    items = [item(i, status="Proposals") for i in range(12)]
    assert not [
        b
        for b in pr.find_wip_breaches([project(7, "the board", items)], POLICY)
        if b["kind"] == "over-wip"
    ]


def test_counts_come_from_every_item_not_the_first_page():
    # The regression: 353 items, only 100 read, breach invisible. This asserts the
    # count is taken from the full node list handed in.
    items = [item(i, status="Proposals") for i in range(200)]
    over = [
        b
        for b in pr.find_wip_breaches([project(7, "the board", items)], POLICY)
        if b["kind"] == "over-wip"
    ]
    assert over[0]["actual"] == 200, "counted a truncated page"


def test_an_exempt_board_is_never_reported():
    # References is a shelf, not a queue; a WIP breach every week for a board that
    # is behaving trains people to ignore the section.
    items = [item(i, status="Todo") for i in range(50)]
    assert not pr.find_wip_breaches([project(8, "References", items)], POLICY)


def test_a_board_with_no_policy_is_reported_not_skipped():
    # An unmanaged board is the gap. Silence would hide it.
    breaches = pr.find_wip_breaches([project(99, "mystery", [item(1)])], POLICY)
    assert [b for b in breaches if b["kind"] == "no-policy"]


def test_a_dormant_board_that_revived_is_reported():
    breaches = pr.find_wip_breaches([project(5, "template", [item(1)])], POLICY)
    assert [b for b in breaches if b["kind"] == "dormant-revived"]


def test_a_dormant_board_that_is_still_empty_is_quiet():
    assert not pr.find_wip_breaches([project(5, "template", [])], POLICY)


# ── the write fences ─────────────────────────────────────────────────────────


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def fake_gql(query, **kw):
        calls.append((query, kw))
        if "createProjectV2Field" in query:
            return {
                "createProjectV2Field": {
                    "projectV2Field": {
                        "id": "pf",
                        "options": [{"id": f"o{n}", "name": n} for n in pr.PRIORITIES],
                    }
                }
            }
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}

    monkeypatch.setattr(pr, "gql", fake_gql)
    return calls


PRIORITY_FIELD = [
    {"id": "f1", "name": "Status"},
    {
        "id": "pf",
        "name": "Priority",
        "options": [{"id": f"o{n}", "name": n} for n in pr.PRIORITIES],
    },
]


def test_dry_run_writes_absolutely_nothing(no_network):
    proj = project(7, "the board", [item(1)], fields=PRIORITY_FIELD)
    out = pr.write_priorities([proj], {"discobots#1": "P1"}, apply=False)
    assert out["changed"][0]["dry_run"] is True
    assert not [q for q, _ in no_network if "updateProjectV2ItemFieldValue" in q], "dry run mutated"


def test_apply_writes_and_says_so(no_network):
    proj = project(7, "the board", [item(1)], fields=PRIORITY_FIELD)
    out = pr.write_priorities([proj], {"discobots#1": "P1"}, apply=True)
    assert out["applied"] is True and len(out["changed"]) == 1
    assert [q for q, _ in no_network if "updateProjectV2ItemFieldValue" in q]


def test_a_priority_a_human_set_is_never_overwritten(no_network):
    # Someone who set P0 knew more than this job does. Silently demoting it is the
    # single most annoying thing this could do.
    proj = project(7, "the board", [item(1, priority="P0")], fields=PRIORITY_FIELD)
    out = pr.write_priorities([proj], {"discobots#1": "P3"}, apply=True)
    assert out["changed"] == [] and out["skipped_already_set"] == 1
    assert not [q for q, _ in no_network if "updateProjectV2ItemFieldValue" in q]


def test_the_per_run_cap_holds(no_network, monkeypatch):
    monkeypatch.setattr(pr, "MAX_PRIORITY_WRITES", 5)
    items = [item(i) for i in range(30)]
    proj = project(7, "the board", items, fields=PRIORITY_FIELD)
    ranking = {f"discobots#{i}": "P2" for i in range(30)}
    out = pr.write_priorities([proj], ranking, apply=True)
    # A bad ranking is a 5-item mistake, and next week is a chance to notice.
    assert len(out["changed"]) == 5
    assert out["deferred"] == 25


def test_an_item_with_no_ranking_is_left_alone(no_network):
    proj = project(7, "the board", [item(1), item(2)], fields=PRIORITY_FIELD)
    out = pr.write_priorities([proj], {"discobots#1": "P1"}, apply=True)
    assert [c["key"] for c in out["changed"]] == ["discobots#1"]


def test_a_board_without_the_field_gets_one_only_on_apply(no_network):
    proj = project(7, "the board", [item(1)], fields=[{"id": "f1", "name": "Status"}])
    pr.write_priorities([proj], {"discobots#1": "P1"}, apply=False)
    assert not [q for q, _ in no_network if "createProjectV2Field" in q], (
        "created a field in dry run"
    )
    pr.write_priorities([proj], {"discobots#1": "P1"}, apply=True)
    assert [q for q, _ in no_network if "createProjectV2Field" in q]


def test_field_creation_is_idempotent(no_network):
    proj = project(7, "the board", [item(1)], fields=PRIORITY_FIELD)
    pr.write_priorities([proj], {"discobots#1": "P1"}, apply=True)
    assert not [q for q, _ in no_network if "createProjectV2Field" in q], (
        "recreated an existing field"
    )


# ── rendering ────────────────────────────────────────────────────────────────

EMPTY_STALE = {
    "prs": [],
    "issues": [],
    "failing": [],
    "awaiting_review": [],
    "totals": {"prs": 0, "issues": 0},
}


def test_the_card_says_nothing_was_mutated():
    card, _ = pr.render(EMPTY_STALE, [], {}, [], {})
    assert "Nothing was closed, labelled or commented on" in card


def test_a_quiet_week_still_produces_a_thread():
    _, parts = pr.render(EMPTY_STALE, [], {}, [], {})
    assert parts and "Quiet week" in parts[0]


def test_thread_parts_stay_under_the_discord_limit():
    many = {
        **EMPTY_STALE,
        "issues": [
            {
                "number": i,
                "title": "t" * 60,
                "url": "https://example.com/x",
                "updatedAt": "2026-01-01T00:00:00Z",
                "repository": {"name": "r" * 20},
            }
            for i in range(60)
        ],
        "totals": {"prs": 0, "issues": 60},
    }
    wip = [
        {"kind": "over-wip", "project": "p" * 40, "column": "c" * 20, "actual": 9, "limit": 1}
    ] * 40
    _, parts = pr.render(many, wip, {"orphans": {"a": 3}}, [], {})
    assert parts, "no thread produced"
    for p in parts:
        assert len(p) <= 2000, f"a part is {len(p)} chars — Discord rejects it"


def test_the_write_note_distinguishes_dry_run_from_applied():
    _, dry = pr.render(EMPTY_STALE, [], {}, [], {"changed": [{"key": "a#1"}], "applied": False})
    assert "Would set (dry run)" in dry[-1]
    _, live = pr.render(EMPTY_STALE, [], {}, [], {"changed": [{"key": "a#1"}], "applied": True})
    assert "Set Priority" in live[-1]
