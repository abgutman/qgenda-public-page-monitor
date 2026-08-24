#!/usr/bin/env python3
"""Monitor public QGenda landing pages without storing their page contents."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


USER_AGENT = "QGendaPublicLandingPageMonitor/1.0 (+GitHub Actions)"
STATE_VERSION = 1
CHANGE_FIELDS = (
    "status",
    "final_url",
    "title",
    "robots",
    "section_count",
    "link_count",
    "content_hash",
    "error",
)
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


class LandingPageParser(HTMLParser):
    """Extract stable, meaningful public landing-page content."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.in_title = False
        self.title_parts: list[str] = []
        self.robots = ""
        self.content_depth: int | None = None
        self.depth = 0
        self.skip_depth = 0
        self.visible_parts: list[str] = []
        self.current_anchor: dict[str, Any] | None = None
        self.links: list[dict[str, str]] = []
        self.current_heading: list[str] | None = None
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag not in VOID_ELEMENTS:
            self.depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta" and values.get("name", "").lower() == "robots":
            self.robots = clean_text(values.get("content", ""))
        if tag == "div" and values.get("id") == "linkCollections":
            self.content_depth = self.depth
        if self.content_depth is not None and tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if self.content_depth is not None and self.skip_depth == 0:
            if tag == "a":
                href = urllib.parse.urljoin(self.base_url, values.get("href", ""))
                self.current_anchor = {"href": href, "parts": []}
            elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.current_heading = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if self.content_depth is not None and tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if self.content_depth is not None and self.skip_depth == 0:
            if tag == "a" and self.current_anchor is not None:
                label = clean_text(" ".join(self.current_anchor["parts"]))
                self.links.append({"text": label, "href": self.current_anchor["href"]})
                self.current_anchor = None
            elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.current_heading is not None:
                heading = clean_text(" ".join(self.current_heading))
                if heading:
                    self.headings.append(heading)
                self.current_heading = None
        if self.content_depth is not None and self.depth == self.content_depth:
            self.content_depth = None
        if tag not in VOID_ELEMENTS:
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.content_depth is None or self.skip_depth:
            return
        cleaned = clean_text(data)
        if not cleaned:
            return
        self.visible_parts.append(cleaned)
        if self.current_anchor is not None:
            self.current_anchor["parts"].append(cleaned)
        if self.current_heading is not None:
            self.current_heading.append(cleaned)

    @property
    def title(self) -> str:
        return clean_text(" ".join(self.title_parts))

    def canonical_content(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "robots": self.robots,
            "visible_text": self.visible_parts,
            "headings": self.headings,
            "links": self.links,
        }


@dataclass
class Snapshot:
    url: str
    status: int | None
    final_url: str
    title: str
    robots: str
    section_count: int
    link_count: int
    content_hash: str
    error: str | None
    checked_at: str


def snapshot_from_html(url: str, final_url: str, status: int, body: str, checked_at: str | None = None) -> Snapshot:
    parser = LandingPageParser(final_url)
    parser.feed(body)
    canonical = json.dumps(parser.canonical_content(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return Snapshot(
        url=url,
        status=status,
        final_url=final_url,
        title=parser.title,
        robots=parser.robots,
        section_count=len(parser.headings),
        link_count=len(parser.links),
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        error=None,
        checked_at=checked_at or utc_now(),
    )


def request_page(url: str, timeout: int) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, response.geturl(), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, exc.geturl(), body


def fetch_snapshot(url: str, timeout: int, attempts: int = 3) -> Snapshot:
    checked_at = utc_now()
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            status, final_url, body = request_page(url, timeout)
            if status >= 500 and attempt + 1 < attempts:
                time.sleep(1 + attempt)
                continue
            return snapshot_from_html(url, final_url, status, body, checked_at)
        except Exception as exc:  # network and TLS failures are represented in state
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    message = clean_text(str(last_error or "unknown fetch error"))
    message = re.sub(r"(?i)(token|password|secret)=[^\s&]+", r"\1=[redacted]", message)
    return Snapshot(url, None, url, "", "", 0, 0, "", message[:500], checked_at)


def load_pages(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("pages", payload) if isinstance(payload, dict) else payload
    urls = [row["url"] if isinstance(row, dict) else row for row in rows]
    normalized = sorted({str(url).strip() for url in urls if str(url).strip()})
    if not normalized:
        raise ValueError("pages file is empty")
    return normalized


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported state version: {payload.get('version')}")
    return payload


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def public_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "status": row.get("status"),
        "final_url": row.get("final_url"),
        "title": row.get("title"),
        "robots": row.get("robots"),
        "section_count": row.get("section_count"),
        "link_count": row.get("link_count"),
        "content_hash": row.get("content_hash"),
        "error": row.get("error"),
    }


def compare_records(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for url in sorted(current):
        before = previous.get(url)
        after = current[url]
        if before is None:
            changed_fields = ["new_page"]
        else:
            changed_fields = [field for field in CHANGE_FIELDS if before.get(field) != after.get(field)]
        if changed_fields:
            changes.append(
                {
                    "url": url,
                    "changed_fields": changed_fields,
                    "before": public_view(before),
                    "after": public_view(after),
                }
            )
    return changes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=Path, default=Path("pages.json"))
    parser.add_argument("--state", type=Path, default=Path(".monitor/state.json"))
    parser.add_argument("--report", type=Path, default=Path(".monitor/report.json"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-error-rate", type=float, default=0.10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    urls = load_pages(args.pages)
    previous_state = load_state(args.state)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_snapshot, url, args.timeout): url for url in urls}
        snapshots = [future.result() for future in concurrent.futures.as_completed(futures)]
    records = {snapshot.url: asdict(snapshot) for snapshot in snapshots}
    error_count = sum(row["error"] is not None for row in records.values())
    generated_at = utc_now()
    allowed_errors = max(5, int(len(urls) * args.max_error_rate))
    if error_count > allowed_errors:
        report = {
            "generated_at": generated_at,
            "aborted": True,
            "reason": f"{error_count} fetch errors exceeded safety threshold {allowed_errors}",
            "page_count": len(urls),
            "error_count": error_count,
            "change_count": 0,
            "changes": [],
        }
        atomic_write_json(args.report, report)
        print(report["reason"], file=sys.stderr)
        return 2
    previous_records = (previous_state or {}).get("records", {})
    changes = [] if previous_state is None else compare_records(previous_records, records)
    state = {"version": STATE_VERSION, "generated_at": generated_at, "records": records}
    report = {
        "generated_at": generated_at,
        "aborted": False,
        "initialized": previous_state is None,
        "page_count": len(urls),
        "error_count": error_count,
        "change_count": len(changes),
        "changes": changes,
    }
    # Preserve the committed baseline on no-change runs. This prevents an
    # hourly commit caused only by generated/checked timestamps.
    if previous_state is None or changes:
        atomic_write_json(args.state, state)
    atomic_write_json(args.report, report)
    print(json.dumps({key: report[key] for key in ("initialized", "page_count", "error_count", "change_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
