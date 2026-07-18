#!/usr/bin/env python3
"""project_sync.py — reconcile org GitHub Project boards with the true state of the work.

Two jobs, both idempotent and safe to run on a schedule:

  * RECONCILE — for every item already on a board, set Status (and Kind, where the board has
    it) from the item's real content-state. Finished work (merged/closed) is authoritative and
    always lands in Done; OPEN work only gets an INITIAL column when it has no status yet, so a
    hand-curated lane on in-flight work is never overwritten.
  * CAPTURE (`--capture-open`) — add open PRs + issues across the owner's repos that aren't on
    the board yet, then column them by the same rules. Dependabot dep-bumps are skipped.

Per-board Status profile — chosen from the board's own option labels, so different boards
column differently without hardcoding project numbers:

  * lifecycle board (has Ideas / Proposals / Drafts / Done — e.g. org project #7 "the board"):
        merged/closed -> Done ; open ready PR -> Proposals ; open draft PR -> Drafts ;
        open Issue -> Ideas
  * default board (In Progress / Todo / Done):
        merged/closed -> Done ; open PR -> In Progress ; open Issue -> In Progress if assigned
        else Todo

Only options that already exist on a board are ever written; a missing field/option is skipped,
never mutated blindly. Matching is case-insensitive on the option label.

    project_sync.py --owner robogeosociety --project 7 --capture-open   # the org master board
    project_sync.py --dry-run                                           # report, write nothing
    project_sync.py --quiet --throttle 900                              # hook mode

Auth + transport is the `gh` CLI (its token, its GraphQL endpoint). Writing an *org* Projects v2
board needs a token with `project` scope — in CI, set GH_TOKEN to a project-scoped PAT/App token.
No third-party deps.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

STATE = Path.home() / ".claude" / "state" / "project-sync.last"

KIND_FOR = {  # desired Kind label by conventional-commit prefix
    "feat": "Feature",
    "feature": "Feature",
    "fix": "Fix",
    "bugfix": "Fix",
    "hotfix": "Fix",
    "docs": "Docs",
    "doc": "Docs",
    "chore": "Infra",
    "ci": "Infra",
    "build": "Infra",
    "refactor": "Infra",
    "perf": "Infra",
    "test": "Infra",
    "style": "Infra",
    "infra": "Infra",
}

# A board is a "lifecycle" board when it carries these status columns.
_LIFECYCLE_OPTS = {"ideas", "proposals", "drafts"}

# Shared item selection: current single-select values + content-state. Reused by the first-page
# fetch and the cursor-paginated follow-up so both see identical shapes.
_ITEM_NODE = """
  id
  fieldValues(first: 30) { nodes {
    ... on ProjectV2ItemFieldSingleSelectValue {
      optionId field { ... on ProjectV2SingleSelectField { name } }
    }
  } }
  content {
    __typename
    ... on PullRequest {
      number title state isDraft merged
      assignees { totalCount } repository { nameWithOwner }
    }
    ... on Issue {
      number title state assignees { totalCount } repository { nameWithOwner }
    }
    ... on DraftIssue { title }
  }
"""

_ITEMS_FRAG = (
    """
  projectsV2(first: 25) {
    nodes {
      id title number
      fields(first: 50) { nodes {
        ... on ProjectV2SingleSelectField { id name options { id name } }
      } }
      items(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {"""
    + _ITEM_NODE
    + """}
      }
    }
  }
"""
)

_ITEMS_PAGE = (
    """
query($id:ID!,$after:String){
  node(id:$id){ ... on ProjectV2 {
    items(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + _ITEM_NODE
    + """}
    }
  } }
}
"""
)

_MUTATION = """
mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
  updateProjectV2ItemFieldValue(
    input:{projectId:$p, itemId:$i, fieldId:$f, value:{singleSelectOptionId:$o}}
  ){ projectV2Item { id } }
}"""


def gh_graphql(query: str, **vars_) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in vars_.items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "gh graphql failed")
    return json.loads(out.stdout)


def gh_json(args: list[str]) -> object | None:
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout) if out.stdout.strip() else None


def owners() -> list[str]:
    d = gh_graphql("{ viewer { login organizations(first:25){ nodes{ login } } } }")[
        "data"
    ]["viewer"]
    return [d["login"]] + [o["login"] for o in d["organizations"]["nodes"]]


def _paginate_items(project: dict) -> None:
    """Pull every remaining page of a board's items into project['items']['nodes'] in place.
    Boards can outgrow the 100-item first page (org #7 alone is 250+)."""
    page = project["items"]
    while page["pageInfo"]["hasNextPage"]:
        nxt = gh_graphql(
            _ITEMS_PAGE, id=project["id"], after=page["pageInfo"]["endCursor"]
        )
        page = nxt["data"]["node"]["items"]
        project["items"]["nodes"] += page["nodes"]


def fetch_projects(owner: str) -> list[dict]:
    """All of an owner's projects with items (fully paginated) — org root, then user root."""
    for root in ("organization", "user"):
        try:
            q = f"query($login:String!){{ {root}(login:$login){{ {_ITEMS_FRAG} }} }}"
            data = gh_graphql(q, login=owner).get("data", {})
            node = data.get(root)
            if node:
                projects = node["projectsV2"]["nodes"]
                for p in projects:
                    _paginate_items(p)
                return projects
        except RuntimeError:
            continue
    return []


def _lifecycle(status_field: dict) -> bool:
    opts = {o["name"].lower() for o in status_field["options"]}
    return _LIFECYCLE_OPTS <= opts


def desired_status(c: dict, lifecycle: bool) -> tuple[str, bool] | None:
    """Return (label, terminal). `terminal` (finished → Done) is authoritative and may overwrite
    an existing column; a non-terminal label is an INITIAL column only, applied solely when the
    item has no status yet."""
    t = c.get("__typename")
    if t == "PullRequest":
        if c["merged"] or c["state"] == "CLOSED":
            return ("Done", True)
        if lifecycle:
            return ("Drafts", False) if c["isDraft"] else ("Proposals", False)
        return ("In Progress", False)
    if t == "Issue":
        if c["state"] == "CLOSED":
            return ("Done", True)
        if lifecycle:
            return ("Ideas", False)
        assigned = (c.get("assignees") or {}).get("totalCount", 0) > 0
        return ("In Progress", False) if assigned else ("Todo", False)
    return None


def desired_kind(c: dict) -> str | None:
    title = c.get("title", "")
    prefix = (
        title.split(":", 1)[0].split("(", 1)[0].strip().lower() if ":" in title else ""
    )
    return KIND_FOR.get(prefix)


def _select_field(project: dict, name: str) -> dict | None:
    for f in project["fields"]["nodes"]:
        if f and f.get("name", "").lower() == name.lower() and "options" in f:
            return f
    return None


def _option_id(field: dict, label: str) -> str | None:
    for o in field["options"]:
        if o["name"].lower() == label.lower():
            return o["id"]
    return None


def _current_option(item: dict, field_name: str) -> str | None:
    for fv in item["fieldValues"]["nodes"]:
        f = fv.get("field") or {}
        if f.get("name", "").lower() == field_name.lower():
            return fv.get("optionId")
    return None


def _set(project, item, field, label, fname, want, current, changes, apply):
    """Set one single-select field to `want` if the board has that option and it differs."""
    opt = _option_id(field, want)
    if not opt or current == opt:
        return  # board lacks the option, or already correct — no write
    changes.append(f"{label}: {fname} -> {want}")
    if apply:
        gh_graphql(_MUTATION, p=project["id"], i=item["id"], f=field["id"], o=opt)


def _label(c: dict) -> str:
    if c.get("number"):
        return f"{c.get('repository', {}).get('nameWithOwner', '?').split('/')[-1]}#{c['number']}"
    return c.get("title", "draft")[:24]


def reconcile(project: dict, *, apply: bool) -> list[str]:
    changes: list[str] = []
    status_f = _select_field(project, "Status")
    kind_f = _select_field(project, "Kind")
    lifecycle = bool(status_f) and _lifecycle(status_f)
    for item in project["items"]["nodes"]:
        c = item.get("content") or {}
        label = _label(c)
        if status_f:
            cur = _current_option(item, "Status")
            ds = desired_status(c, lifecycle)
            if ds:
                want, terminal = ds
                # Terminal (Done) is authoritative; a non-terminal label is only an INITIAL
                # column, applied solely when the item has no status yet — never re-columning
                # curated open work.
                if terminal or cur is None:
                    _set(
                        project,
                        item,
                        status_f,
                        label,
                        "Status",
                        want,
                        cur,
                        changes,
                        apply,
                    )
        if kind_f:
            cur = _current_option(item, "Kind")
            dk = desired_kind(c)
            if dk and cur is None:  # fill only — preserve any hand-set Kind
                _set(project, item, kind_f, label, "Kind", dk, cur, changes, apply)
    return changes


def _search_open(owner: str, kind: str) -> list[dict]:
    """Open PRs or issues across the owner's repos, via `gh search`."""
    fields = "url,repository,number,author" + (",isDraft" if kind == "prs" else "")
    query = ["is:issue"] if kind == "issues" else []
    data = gh_json(
        [
            "search",
            kind,
            *query,
            "--owner",
            owner,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            fields,
        ]
    )
    return data or []


def capture_open(owner: str, project: dict, *, apply: bool) -> list[str]:
    """Add open PRs/issues not yet on the board, columning each by the board's profile.
    Idempotent: items already present are skipped; dependabot dep-bumps are excluded."""
    status_f = _select_field(project, "Status")
    if not status_f:
        return []
    lifecycle = _lifecycle(status_f)
    present = {
        f"{c['repository']['nameWithOwner']}#{c['number']}"
        for item in project["items"]["nodes"]
        for c in [item.get("content") or {}]
        if c.get("number") and c.get("repository")
    }
    added: list[str] = []
    for kind in ("prs", "issues"):
        for it in _search_open(owner, kind):
            if (it.get("author") or {}).get("login") == "dependabot[bot]":
                continue
            if (
                kind == "issues" and "/pull/" in it["url"]
            ):  # guard: search can return PRs
                continue
            key = f"{it['repository']['nameWithOwner']}#{it['number']}"
            if key in present:
                continue
            if kind == "prs":
                want = (
                    ("Drafts" if it.get("isDraft") else "Proposals")
                    if lifecycle
                    else "In Progress"
                )
            else:
                want = "Ideas" if lifecycle else "Todo"
            added.append(f"+ {key} -> {want}")
            if not apply:
                continue
            res = gh_json(
                [
                    "project",
                    "item-add",
                    str(project["number"]),
                    "--owner",
                    owner,
                    "--url",
                    it["url"],
                    "--format",
                    "json",
                ]
            )
            iid = (res or {}).get("id")
            opt = _option_id(status_f, want)
            if iid and opt:
                gh_graphql(_MUTATION, p=project["id"], i=iid, f=status_f["id"], o=opt)
    return added


def capture_local() -> list[str]:
    """Best-effort snapshot of local work in the current checkout (worktrees + branch)."""
    wt = subprocess.run(["git", "worktree", "list"], capture_output=True, text=True)
    return (
        wt.stdout.strip().splitlines()
        if wt.returncode == 0 and wt.stdout.strip()
        else []
    )


def repo_owner() -> str | None:
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "owner", "-q", ".owner.login"],
        capture_output=True,
        text=True,
    )
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile GitHub Project boards with real work state."
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="report changes, write nothing"
    )
    ap.add_argument(
        "--quiet", action="store_true", help="only print a one-line summary"
    )
    ap.add_argument(
        "--throttle",
        type=int,
        default=0,
        help="skip if a run succeeded within N seconds",
    )
    ap.add_argument("--owner", help="limit to one owner login")
    ap.add_argument(
        "--project", type=int, help="limit to one project number (with --owner)"
    )
    ap.add_argument(
        "--capture-open",
        action="store_true",
        help="also add open PRs/issues not yet on the board (needs --owner)",
    )
    ap.add_argument(
        "--only-owner-of-cwd",
        action="store_true",
        help="hook guard: no-op unless the cwd repo's owner is one of mine",
    )
    args = ap.parse_args()

    if (
        args.throttle
        and STATE.exists()
        and (time.time() - STATE.stat().st_mtime) < args.throttle
    ):
        if not args.quiet:
            print(f"project-sync: throttled (last run <{args.throttle}s ago)")
        return 0

    try:
        mine = [args.owner] if args.owner else owners()
    except RuntimeError as e:
        print(f"project-sync: cannot reach GitHub ({e})", file=sys.stderr)
        return 0  # never fail a hook over a transient gh/network error

    if args.only_owner_of_cwd and repo_owner() not in mine:
        return 0  # not one of my repos — silently do nothing

    if not args.quiet:
        for line in capture_local():
            print(f"  worktree: {line}")

    all_changes: list[str] = []
    for owner in mine:
        try:
            projects = fetch_projects(owner)
        except RuntimeError as e:
            print(f"project-sync: {owner}: {e}", file=sys.stderr)
            continue
        for p in projects:
            if args.project and p["number"] != args.project:
                continue
            changes = (
                capture_open(owner, p, apply=not args.dry_run)
                if args.capture_open
                else []
            )
            changes += reconcile(p, apply=not args.dry_run)
            if changes and not args.quiet:
                verb = "would set" if args.dry_run else "set"
                print(f"[{owner}/{p['title']} #{p['number']}] {verb}:")
                for ch in changes:
                    print(f"    {ch}")
            all_changes += changes

    verb = "would update" if args.dry_run else "updated"
    print(
        f"project-sync: {verb} {len(all_changes)} field(s) across {len(mine)} owner(s)."
    )
    if not args.dry_run and args.throttle:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.touch()  # record every real run (even clean) so the throttle window holds
    return 0


if __name__ == "__main__":
    sys.exit(main())
