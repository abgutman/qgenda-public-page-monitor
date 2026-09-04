#!/usr/bin/env python3
"""Reconstruct meaningful tracker history from committed state snapshots."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from monitor import atomic_write_json, build_summary, compare_records, load_pages, write_history


def git_output(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=Path, default=Path("pages.json"))
    parser.add_argument("--history", type=Path, default=Path(".monitor/history.jsonl"))
    parser.add_argument("--summary", type=Path, default=Path(".monitor/summary.json"))
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    if args.history.exists() and not args.replace:
        raise SystemExit(f"{args.history} already exists; pass --replace to rebuild it")

    pages = load_pages(args.pages)
    commits = git_output("rev-list", "--reverse", "HEAD", "--", ".monitor/state.json").decode().splitlines()
    states: list[dict] = []
    for commit in commits:
        raw = git_output("show", f"{commit}:.monitor/state.json")
        states.append(json.loads(raw))
    if not states:
        raise SystemExit("no committed monitor state snapshots found")

    events: list[dict] = []
    previous = states[0].get("records", {})
    for state in states[1:]:
        current = state.get("records", {})
        events.extend(
            compare_records(
                previous,
                current,
                pages=pages,
                generated_at=state.get("generated_at"),
            )
        )
        previous = current

    latest = states[-1]
    summary = build_summary(events, pages, latest.get("records", {}), latest.get("generated_at", ""))
    write_history(args.history, events)
    atomic_write_json(args.summary, summary)
    headline = {
        key: summary["classifications"][key]
        for key in ("schedule_content_removed", "page_removed", "sso_added")
    }
    print(json.dumps({"commits": len(commits), "events": len(events), "headline": headline}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
