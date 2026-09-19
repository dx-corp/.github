#!/usr/bin/env python3
"""Fail a PR when high-severity review feedback is still unresolved.

Port of .github/scripts/check-pr-review-threads.rb. The Ruby original could not
run on the ARC runner image, which ships no Ruby interpreter. This port uses the
Python standard library only -- json, re, subprocess, argparse -- so it has no
dependency on anything beyond the interpreter itself.

Behaviour is intentionally identical to the Ruby version: same severity ranking,
same informational-summary suppression, same GraphQL queries and pagination,
same annotations, same exit codes (0 clean, 1 blocking feedback, 2 bad usage).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

SEVERITY_RANK = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "p1": 4,
    "p0": 5,
}

INFORMATIONAL_HEADING = re.compile(r"\A##\s+(PR\s+Summary|Summary|Walkthrough)\b", re.IGNORECASE)
INFORMATIONAL_AUTHOR = re.compile(r"\A(cursor|coderabbitai|chatgpt-codex-connector)\b", re.IGNORECASE)

SEVERITY_PATTERNS = (
    ("p0", (re.compile(r"\bP0\b", re.IGNORECASE),)),
    ("p1", (re.compile(r"\bP1\b", re.IGNORECASE),)),
    ("high", (re.compile(r"\bHigh Severity\b", re.IGNORECASE), re.compile(r"!\[High Badge\]", re.IGNORECASE))),
    ("medium", (re.compile(r"\bMedium Severity\b", re.IGNORECASE), re.compile(r"!\[Medium Badge\]", re.IGNORECASE))),
    ("low", (re.compile(r"\bLow Severity\b", re.IGNORECASE), re.compile(r"!\[Low Badge\]", re.IGNORECASE))),
)


def _str(value) -> str:
    return "" if value is None else str(value)


def _dig(node, *keys):
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _nodes(value):
    return value if isinstance(value, list) else []


def first_nonblank_line(body) -> str:
    for line in _str(body).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def informational_summary(body, author=None) -> bool:
    if not INFORMATIONAL_HEADING.search(first_nonblank_line(body)):
        return False
    return bool(INFORMATIONAL_AUTHOR.search(_str(author)))


def severity(body) -> str:
    text = _str(body)
    for level, patterns in SEVERITY_PATTERNS:
        if any(pattern.search(text) for pattern in patterns):
            return level
    return "none"


def severity_comment(comments):
    """Return (severity, comment) for the highest-severity comment, or None."""
    candidates = []
    for comment in _nodes(comments):
        detected = severity(_dig(comment, "body"))
        if SEVERITY_RANK[detected] <= SEVERITY_RANK["none"]:
            continue
        candidates.append((detected, comment))
    if not candidates:
        return None
    # max() keeps the first maximal element, matching Ruby's max_by.
    return max(candidates, key=lambda pair: SEVERITY_RANK[pair[0]])


def unresolved_threads(payload, min_severity="high", include_outdated=False):
    threshold = SEVERITY_RANK[min_severity]
    threads = _nodes(_dig(payload, "data", "repository", "pullRequest", "reviewThreads", "nodes"))
    matches = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        if thread.get("isResolved"):
            continue
        if thread.get("isOutdated") and not include_outdated:
            continue

        found = severity_comment(_dig(thread, "comments", "nodes"))
        detected, comment = found if found else ("none", {})
        if SEVERITY_RANK[detected] < threshold:
            continue

        matches.append(
            {
                "kind": "review_thread",
                "id": thread.get("id"),
                "path": thread.get("path"),
                "line": thread.get("line"),
                "is_outdated": thread.get("isOutdated"),
                "severity": detected,
                "url": comment.get("url"),
                "body": _str(comment.get("body")),
            }
        )
    return matches


def top_level_feedback(payload, min_severity="high"):
    threshold = SEVERITY_RANK[min_severity]
    pull_request = _dig(payload, "data", "repository", "pullRequest") or {}
    current_head_oid = _str(pull_request.get("headRefOid"))
    feedback = []

    for comment in _nodes(_dig(pull_request, "comments", "nodes")):
        if not isinstance(comment, dict):
            continue
        author = _dig(comment, "author", "login")
        if informational_summary(comment.get("body"), author=author):
            continue
        detected = severity(comment.get("body"))
        if SEVERITY_RANK[detected] < threshold:
            continue
        feedback.append(
            {
                "kind": "pr_comment",
                "severity": detected,
                "url": comment.get("url"),
                "body": _str(comment.get("body")),
                "author": author,
            }
        )

    for review in _nodes(_dig(pull_request, "reviews", "nodes")):
        if not isinstance(review, dict):
            continue
        author = _dig(review, "author", "login")
        if informational_summary(review.get("body"), author=author):
            continue
        detected = severity(review.get("body"))
        if SEVERITY_RANK[detected] < threshold:
            continue
        review_commit_oid = _str(_dig(review, "commit", "oid"))
        if current_head_oid and review_commit_oid and review_commit_oid != current_head_oid:
            continue
        feedback.append(
            {
                "kind": "pr_review",
                "severity": detected,
                "url": review.get("url"),
                "body": _str(review.get("body")),
                "author": author,
                "state": review.get("state"),
            }
        )

    return feedback


def blocking_feedback(payload, min_severity="high", include_outdated=False):
    return unresolved_threads(
        payload, min_severity=min_severity, include_outdated=include_outdated
    ) + top_level_feedback(payload, min_severity=min_severity)


GRAPHQL_QUERY = """
query($owner:String!,$repo:String!,$number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      headRefOid
      comments(first:100) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } body url }
      }
      reviews(first:100) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } body commit { oid } state url }
      }
      reviewThreads(first:100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first:20) { nodes { body url } }
        }
      }
    }
  }
}
"""

COMMENTS_PAGE_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$after:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      comments(first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } body url }
      }
    }
  }
}
"""

REVIEWS_PAGE_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$after:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      reviews(first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } body commit { oid } state url }
      }
    }
  }
}
"""

REVIEW_THREADS_PAGE_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$after:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first:20) { nodes { body url } }
        }
      }
    }
  }
}
"""


class GraphQLError(RuntimeError):
    pass


def fetch_graphql(owner, name, pr, query, cursor=None):
    args = [
        "gh", "api", "graphql",
        "-f", f"owner={owner}",
        "-f", f"repo={name}",
        "-F", f"number={pr}",
        "-f", f"query={query}",
    ]
    if cursor:
        args += ["-f", f"after={cursor}"]

    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        # Never degrade to "no feedback found" when the GitHub CLI is absent.
        raise GraphQLError(f"gh is not installed on this runner: {exc}") from exc

    if completed.returncode != 0:
        raise GraphQLError(f"gh api graphql failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout)


def fetch_connection_tail(owner, name, pr, query, connection_name, first_connection):
    nodes = []
    connection = first_connection or {}
    page_info = connection.get("pageInfo") or {}
    while page_info.get("hasNextPage"):
        cursor = _str(page_info.get("endCursor"))
        if not cursor:
            raise GraphQLError(f"gh api graphql failed: missing {connection_name} endCursor")
        payload = fetch_graphql(owner, name, pr, query, cursor=cursor)
        connection = _dig(payload, "data", "repository", "pullRequest", connection_name) or {}
        nodes.extend(_nodes(connection.get("nodes")))
        page_info = connection.get("pageInfo") or {}
    return nodes


def merge_pull_request_connections(payload, comments=None, reviews=None, review_threads=None):
    merged = payload if isinstance(payload, dict) else {}
    if not isinstance(merged.get("data"), dict):
        merged["data"] = {}
    if not isinstance(merged["data"].get("repository"), dict):
        merged["data"]["repository"] = {}
    if not isinstance(merged["data"]["repository"].get("pullRequest"), dict):
        merged["data"]["repository"]["pullRequest"] = {}
    pull_request = merged["data"]["repository"]["pullRequest"]

    for key, value in (("comments", comments), ("reviews", reviews), ("reviewThreads", review_threads)):
        if value is None:
            continue
        if not isinstance(pull_request.get(key), dict):
            pull_request[key] = {}
        pull_request[key]["nodes"] = value
    return merged


def fetch_payload(repo, pr):
    owner, _, name = repo.partition("/")
    payload = fetch_graphql(owner, name, pr, GRAPHQL_QUERY)
    pull_request = _dig(payload, "data", "repository", "pullRequest") or {}
    comments_connection = pull_request.get("comments") or {}
    reviews_connection = pull_request.get("reviews") or {}
    threads_connection = pull_request.get("reviewThreads") or {}

    comments = _nodes(comments_connection.get("nodes")) + fetch_connection_tail(
        owner, name, pr, COMMENTS_PAGE_QUERY, "comments", comments_connection
    )
    reviews = _nodes(reviews_connection.get("nodes")) + fetch_connection_tail(
        owner, name, pr, REVIEWS_PAGE_QUERY, "reviews", reviews_connection
    )
    review_threads = _nodes(threads_connection.get("nodes")) + fetch_connection_tail(
        owner, name, pr, REVIEW_THREADS_PAGE_QUERY, "reviewThreads", threads_connection
    )

    return merge_pull_request_connections(
        payload, comments=comments, reviews=reviews, review_threads=review_threads
    )


def annotation(thread) -> str:
    path = thread.get("path")
    if not path:
        title = f"unresolved {thread['severity'].upper()} {thread['kind'].replace('_', ' ')}"
        return f"::error title={title}::{thread['url']}"

    line = thread.get("line")
    location = f"{path}:{line}" if line is not None else path
    title = f"unresolved {thread['severity'].upper()} review thread"
    return f"::error file={path},line={line if line is not None else 1},title={title}::{location} {thread['url']}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--repo", help="Repository to inspect, as OWNER/REPO")
    parser.add_argument("--pr", type=int, help="Pull request number")
    parser.add_argument("--min-severity", default="high", help="Minimum severity: low, medium, high, p1, p0")
    parser.add_argument("--include-outdated", action="store_true", help="Include outdated unresolved threads")
    parser.add_argument("--json", dest="json_path", help="Read GraphQL payload from a file instead of gh")
    options = parser.parse_args(argv)

    min_severity = options.min_severity.lower()
    if min_severity not in SEVERITY_RANK:
        print(f"invalid --min-severity {min_severity!r}", file=sys.stderr)
        return 2

    if options.json_path:
        with open(options.json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        missing = [name for name in ("repo", "pr") if not _str(getattr(options, name))]
        if missing:
            print(f"missing required options: {', '.join(missing)}", file=sys.stderr)
            return 2
        payload = fetch_payload(options.repo, options.pr)

    feedback = blocking_feedback(
        payload, min_severity=min_severity, include_outdated=options.include_outdated
    )

    if not feedback:
        print(f"No unresolved PR feedback at or above {min_severity} severity.")
        return 0

    print(
        f"Found {len(feedback)} unresolved PR feedback item(s) at or above {min_severity} severity:",
        file=sys.stderr,
    )
    for thread in feedback:
        if thread.get("path"):
            location = f"{thread['path']}:{thread.get('line') if thread.get('line') is not None else '?'}"
        else:
            location = _str(thread.get("kind"))
        print(f"- [{thread['severity']}] {location} {thread.get('url')}", file=sys.stderr)
        print(annotation(thread))
    return 1


if __name__ == "__main__":
    sys.exit(main())
