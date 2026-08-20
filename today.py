#!/usr/bin/env python3
"""Refresh dynamic fields in dark_mode.svg and light_mode.svg."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

import requests
from dateutil import relativedelta
from lxml import etree

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "cache" / "loc.json"
ACCOUNT_CREATED = dt.datetime(2021, 9, 14, tzinfo=dt.timezone.utc)
USER_NAME = os.environ.get("USER_NAME", "0xneves")
SVG_FILES = ("dark_mode.svg", "light_mode.svg")

# Reserved value widths for Andrew-style justify_format (dots shrink as numbers grow).
LINE_WIDTH = 76
# SLOT[id] = reserved (dots + value) width so each line stays LINE_WIDTH.
# just_len = SLOT - len(value) is the exact dots string length.
SLOT = {
    "age_data": LINE_WIDTH - len(". Uptime:"),  # 67
    "repo_data": 28,   # 8 + 28 = 36
    "contrib_data": 15,  # right starts at 39; 39+22+15=76
    "commit_data": 26,  # 10 + 26 = 36
    "follower_data": 27,  # 39+10+27=76
    "loc_data": 11,  # 25 + 11 = 36
    "loc_del": 21,  # keep LoC trailing part ending at 76
}


def get_token() -> str:
    token = os.environ.get("ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("No GitHub token. Set ACCESS_TOKEN or GITHUB_TOKEN.") from exc
    token = proc.stdout.strip()
    if proc.returncode == 0 and token:
        return token
    raise SystemExit("No GitHub token. Set ACCESS_TOKEN or GITHUB_TOKEN, or run gh auth login.")


HEADERS = {
    "Authorization": f"Bearer {get_token()}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def format_plural(unit: int) -> str:
    return "s" if unit != 1 else ""


def format_uptime(created: dt.datetime) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    diff = relativedelta.relativedelta(now, created)
    return (
        f"{diff.years} year{format_plural(diff.years)}, "
        f"{diff.months} month{format_plural(diff.months)}, "
        f"{diff.days} day{format_plural(diff.days)}"
    )


def graphql(query: str, variables: dict | None = None) -> dict:
    response = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables or {}},
        headers=HEADERS,
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"GraphQL {response.status_code}: {response.text}")
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def rest_get(url: str, retries: int = 3) -> tuple[int, object]:
    last_status = 0
    last_body: object = None
    for attempt in range(retries):
        response = requests.get(url, headers=HEADERS, timeout=30)
        last_status = response.status_code
        if response.status_code == 202:
            time.sleep(1.5 * (attempt + 1))
            last_body = None
            continue
        if response.status_code == 204:
            return 204, []
        if response.status_code == 200:
            return 200, response.json()
        last_body = response.text
        break
    return last_status, last_body


def owned_repos() -> tuple[int, int, list[dict]]:
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        allRepos: repositories(first: 1, ownerAffiliations: OWNER) {
          totalCount
        }
        repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
          nodes {
            name
            nameWithOwner
            stargazerCount
            pushedAt
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    nodes: list[dict] = []
    cursor = None
    total = 0
    while True:
        data = graphql(query, {"login": USER_NAME, "cursor": cursor})
        user = data["user"]
        total = user["allRepos"]["totalCount"]
        repos = user["repositories"]
        nodes.extend(repos["nodes"])
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    return total, 0, nodes


def profile_counts() -> tuple[int, int]:
    query = """
    query($login: String!) {
      user(login: $login) {
        followers { totalCount }
        repositoriesContributedTo {
          totalCount
        }
      }
    }
    """
    user = graphql(query, {"login": USER_NAME})["user"]
    return (
        int(user["repositoriesContributedTo"]["totalCount"]),
        int(user["followers"]["totalCount"]),
    )


def lifetime_contributions() -> int:
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar { totalContributions }
        }
      }
    }
    """
    start = ACCOUNT_CREATED
    end_now = dt.datetime.now(dt.timezone.utc)
    total = 0
    while start < end_now:
        window_end = start + relativedelta.relativedelta(years=1)
        if window_end > end_now:
            window_end = end_now
        data = graphql(
            query,
            {
                "login": USER_NAME,
                "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        total += int(
            data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
        )
        start = window_end
    return total


def load_loc_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_loc_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def contributor_loc(name_with_owner: str) -> tuple[int | None, int | None]:
    url = f"https://api.github.com/repos/{name_with_owner}/stats/contributors"
    status, body = rest_get(url, retries=3)
    if status != 200 or not isinstance(body, list):
        return None, None
    additions = 0
    deletions = 0
    found = False
    for row in body:
        author = row.get("author") or {}
        login = (author.get("login") or "").lower()
        if login != USER_NAME.lower():
            continue
        found = True
        for week in row.get("weeks") or []:
            additions += int(week.get("a") or 0)
            deletions += int(week.get("d") or 0)
    if not found:
        return 0, 0
    return additions, deletions


def lines_of_code(repos: list[dict]) -> tuple[int, int, int, dict]:
    cache = load_loc_cache()
    added = 0
    deleted = 0
    skipped = 0
    refreshed = 0
    for repo in repos:
        key = repo["nameWithOwner"]
        pushed = repo.get("pushedAt") or ""
        cached = cache.get(key) or {}
        if cached.get("pushedAt") == pushed and "additions" in cached and "deletions" in cached:
            added += int(cached["additions"])
            deleted += int(cached["deletions"])
            continue
        loc_add, loc_del = contributor_loc(key)
        if loc_add is None:
            skipped += 1
            continue
        cache[key] = {"pushedAt": pushed, "additions": loc_add, "deletions": loc_del}
        added += loc_add
        deleted += loc_del
        refreshed += 1
    save_loc_cache(cache)
    return added, deleted, added - deleted, {"refreshed": refreshed, "skipped": skipped}


def find_by_id(root: etree._Element, element_id: str) -> etree._Element | None:
    for element in root.iter():
        if element.get("id") == element_id:
            return element
    return None


def justify_format(root: etree._Element, element_id: str, new_text, length: int = 0) -> None:
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    element = find_by_id(root, element_id)
    if element is not None:
        element.text = new_text
    just_len = max(0, length - len(new_text))
    if just_len == 0:
        dot_string = ""
    elif just_len == 1:
        dot_string = " "
    elif just_len == 2:
        dot_string = ". "
    else:
        dot_string = " " + ("." * (just_len - 2)) + " "
    dots = find_by_id(root, f"{element_id}_dots")
    if dots is not None:
        dots.text = dot_string


def svg_overwrite(
    filename: str,
    age_data: str,
    commit_data: int,
    repo_data: int,
    contrib_data: int,
    follower_data: int,
    loc_net: int,
    loc_add: int,
    loc_del: int,
) -> None:
    path = ROOT / filename
    tree = etree.parse(str(path))
    root = tree.getroot()
    justify_format(root, "age_data", age_data, SLOT["age_data"])
    justify_format(root, "repo_data", repo_data, SLOT["repo_data"])
    justify_format(root, "contrib_data", contrib_data, SLOT["contrib_data"])
    justify_format(root, "commit_data", commit_data, SLOT["commit_data"])
    justify_format(root, "follower_data", follower_data, SLOT["follower_data"])
    justify_format(root, "loc_data", loc_net, SLOT["loc_data"])
    justify_format(root, "loc_add", loc_add)
    justify_format(root, "loc_del", loc_del, SLOT["loc_del"])
    tree.write(str(path), encoding="utf-8", xml_declaration=True, pretty_print=False)


def timed(label: str, func, *args):
    start = time.perf_counter()
    result = func(*args)
    elapsed = time.perf_counter() - start
    stamp = f"{elapsed:.4f} s" if elapsed >= 1 else f"{elapsed * 1000:.1f} ms"
    print(f"  {label:<22} {stamp:>12}")
    return result, elapsed


def main() -> None:
    print("Calculation times:")
    total = 0.0

    age, elapsed = timed("uptime", format_uptime, ACCOUNT_CREATED)
    total += elapsed

    (repo_count, _, repos), elapsed = timed("repos", owned_repos)
    total += elapsed

    (contributed, followers), elapsed = timed("contrib/followers", profile_counts)
    total += elapsed

    commits, elapsed = timed("commits", lifetime_contributions)
    total += elapsed

    (loc_add, loc_del, loc_net, loc_meta), elapsed = timed("loc", lines_of_code, repos)
    total += elapsed

    def _patch():
        for name in SVG_FILES:
            svg_overwrite(
                name,
                age,
                commits,
                repo_count,
                contributed,
                followers,
                loc_net,
                loc_add,
                loc_del,
            )

    _, elapsed = timed("svg patch", _patch)
    total += elapsed

    print(f"  {'total':<22} {total:.4f} s")
    print()
    print(f"{USER_NAME}@enor")
    print(f"  Uptime:     {age}")
    print(f"  Repos:      {repo_count:,}  (contributed: {contributed:,})")
    print(f"  Commits:    {commits:,}")
    print(f"  Followers:  {followers:,}")
    print(f"  LoC:        {loc_net:,} ({loc_add:,}++, {loc_del:,}--)")
    print(f"  LoC cache:  refreshed={loc_meta['refreshed']} skipped={loc_meta['skipped']}")


if __name__ == "__main__":
    main()
