#!/usr/bin/env python3
"""Create the missing project boards and place the org's unboarded work on them.

Surveyed 2026-08-08: `tommybot` had a board to itself (#3, 44 items) while four
clusters of active repos had none, and 51 open items sat on no board at all.
Adding them to #7 was not the alternative — it already holds 354 items and is over
its Proposals limit.

  maps    64 issues / 3 PRs   robot-geographical-society, maps, pointcollector, discord-maps
  sites   24 issues / 7 PRs   walksheds + its two wikis, vitamind, intentcity, …
  models  24 issues / 5 PRs   is-the-mountain-out, transit-tracker-tui, tallest-tree,
                              wikipedia-local
  fleet    8 issues / 8 PRs   supervisor — not even in repo-categories.tsv, and its
                              work was split across three boards

Idempotent throughout: a board that exists is reused, a field that exists is not
recreated, an item already on the board is not added twice. Safe to re-run when a
repo is added to a cluster.

  board_bootstrap.py            # plan only, writes nothing
  board_bootstrap.py --apply    # create boards, add fields, place items
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime

ORG = "robogeosociety"

# Clusters follow scripts/repo-categories.tsv, with two deliberate departures:
# `tommybot` is left out of models because it already has board #3, and `supervisor`
# gets its own board despite being absent from the taxonomy entirely — which is
# itself the finding. Add it to the TSV when this lands.
BOARDS = {
    "maps": ["robot-geographical-society", "maps", "pointcollector", "discord-maps"],
    "sites": [
        "walksheds",
        "walksheds-wiki",
        "walksheds-dev-wiki",
        "vitamind",
        "intentcity",
        "JudkinsParkForPeople",
        "Bathymetryst",
        "sporty40",
        # NOT tommyroar.github.io: repo-categories.tsv lists it under `web`, but it
        # lives at tommyroar/tommyroar.github.io — a personal repo, not an org one.
        # An org project cannot hold it. Left here as a note rather than a silent
        # omission, since the TSV is what someone will check next.
    ],
    "models": ["is-the-mountain-out", "transit-tracker-tui", "tallest-tree", "wikipedia-local"],
    "fleet": ["supervisor"],
}

# Matches the vocabulary already on #3 and #4, so a priority means the same thing on
# every board rather than becoming a second dialect.
PRIORITY_OPTIONS = [
    ("P0", "RED", "Blocks something already running"),
    ("P1", "ORANGE", "This week"),
    ("P2", "YELLOW", "Real, but can wait"),
    ("P3", "GRAY", "Someday"),
]

# The stale flag. A board that arrives holding a month-old backlog looks like a
# board with a lot of work on it; this is what makes the difference visible on day
# one. robot-geographical-society alone brings 59 issues, most of them a month old
# and described by the daily check-in as "a graveyard".
FRESHNESS_OPTIONS = [
    ("fresh", "GREEN", "Touched inside the policy window"),
    ("stale", "GRAY", "Past the policy threshold — 14d for PRs, 30d for issues"),
]

STALE_PR_DAYS = 14
STALE_ISSUE_DAYS = 30


def gql(query: str, **variables) -> dict:
    """GraphQL via a JSON request body, not `-F` key=value.

    `gh api graphql -F opts=<json>` sends the JSON as a STRING, and the API rejects
    it: `Variable $opts of type [ProjectV2SingleSelectFieldOptionInput!]! was
    provided invalid value`. That only bites on a mutation carrying a structured
    variable — creating a single-select field — so it survived every read and every
    scalar-only write, and would have surfaced first on the scheduled run that
    creates a Priority field on a board lacking one.

    Posting {query, variables} as the body handles every type the schema has.
    """
    body = json.dumps({"query": query, "variables": variables})
    proc = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=body,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"graphql failed: {proc.stderr.strip()[:400]}")
    out = json.loads(proc.stdout)
    if out.get("errors"):
        raise RuntimeError(f"graphql errors: {json.dumps(out['errors'])[:400]}")
    return out["data"]


def org_id() -> str:
    return gql("query($o:String!){ organization(login:$o){ id } }", o=ORG)["organization"]["id"]


def existing_projects() -> dict:
    """Title → {id, number, fields, item content keys}. Items are paged: a board read
    off the first page silently looks emptier than it is, which is how the weekly
    review first reported zero WIP breaches against a 353-item board."""
    nodes = gql(
        """query($o:String!){ organization(login:$o){ projectsV2(first:20){ nodes{
             id number title
             fields(first:30){ nodes{
               ... on ProjectV2FieldCommon { id name }
               ... on ProjectV2SingleSelectField { id name options{ id name } } } }
             items(first:1){ totalCount } } } } }""",
        o=ORG,
    )["organization"]["projectsV2"]["nodes"]

    out = {}
    for p in nodes:
        keys, cursor = set(), None
        while len(keys) < p["items"]["totalCount"]:
            page = gql(
                """query($id:ID!,$after:String){ node(id:$id){ ... on ProjectV2 {
                     items(first:100, after:$after){ pageInfo{ hasNextPage endCursor }
                       nodes{ content{
                         ... on Issue { number repository{name} }
                         ... on PullRequest { number repository{name} } } } } } } }""",
                id=p["id"],
                after=cursor or "",
            )["node"]["items"]
            for it in page["nodes"]:
                c = it.get("content") or {}
                if c.get("number"):
                    keys.add(f"{c['repository']['name']}#{c['number']}")
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        out[p["title"]] = {
            "id": p["id"],
            "number": p["number"],
            "fields": p["fields"]["nodes"],
            "keys": keys,
        }
    return out


def open_items(repo: str) -> list:
    """Every open issue and PR in a repo, with its age. Paged for the same reason."""
    out, cursor = [], None
    while True:
        page = gql(
            """query($o:String!,$r:String!,$after:String){ repository(owner:$o,name:$r){
                 issues(states:OPEN, first:100, after:$after){
                   pageInfo{ hasNextPage endCursor }
                   nodes{ id number title updatedAt } } } }""",
            o=ORG,
            r=repo,
            after=cursor or "",
        )["repository"]["issues"]
        out += [{**n, "kind": "issue", "repo": repo} for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    cursor = None
    while True:
        page = gql(
            """query($o:String!,$r:String!,$after:String){ repository(owner:$o,name:$r){
                 pullRequests(states:OPEN, first:100, after:$after){
                   pageInfo{ hasNextPage endCursor }
                   nodes{ id number title updatedAt } } } }""",
            o=ORG,
            r=repo,
            after=cursor or "",
        )["repository"]["pullRequests"]
        out += [{**n, "kind": "pr", "repo": repo} for n in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return out


def is_stale(item: dict) -> bool:
    age = (
        datetime.now(UTC) - datetime.fromisoformat(item["updatedAt"].replace("Z", "+00:00"))
    ).days
    return age >= (STALE_PR_DAYS if item["kind"] == "pr" else STALE_ISSUE_DAYS)


def ensure_project(title: str, existing: dict, apply: bool) -> dict | None:
    if title in existing:
        return existing[title]
    if not apply:
        return None
    created = gql(
        """mutation($o:ID!,$t:String!){ createProjectV2(input:{ownerId:$o,title:$t}){
             projectV2{ id number title
               fields(first:30){ nodes{
                 ... on ProjectV2FieldCommon { id name }
                 ... on ProjectV2SingleSelectField { id name options{ id name } } } } } } }""",
        o=org_id(),
        t=title,
    )["createProjectV2"]["projectV2"]
    return {
        "id": created["id"],
        "number": created["number"],
        "fields": created["fields"]["nodes"],
        "keys": set(),
    }


def ensure_select_field(project: dict, name: str, options: list, apply: bool) -> dict | None:
    for f in project["fields"]:
        if f.get("name") == name:
            return {"id": f["id"], "options": {o["name"]: o["id"] for o in f.get("options") or []}}
    if not apply:
        return None
    created = gql(
        """mutation($p:ID!,$n:String!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){
             createProjectV2Field(input:{projectId:$p,dataType:SINGLE_SELECT,name:$n,singleSelectOptions:$opts}){
                projectV2Field{ ... on ProjectV2SingleSelectField {
                  id name options{ id name } } } } }""",
        p=project["id"],
        n=name,
        opts=[{"name": n, "color": c, "description": d} for n, c, d in options],
    )["createProjectV2Field"]["projectV2Field"]
    project["fields"].append(created)
    return {"id": created["id"], "options": {o["name"]: o["id"] for o in created["options"]}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="create boards and place items")
    ap.add_argument("--only", help="one board name, for a narrower run")
    args = ap.parse_args()

    existing = existing_projects()
    print(f"{len(existing)} existing project(s): {', '.join(sorted(existing))}\n")

    total_new = 0
    for title, repos in BOARDS.items():
        if args.only and args.only != title:
            continue
        project = ensure_project(title, existing, args.apply)
        verb = "reusing" if title in existing else ("creating" if args.apply else "would create")
        print(f"── {title}  ({verb})")

        prio = (
            ensure_select_field(project, "Priority", PRIORITY_OPTIONS, args.apply)
            if project
            else None
        )
        fresh = (
            ensure_select_field(project, "Freshness", FRESHNESS_OPTIONS, args.apply)
            if project
            else None
        )

        placed = skipped = stale_n = 0
        for repo in repos:
            try:
                items = open_items(repo)
            except RuntimeError as err:
                print(f"    {repo}: unreadable ({str(err)[:70]}) — skipped, NOT counted as empty")
                continue
            for it in items:
                key = f"{repo}#{it['number']}"
                if project and key in project["keys"]:
                    skipped += 1
                    continue
                stale = is_stale(it)
                stale_n += stale
                if not args.apply:
                    placed += 1
                    continue
                added = gql(
                    """mutation($p:ID!,$c:ID!){ addProjectV2ItemById(
                         input:{projectId:$p,contentId:$c}){
                         item{ id } } }""",
                    p=project["id"],
                    c=it["id"],
                )["addProjectV2ItemById"]["item"]
                if fresh:
                    want = "stale" if stale else "fresh"
                    gql(
                        """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
                             updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,
                               value:{singleSelectOptionId:$o}}){ projectV2Item{ id } } }""",
                        p=project["id"],
                        i=added["id"],
                        f=fresh["id"],
                        o=fresh["options"][want],
                    )
                placed += 1
        total_new += placed
        note = f"    {placed} to place, {stale_n} of them stale"
        if skipped:
            note += f"; {skipped} already on the board"
        if not prio and args.apply:
            note += "; Priority field missing"
        print(note)

    print(f"\n{'placed' if args.apply else 'would place'} {total_new} item(s)")
    if not args.apply:
        print("nothing was written — re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
