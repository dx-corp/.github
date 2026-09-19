#!/usr/bin/env python3
"""Classify PR commit authorship from a JSONL stream of commits.

Port of classify-agent-authorship.rb. Ruby is not present on every runner
image this reusable workflow runs on, so the classifier uses python3, which
is proven to exist on all of them. Standard library only.
"""

import argparse
import json
import re
import sys

# Ruby \s / \S semantics are ASCII-only; keep re.ASCII so unicode whitespace
# does not change trailer matching relative to the Ruby original.
FLAGS = re.IGNORECASE | re.ASCII

REQUIRED_PATTERNS = {
    "co_author": re.compile(r"^Co-Authored-By:\s*Maestro\s+<maestro@evalops\.dev>\s*$", FLAGS),
    "version": re.compile(r"^Maestro-Version:\s*\S.*$", FLAGS),
    "prompt_id": re.compile(r"^Maestro-Prompt-Id:\s*\S.*$", FLAGS),
    "approvals_id": re.compile(r"^Maestro-Approvals-Id:\s*\S.*$", FLAGS),
}

MARKER_PATTERN = re.compile(
    r"^Co-Authored-By:\s*Maestro\s+<maestro@evalops\.dev>\s*$"
    r"|^Maestro-(?:Version|Prompt-Id|Approvals-Id):",
    FLAGS,
)


def read_input(paths):
    if paths:
        chunks = []
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                chunks.append(handle.read())
        return "".join(chunks)
    return sys.stdin.read()


def extract_messages(text):
    messages = []
    # Split only on "\n" to match Ruby's each_line: jq emits characters like
    # U+2028/U+2029 literally inside JSON strings, and str.splitlines() would
    # tear one JSON record into unparseable fragments.
    for line in text.split("\n"):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            commit = parsed.get("commit")
            message = commit.get("message") if isinstance(commit, dict) else None
            if message is None:
                message = parsed.get("message")
            if message is not None:
                messages.append(message)
    return messages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--github-output",
        metavar="PATH",
        help="Append key=value outputs for GitHub Actions",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()

    messages = extract_messages(read_input(args.files))

    agent_commits = 0
    untrailered_commits = 0
    incomplete_commits = 0

    for message in messages:
        # Ruby each_line splits on "\n" only; keep that exact segmentation.
        parts = message.split("\n")
        lines = [part + "\n" for part in parts[:-1]]
        if parts[-1]:
            lines.append(parts[-1])
        has_marker = any(MARKER_PATTERN.search(line) for line in lines)

        if not has_marker:
            untrailered_commits += 1
            continue

        agent_commits += 1
        missing_required = any(
            not any(pattern.search(line) for line in lines)
            for pattern in REQUIRED_PATTERNS.values()
        )
        if missing_required:
            incomplete_commits += 1

    if agent_commits > 0 and untrailered_commits > 0:
        label = "mixed-authorship"
    elif agent_commits > 0:
        label = "agent-authored"
    else:
        label = "agent-assisted"

    outputs = {
        "label": label,
        "total_commits": len(messages),
        "agent_commits": agent_commits,
        "untrailered_commits": untrailered_commits,
        "human_commits": untrailered_commits,
        "incomplete_agent_commits": incomplete_commits,
    }

    for key, value in outputs.items():
        print(f"{key}={value}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
