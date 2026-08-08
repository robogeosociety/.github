#!/usr/bin/env python3
"""Weekly project review: what has gone stale, what is over WIP, what to do next.

Replaces the wiki-staleness wall of text in #dev with something actionable, and is
NON-MUTATING on issues and pull requests throughout: nothing here closes, labels,
comments on, or reopens anything. A stale-bot that closes work is a stale-bot that
loses work, and the org's oldest open item is 30 days old — there is nothing here
that needs reaping, only surfacing.

The one thing it DOES write is the Priority field on project boards, and only with
`--apply`. That is deliberate and deliberately fenced (see write_priorities): the
main board carries 353 items, so a ranking bug with no cap is a 353-item mistake.

Four inputs, because a priority nobody recognises is a priority nobody follows:
  * GitHub activity — age, review state, checks, linked issues
  * the project boards — where an item sits, and whether that column is over WIP
  * recent #dev — what the team actually touched this week
  * the dev vault, over Vectorize — the design context a title does not carry

Usage:
  project_review.py                    # report to stdout, write nothing
  project_review.py --post             # also post the card + thread to #dev
  project_review.py --apply --post     # additionally write Priority to the boards
"""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

ORG = os.environ.get("GH_ORG", "robogeosociety")
GATE_URL = os.environ.get("GATE_URL", "https://deploy-gate.tommy-b-doerr.workers.dev")
NOTIFY_SECRET = os.environ.get("NOTIFY_SECRET", "")
POLICY_PATH = os.environ.get("POLICY_PATH", "standard/project-policy.yml")

# The board is 353 items. A ranking bug that rewrites all of them in one run is a
# mess to undo by hand, and Projects v2 has no bulk undo. So a single run may
# change at most this many items; the rest are reported and wait for next week.
MAX_PRIORITY_WRITES = int(os.environ.get("MAX_PRIORITY_WRITES", "25"))

PRIORITIES = ["P0", "P1", "P2", "P3"]

# ── GitHub ───────────────────────────────────────────────────────────────────


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


def search_all(query: str, limit: int = 200) -> list:
    """Paged issue/PR search. GitHub caps a page at 100 and the org is past that."""
    out, cursor = [], None
    while len(out) < limit:
        page = gql(
            """query($q:String!,$after:String){
                 search(query:$q, type:ISSUE, first:100, after:$after){
                   pageInfo{ hasNextPage endCursor }
                   nodes{
                     __typename
                     ... on Issue { number title url updatedAt createdAt
                       repository{name} labels(first:10){nodes{name}}
                       projectItems(first:5){totalCount} comments{totalCount} }
                     ... on PullRequest { number title url updatedAt createdAt isDraft
                       repository{name} labels(first:10){nodes{name}}
                       projectItems(first:5){totalCount} reviewDecision
                       commits(last:1){nodes{commit{statusCheckRollup{state}}}} }
                   } } }""",
            q=query,
            after=cursor or "",
        )["search"]
        out.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return out[:limit]


def days_since(iso: str) -> int:
    then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(UTC) - then).days


PROJECT_FIELDS = """
  id number title
  fields(first:30){ nodes{
    ... on ProjectV2FieldCommon { id name dataType }
    ... on ProjectV2SingleSelectField { id name options{ id name } } } }
"""

ITEM_FIELDS = """
  id
  fieldValues(first:20){ nodes{
    ... on ProjectV2ItemFieldSingleSelectValue {
      name
      field{ ... on ProjectV2FieldCommon { name } } } } }
  content{
    ... on Issue { number title url updatedAt repository{name} }
    ... on PullRequest { number title url updatedAt repository{name} } }
"""


def load_projects() -> list:
    """Every org project with its fields and ALL of its items.

    Items are paged. A single `first: 100` looks like it works — the query returns,
    the report renders — but the main board holds 353 items, so every WIP count
    would be computed from the first 100 and quietly understated. A WIP report that
    undercounts is worse than no WIP report: it says the column is fine.
    """
    projects = gql(
        "query($org:String!){ organization(login:$org){ projectsV2(first:20){ nodes{"
        + PROJECT_FIELDS
        + " items(first:1){ totalCount } } } } }",
        org=ORG,
    )["organization"]["projectsV2"]["nodes"]

    for proj in projects:
        nodes, cursor = [], None
        while len(nodes) < proj["items"]["totalCount"]:
            page = gql(
                "query($id:ID!,$after:String){ node(id:$id){ ... on ProjectV2 {"
                " items(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{"
                + ITEM_FIELDS
                + " } } } } }",
                id=proj["id"],
                after=cursor or "",
            )["node"]["items"]
            nodes.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        proj["items"]["nodes"] = nodes
    return projects


def status_of(item: dict) -> str | None:
    for fv in item.get("fieldValues", {}).get("nodes", []):
        if (fv.get("field") or {}).get("name") == "Status":
            return fv.get("name")
    return None


def priority_of(item: dict) -> str | None:
    for fv in item.get("fieldValues", {}).get("nodes", []):
        if (fv.get("field") or {}).get("name") == "Priority":
            return fv.get("name")
    return None


# ── the report ───────────────────────────────────────────────────────────────


def visibility() -> dict:
    """What this token can actually see, versus what the org contains.

    The first CI run reported 17 open PRs and 134 open issues; an org-admin token
    saw 50 and 191 at the same moment. PROJECT_SYNC_TOKEN is a PAT, and a PAT only
    reaches repositories it was granted — so two thirds of open PRs were missing
    from a report that gave no hint anything was missing.

    A report that undercounts silently is the failure mode this whole job exists to
    avoid, so the numbers are compared and the shortfall is stated in the report
    rather than left for someone to notice.
    """
    try:
        repos = gql(
            """query($org:String!){ organization(login:$org){
                 repositories(first:100){ totalCount nodes{ name } } } }""",
            org=ORG,
        )["organization"]["repositories"]
        return {"repos_visible": len(repos["nodes"]), "repos_total": repos["totalCount"]}
    except RuntimeError as err:
        return {"error": str(err)[:200]}


def find_stale(policy: dict) -> dict:
    """Items nobody has touched. Reported only — never closed, labelled or nudged."""
    s = policy["staleness"]
    prs = search_all(f"org:{ORG} is:open is:pr sort:updated-asc")
    issues = search_all(f"org:{ORG} is:open is:issue sort:updated-asc")

    stale_prs = [
        p
        for p in prs
        if days_since(p["updatedAt"]) >= s["pull_request_days"]
        and (s.get("include_draft_prs") or not p.get("isDraft"))
    ]
    stale_issues = [i for i in issues if days_since(i["updatedAt"]) >= s["issue_days"]]

    failing = []
    if s.get("report_failing_checks"):
        for p in prs:
            roll = (p.get("commits") or {}).get("nodes") or []
            state = ((roll[0] or {}).get("commit") or {}).get("statusCheckRollup") if roll else None
            if state and state.get("state") == "FAILURE" and not p.get("isDraft"):
                failing.append(p)

    return {
        "prs": stale_prs,
        "issues": stale_issues,
        "failing": failing,
        # Blocked is a different state from forgotten: a PR waiting on review for a
        # fortnight needs a reviewer, not a nudge to its author.
        "awaiting_review": [
            p
            for p in prs
            if p.get("reviewDecision") == "REVIEW_REQUIRED" and days_since(p["updatedAt"]) >= 7
        ],
        "totals": {"prs": len(prs), "issues": len(issues)},
    }


def find_wip_breaches(projects: list, policy: dict) -> list:
    """Columns over their limit, plus projects with no policy at all."""
    by_number = {p["number"]: p for p in policy.get("projects", [])}
    dormant = {p["number"] for p in policy.get("dormant", [])}
    out = []
    for proj in projects:
        cfg = by_number.get(proj["number"])
        if proj["number"] in dormant:
            if proj["items"]["totalCount"]:
                # A board declared dead that has started collecting work again.
                out.append(
                    {
                        "kind": "dormant-revived",
                        "project": proj["title"],
                        "count": proj["items"]["totalCount"],
                    }
                )
            continue
        if not cfg:
            out.append({"kind": "no-policy", "project": proj["title"], "number": proj["number"]})
            continue
        if cfg.get("wip_exempt"):
            continue
        counts: dict[str, int] = {}
        for item in proj["items"]["nodes"]:
            st = status_of(item)
            if st:
                counts[st] = counts.get(st, 0) + 1
        for column, limit in (cfg.get("limits") or {}).items():
            actual = counts.get(column, 0)
            if actual > limit:
                out.append(
                    {
                        "kind": "over-wip",
                        "project": proj["title"],
                        "column": column,
                        "actual": actual,
                        "limit": limit,
                    }
                )
    return out


def find_coverage_gaps(stale: dict, projects: list, policy: dict) -> dict:
    """Open work on no board, and boards that hold nothing."""
    if not policy.get("coverage", {}).get("report_unboarded", True):
        return {}
    exempt = set(policy.get("coverage", {}).get("exempt_repos") or [])
    orphans: dict[str, int] = {}
    for item in [
        *search_all(f"org:{ORG} is:open is:issue"),
        *search_all(f"org:{ORG} is:open is:pr"),
    ]:
        if item.get("projectItems", {}).get("totalCount"):
            continue
        repo = item["repository"]["name"]
        if repo in exempt:
            continue
        orphans[repo] = orphans.get(repo, 0) + 1
    empty = [p["title"] for p in projects if p["items"]["totalCount"] == 0]
    return {"orphans": orphans, "empty_projects": empty}


# ── grounding ────────────────────────────────────────────────────────────────


def _signed_post(path: str, payload: dict, timeout: int = 45) -> dict:
    body = json.dumps(payload).encode()
    sig = hmac.new(NOTIFY_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{GATE_URL}{path}",
        method="POST",
        data=body,
        headers={
            "content-type": "application/json",
            "x-request-signature": f"sha256={sig}",
            "user-agent": "rgs-project-review/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return {}


def vault_context(queries: list) -> tuple[str, dict]:
    """Dev-vault passages for the busiest work, via deploy-gate's /search.

    Fetched HERE rather than handed to the model as a tool. The Models API is a
    single-turn completion endpoint with no tool-use, so retrieval must happen in
    Python before the call. This keeps the request deterministic and cheap.
    """
    if not NOTIFY_SECRET:
        return "", {}
    seen, blocks, stats = set(), [], {"ragHits": 0, "ragMs": 0}
    for q in queries[:4]:
        try:
            res = _signed_post("/search", {"query": q, "top_k": 3})
        except (urllib.error.HTTPError, urllib.error.URLError) as err:
            print(f"vault search failed for {q[:40]!r}: {err}", file=sys.stderr)
            continue
        stats["ragMs"] += res.get("ragMs") or 0
        for h in res.get("hits") or []:
            key = h.get("path")
            if not key or key in seen:
                continue
            seen.add(key)
            stats["ragHits"] += 1
            blocks.append(f"### {h.get('vault')}/{h.get('title')}\n{(h.get('text') or '')[:400]}")
    return ("\n\n".join(blocks[:8]), stats)


def recent_dev_activity() -> list:
    """What the org actually merged this week — the strongest signal of focus."""
    since = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    merged = search_all(f"org:{ORG} is:pr is:merged merged:>={since}", limit=60)
    by_repo: dict[str, int] = {}
    for p in merged:
        by_repo[p["repository"]["name"]] = by_repo.get(p["repository"]["name"], 0) + 1
    return sorted(by_repo.items(), key=lambda kv: -kv[1])


# ── ranking ──────────────────────────────────────────────────────────────────

# GitHub Models API — replaces `claude -p` subprocess. Uses the same GH_TOKEN
# that already has `project` scope; Models access is included with Copilot Pro.
MODELS_URL = "https://models.github.com/chat/completions"
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "openai/gpt-4o-mini")
SYSTEM = (
    "You are ranking work that has already been described to you in full. You have "
    "no tools, no repository and no network. Never attempt to look anything up or "
    "ask for more detail. Reply with the requested JSON and nothing else."
)


def rank(candidates: list, context: str, activity: list) -> tuple[list, dict]:
    """Ask for a priority per item. Returns ([{key, priority, why}], stats).

    Returns ([], {}) on any failure. An unranked report still lists what is stale
    and what is over WIP, which is most of the value; a failed model call must not
    cost the whole review.
    """
    if not candidates:
        return [], {}
    lines = [
        f"- {c['key']} ({c['repo']}, {c['age']}d idle, {c['state']}): {c['title'][:90]}"
        for c in candidates[:40]
    ]
    focus = ", ".join(f"{r} ({n})" for r, n in activity[:6]) or "nothing merged this week"
    prompt = (
        "Assign a priority to each open item below for a solo operator's weekly plan.\n\n"
        f"Where the work actually went this week: {focus}\n\n"
        + (f"Design context from the team's own notes:\n{context}\n\n" if context else "")
        + "Items:\n"
        + "\n".join(lines)
        + "\n\n"
        "P0 blocks something already running or in flight. P1 is this week's work. "
        "P2 is real but can wait. P3 is someone's someday.\n"
        "Weight toward repos with recent activity — that is where attention already is — "
        "and toward items the notes show something else depends on. Idle age alone is weak "
        "evidence: a thing can be old because nobody needs it.\n"
        'Reply with JSON only: {"items":[{"key":"repo#123","priority":"P1","why":"<=12 words"}]}'
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    body = json.dumps({
        "model": REVIEW_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        MODELS_URL,
        data=body,
        headers={
            "Authorization": f"******",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            payload = json.loads(res.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
        print(f"ranking failed: {err}", file=sys.stderr)
        return [], {}
    elapsed_ms = int((time.time() - t0) * 1000)

    usage = payload.get("usage") or {}
    stats = {
        "model": payload.get("model") or REVIEW_MODEL,
        "inputTokens": usage.get("prompt_tokens") or 0,
        "outputTokens": usage.get("completion_tokens") or 0,
        "totalMs": elapsed_ms,
    }

    text = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    # The model wraps JSON in prose often enough that finding the object beats
    # insisting on a clean parse and discarding an otherwise good answer.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        print(f"no JSON in ranking reply: {text[:200]}", file=sys.stderr)
        return [], stats
    try:
        items = json.loads(text[start : end + 1]).get("items") or []
    except json.JSONDecodeError:
        return [], stats
    valid = [i for i in items if i.get("priority") in PRIORITIES and i.get("key")]
    if len(valid) < len(items):
        print(f"dropped {len(items) - len(valid)} items with an unknown priority", file=sys.stderr)
    return valid, stats


# ── the one mutation ─────────────────────────────────────────────────────────


def ensure_priority_field(project: dict, apply: bool) -> tuple[str | None, dict]:
    """The Priority field, created if the board lacks one. Idempotent.

    Six of nine boards had no Priority field, including the 353-item main board —
    which is why it could never be prioritised. Options match the two boards that
    already had one, so the vocabulary is consistent org-wide rather than a second
    dialect.
    """
    for f in project["fields"]["nodes"]:
        if f.get("name") == "Priority":
            return f["id"], {o["name"]: o["id"] for o in f.get("options") or []}
    if not apply:
        return None, {}
    created = gql(
        """mutation($p:ID!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){
             createProjectV2Field(input:{projectId:$p, dataType:SINGLE_SELECT,
               name:"Priority", singleSelectOptions:$opts}){
               projectV2Field{ ... on ProjectV2SingleSelectField { id options{ id name } } } } }""",
        p=project["id"],
        opts=(
            [
                {"name": n, "color": c, "description": d}
                for n, c, d in [
                    ("P0", "RED", "Blocks something already running"),
                    ("P1", "ORANGE", "This week"),
                    ("P2", "YELLOW", "Real, but can wait"),
                    ("P3", "GRAY", "Someday"),
                ]
            ]
        ),
    )["createProjectV2Field"]["projectV2Field"]
    return created["id"], {o["name"]: o["id"] for o in created["options"]}


def write_priorities(projects: list, ranking: dict, apply: bool) -> dict:
    """Write Priority to boards. The only thing in this script that mutates.

    Three fences, because the main board is 353 items and Projects v2 has no bulk
    undo:
      * --apply is required; the default run writes nothing at all
      * at most MAX_PRIORITY_WRITES items change per run, so a bad ranking is a
        25-item mistake and next week's run is a chance to notice
      * an item that ALREADY has a priority is left alone. A human who set P0 on
        something did so knowing more than this job does, and silently demoting it
        is the single most annoying thing this could do.
    """
    changed, skipped, planned = [], 0, []
    for proj in projects:
        field_id, options = ensure_priority_field(proj, apply)
        for item in proj["items"]["nodes"]:
            content = item.get("content") or {}
            # `is None`, not falsiness: item number 0 is falsy. GitHub numbers start
            # at 1 so this is latent rather than live, but a guard that silently
            # drops a real item under some other numbering is not worth keeping.
            if content.get("number") is None:
                continue
            key = f"{content['repository']['name']}#{content['number']}"
            want = ranking.get(key)
            if not want:
                continue
            if priority_of(item):
                skipped += 1
                continue
            planned.append((proj, item, key, want))

    for proj, item, key, want in planned[:MAX_PRIORITY_WRITES]:
        if not apply:
            changed.append(
                {"key": key, "priority": want, "project": proj["title"], "dry_run": True}
            )
            continue
        field_id, options = ensure_priority_field(proj, apply)
        if not field_id or want not in options:
            continue
        gql(
            """mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
                 updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,
                   value:{singleSelectOptionId:$o}}){ projectV2Item{ id } } }""",
            p=proj["id"],
            i=item["id"],
            f=field_id,
            o=options[want],
        )
        changed.append({"key": key, "priority": want, "project": proj["title"]})

    return {
        "changed": changed,
        "skipped_already_set": skipped,
        "deferred": max(0, len(planned) - MAX_PRIORITY_WRITES),
        "applied": apply,
    }


# ── rendering ────────────────────────────────────────────────────────────────


def link(item: dict) -> str:
    return f"[{item['repository']['name']}#{item['number']}]({item['url']})"


def render(
    stale: dict, wip: list, gaps: dict, ranked: list, writes: dict, vis: dict | None = None
) -> tuple[str, list]:
    """A short card and the thread body, split into Discord-sized parts.

    Split here rather than in the Worker: this code knows where a section ends, and
    a 2000-character cut placed blindly lands in the middle of a table.
    """
    card = (
        f"**{len(stale['prs'])}** stale PR(s) · **{len(stale['issues'])}** stale issue(s) · "
        f"**{len(wip)}** board issue(s)\n"
        f"-# of {stale['totals']['prs']} open PRs and {stale['totals']['issues']} open issues. "
        "Nothing was closed, labelled or commented on."
    )

    sections = []

    # Stated first, because everything below it is wrong if the token is partial.
    if vis and vis.get("repos_total") and vis["repos_visible"] < vis["repos_total"]:
        missing = vis["repos_total"] - vis["repos_visible"]
        sections.append(
            f"⚠️ **This report is incomplete.** The token can see "
            f"{vis['repos_visible']} of {vis['repos_total']} repositories — every count "
            f"below excludes {missing}. Grant PROJECT_SYNC_TOKEN access to the rest, or "
            "the review will keep understating the backlog."
        )

    if stale["prs"]:
        rows = [
            f"- {link(p)} — {days_since(p['updatedAt'])}d idle — {p['title'][:60]}"
            for p in stale["prs"][:12]
        ]
        more = f"\n-# …and {len(stale['prs']) - 12} more" if len(stale["prs"]) > 12 else ""
        sections.append("**Stale pull requests**\n" + "\n".join(rows) + more)

    if stale["awaiting_review"]:
        rows = [
            f"- {link(p)} — waiting {days_since(p['updatedAt'])}d"
            for p in stale["awaiting_review"][:8]
        ]
        sections.append("**Waiting on review** (blocked, not forgotten)\n" + "\n".join(rows))

    if stale["failing"]:
        rows = [f"- {link(p)} — checks red" for p in stale["failing"][:8]]
        sections.append("**Red checks**\n" + "\n".join(rows))

    if stale["issues"]:
        rows = [
            f"- {link(i)} — {days_since(i['updatedAt'])}d idle — {i['title'][:60]}"
            for i in stale["issues"][:12]
        ]
        more = f"\n-# …and {len(stale['issues']) - 12} more" if len(stale["issues"]) > 12 else ""
        sections.append("**Stale issues**\n" + "\n".join(rows) + more)

    if wip:
        rows = []
        for b in wip:
            if b["kind"] == "over-wip":
                rows.append(f"- **{b['project']}** · {b['column']} — {b['actual']}/{b['limit']}")
            elif b["kind"] == "no-policy":
                rows.append(f"- **{b['project']}** (#{b['number']}) — no WIP policy set")
            elif b["kind"] == "dormant-revived":
                rows.append(
                    f"- **{b['project']}** — marked dormant but now holds {b['count']} item(s)"
                )
        sections.append("**WIP and board policy**\n" + "\n".join(rows))

    if gaps.get("orphans"):
        top = sorted(gaps["orphans"].items(), key=lambda kv: -kv[1])[:8]
        rows = [f"- {r} — {n} item(s)" for r, n in top]
        sections.append(
            f"**On no board** ({sum(gaps['orphans'].values())} total)\n" + "\n".join(rows)
        )
    if gaps.get("empty_projects"):
        sections.append("**Empty projects** — " + ", ".join(gaps["empty_projects"]))

    if ranked:
        rows = [f"- `{r['priority']}` {r['key']} — {r.get('why', '')[:60]}" for r in ranked[:15]]
        sections.append("**Suggested priority**\n" + "\n".join(rows))

    if writes.get("changed"):
        verb = "Set" if writes["applied"] else "Would set (dry run)"
        note = f"{verb} Priority on {len(writes['changed'])} item(s)"
        if writes.get("skipped_already_set"):
            note += f"; left {writes['skipped_already_set']} alone that already had one"
        if writes.get("deferred"):
            note += f"; {writes['deferred']} deferred to next week by the per-run cap"
        sections.append(f"-# {note}")

    if not sections:
        sections.append("Nothing stale, nothing over WIP, nothing off a board. Quiet week.")

    return card, pack(sections)


LIMIT = 1800  # under Discord's 2000, leaving room for the telemetry footnote


def pack(sections: list) -> list:
    """Sections into Discord-sized messages, splitting oversized ones by line.

    Packing on section boundaries alone is not enough: one section can exceed the
    limit by itself — forty WIP rows measured at 3064 characters — and Discord
    rejects an over-long message outright rather than truncating it, so the whole
    report would fail to post. Oversized sections are split on line boundaries,
    which keeps a table row intact even when its table is broken across messages.
    """
    chunks = []
    for sec in sections:
        if len(sec) <= LIMIT:
            chunks.append(sec)
            continue
        cur = ""
        for line in sec.split("\n"):
            # A single line longer than the limit is pathological; hard-cut it
            # rather than emit something Discord will refuse.
            line = line if len(line) <= LIMIT else line[: LIMIT - 1] + "…"
            if len(cur) + len(line) + 1 > LIMIT and cur:
                chunks.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            chunks.append(cur)

    parts, cur = [], ""
    for chunk in chunks:
        if len(cur) + len(chunk) + 2 > LIMIT and cur:
            parts.append(cur)
            cur = chunk
        else:
            cur = f"{cur}\n\n{chunk}" if cur else chunk
    if cur:
        parts.append(cur)
    return parts


def load_policy() -> dict:
    import yaml

    with open(POLICY_PATH) as fh:
        return yaml.safe_load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post", action="store_true", help="post the card + thread to #dev")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="WRITE Priority to the boards; without it nothing is written",
    )
    ap.add_argument("--no-rank", action="store_true", help="skip the model call")
    args = ap.parse_args()

    policy = load_policy()
    projects = load_projects()

    stale = find_stale(policy)
    wip = find_wip_breaches(projects, policy)
    gaps = find_coverage_gaps(stale, projects, policy)

    ranked, stats = [], {}
    if not args.no_rank:
        candidates = [
            {
                "key": f"{x['repository']['name']}#{x['number']}",
                "repo": x["repository"]["name"],
                "title": x["title"],
                "age": days_since(x["updatedAt"]),
                "state": "PR" if x.get("__typename") == "PullRequest" else "issue",
            }
            for x in [*stale["prs"], *stale["awaiting_review"], *stale["issues"]]
        ]
        # Deduplicate: a PR can be both stale and awaiting review, and asking the
        # model to rank the same key twice invites two different answers.
        seen, unique = set(), []
        for c in candidates:
            if c["key"] not in seen:
                seen.add(c["key"])
                unique.append(c)
        context, rag = vault_context([c["title"] for c in unique[:4]])
        ranked, stats = rank(unique, context, recent_dev_activity())
        stats.update(rag)

    writes = write_priorities(projects, {r["key"]: r["priority"] for r in ranked}, args.apply)

    card, parts = render(stale, wip, gaps, ranked, writes, visibility())
    print(card)
    for p in parts:
        print("\n" + p)

    if args.post:
        if not NOTIFY_SECRET:
            print("NOTIFY_SECRET unset — cannot post", file=sys.stderr)
            return 1
        _signed_post(
            "/notify",
            {
                "title": "Weekly project review",
                "body": card,
                "level": "info",
                "thread": parts,
                "thread_name": "project review",
                "telemetry": stats,
            },
        )
        print(f"\nposted to #dev ({len(parts)} thread part(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
