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
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


USER_AGENT = "QGendaPublicLandingPageMonitor/1.0 (+GitHub Actions)"
STATE_VERSION = 2
HISTORY_VERSION = 1
CHANGE_FIELDS = (
    "status",
    "final_url",
    "title",
    "robots",
    "section_count",
    "link_count",
    "schedule_link_count",
    "access_state",
    "sso_detected",
    "content_hash",
)
TRANSIENT_HTTP_STATUSES = {408, 425, 429}
SUMMARY_CLASSIFICATIONS = ("schedule_content_removed", "page_removed", "sso_added")
VERSION_2_FIELDS = {"schedule_link_count", "access_state", "sso_detected"}
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


def normalized_final_url(value: str) -> str:
    """Keep a redirect destination while dropping volatile auth/query values."""

    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "", ""))


def is_schedule_link(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    return "/link/view" in parsed.path.lower() and "linkkey" in query_keys


def is_sso_destination(value: str, title: str = "") -> bool:
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    title_signal = bool(re.search(r"\b(sign[ -]?in|log[ -]?in|identity login)\b", title, re.I))
    auth_signal = (
        "samlrequest" in query_keys
        or any(part in path for part in ("/saml", "/adfs/", "/oauth", "/signin", "/login"))
        or host.startswith(("login.", "sso.", "idp.", "fs."))
        or any(part in host for part in ("okta", "onelogin", "auth0", "microsoftonline"))
        or title_signal
    )
    return bool(host and not host.endswith("qgenda.com") and auth_signal)


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
    schedule_link_count: int
    access_state: str
    sso_detected: bool
    content_hash: str
    error: str | None
    checked_at: str


def snapshot_from_html(url: str, final_url: str, status: int, body: str, checked_at: str | None = None) -> Snapshot:
    parser = LandingPageParser(final_url)
    parser.feed(body)
    canonical = json.dumps(parser.canonical_content(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    schedule_link_count = sum(is_schedule_link(link["href"]) for link in parser.links)
    sso_detected = is_sso_destination(final_url, parser.title)
    login_required = (
        status in {401, 403}
        or "landing-page-outside-network-message" in body
        or bool(re.search(r"Log in required!", body, re.I))
    )
    if status in {404, 410}:
        access_state = "removed"
    elif sso_detected:
        access_state = "sso"
    elif login_required:
        access_state = "login_required"
    elif 200 <= status < 400 and schedule_link_count:
        access_state = "public_schedule"
    elif 200 <= status < 400:
        access_state = "live_no_schedule"
    else:
        access_state = "http_error"
    return Snapshot(
        url=url,
        status=status,
        final_url=normalized_final_url(final_url),
        title=parser.title,
        robots=parser.robots,
        section_count=len(parser.headings),
        link_count=len(parser.links),
        schedule_link_count=schedule_link_count,
        access_state=access_state,
        sso_detected=sso_detected,
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
    return Snapshot(url, None, url, "", "", 0, 0, 0, "unavailable", False, "", message[:500], checked_at)


def load_pages(path: Path) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("pages", payload) if isinstance(payload, dict) else payload
    pages: dict[str, dict[str, str]] = {}
    for row in rows:
        source = row if isinstance(row, dict) else {"url": row}
        url = str(source.get("url", "")).strip()
        if not url:
            continue
        pages[url] = {
            "url": url,
            "slug": str(source.get("slug", "")).strip(),
            "title": str(source.get("title", "")).strip(),
            "organization": str(source.get("organization", "")).strip(),
            "grouping_basis": str(source.get("grouping_basis", "")).strip(),
            "system": str(source.get("system", "")).strip(),
        }
    if not pages:
        raise ValueError("pages file is empty")
    return dict(sorted(pages.items()))


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") not in {1, STATE_VERSION}:
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
        "schedule_link_count": row.get("schedule_link_count"),
        "access_state": row.get("access_state"),
        "sso_detected": row.get("sso_detected"),
        "content_hash": row.get("content_hash"),
        "error": row.get("error"),
    }


def stable_record(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize current and legacy snapshots into comparable version-2 metadata."""

    result = dict(row)
    result["final_url"] = normalized_final_url(str(result.get("final_url", "")))
    if "schedule_link_count" not in result:
        result["schedule_link_count"] = int(result.get("link_count") or 0)
    if "sso_detected" not in result:
        result["sso_detected"] = is_sso_destination(str(row.get("final_url", "")), str(row.get("title", "")))
    if "access_state" not in result:
        status = result.get("status")
        if status in {404, 410}:
            result["access_state"] = "removed"
        elif result["sso_detected"]:
            result["access_state"] = "sso"
        elif status is None or result.get("error") or (isinstance(status, int) and status >= 500):
            result["access_state"] = "unavailable"
        elif status in {401, 403}:
            result["access_state"] = "login_required"
        elif 200 <= status < 400 and result["schedule_link_count"]:
            result["access_state"] = "public_schedule"
        elif 200 <= status < 400:
            result["access_state"] = "live_no_schedule"
        else:
            result["access_state"] = "http_error"
    return result


def classify_transition(before: dict[str, Any], after: dict[str, Any], changed_fields: list[str]) -> str:
    before_state = before.get("access_state")
    after_state = after.get("access_state")
    if after_state == "removed" and before_state != "removed":
        return "page_removed"
    if after_state == "sso" and before_state != "sso":
        return "sso_added"
    if (
        int(before.get("schedule_link_count") or 0) > 0
        and int(after.get("schedule_link_count") or 0) == 0
        and after_state not in {"removed", "sso", "unavailable", "http_error"}
    ):
        return "schedule_content_removed"
    if before_state == "removed" and after_state != "removed":
        return "page_restored"
    if before_state == "sso" and after_state != "sso":
        return "sso_removed"
    if int(before.get("schedule_link_count") or 0) == 0 and int(after.get("schedule_link_count") or 0) > 0:
        return "schedule_content_added"
    if "schedule_link_count" in changed_fields or "content_hash" in changed_fields:
        return "page_content_changed"
    return "page_metadata_changed"


def compare_records(
    previous: dict[str, Any],
    current: dict[str, Any],
    pages: dict[str, dict[str, str]] | None = None,
    generated_at: str | None = None,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for url in sorted(current):
        before_raw = previous.get(url)
        after = stable_record(current[url])
        # Adding a URL to the tracker establishes a baseline; it is not a
        # change made by the page owner and must not generate an alert.
        if before_raw is None:
            continue
        before = stable_record(before_raw)
        changed_fields = [
            field
            for field in CHANGE_FIELDS
            if before.get(field) != after.get(field)
            and not (field in VERSION_2_FIELDS and field not in before_raw)
        ]
        if changed_fields:
            metadata = (pages or {}).get(url, {})
            classification = classify_transition(before, after, changed_fields)
            event_basis = json.dumps(
                [generated_at or after.get("checked_at"), url, public_view(before), public_view(after)],
                sort_keys=True,
                separators=(",", ":"),
            )
            changes.append(
                {
                    "event_id": hashlib.sha256(event_basis.encode("utf-8")).hexdigest(),
                    "detected_at": generated_at or after.get("checked_at") or utc_now(),
                    "url": url,
                    "organization": metadata.get("organization", ""),
                    "grouping_basis": metadata.get("grouping_basis", ""),
                    "system": metadata.get("system", ""),
                    "tracked_title": metadata.get("title", ""),
                    "classification": classification,
                    "changed_fields": changed_fields,
                    "before": public_view(before),
                    "after": public_view(after),
                }
            )
    return changes


def is_transient_snapshot(row: dict[str, Any]) -> bool:
    status = row.get("status")
    return bool(
        row.get("error")
        or status is None
        or status in TRANSIENT_HTTP_STATUSES
        or (isinstance(status, int) and status >= 500)
    )


def preserve_transient_results(
    previous: dict[str, Any], current: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    deferred = 0
    for url, row in sorted(current.items()):
        if is_transient_snapshot(row) and url in previous:
            result[url] = previous[url]
            deferred += 1
        else:
            result[url] = row
    return result, deferred


def read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid history JSON on line {line_number}") from exc
    return events


def write_history(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_summary(
    events: list[dict[str, Any]],
    pages: dict[str, dict[str, str]],
    records: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    classification_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        classification_rows[event.get("classification", "unclassified")].append(event)
    classifications: dict[str, Any] = {}
    for classification in sorted(set(classification_rows) | set(SUMMARY_CLASSIFICATIONS)):
        rows = classification_rows[classification]
        organizations = sorted({row.get("organization", "") for row in rows if row.get("organization")})
        systems = sorted({row.get("system", "") for row in rows if row.get("system")})
        classifications[classification] = {
            "event_count": len(rows),
            "page_count": len({row.get("url") for row in rows}),
            "organization_count": len(organizations),
            "system_count": len(systems),
            "systems": systems,
        }
    current_states = Counter(stable_record(row).get("access_state", "unknown") for row in records.values())
    return {
        "version": HISTORY_VERSION,
        "updated_at": generated_at,
        "tracked_inventory": {
            "page_count": len(pages),
            "organization_count": len({row["organization"] for row in pages.values() if row["organization"]}),
            "canonical_system_count": len(
                {
                    row["organization"]
                    for row in pages.values()
                    if row["organization"] and row["grouping_basis"] == "Canonical system rule"
                }
            ),
            "mapped_system_count": len({row["system"] for row in pages.values() if row["system"]}),
        },
        "history": {
            "event_count": len(events),
            "page_count": len({event.get("url") for event in events}),
        },
        "current_page_states": dict(sorted(current_states.items())),
        "classifications": classifications,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=Path, default=Path("pages.json"))
    parser.add_argument("--state", type=Path, default=Path(".monitor/state.json"))
    parser.add_argument("--report", type=Path, default=Path(".monitor/report.json"))
    parser.add_argument("--history", type=Path, default=Path(".monitor/history.jsonl"))
    parser.add_argument("--summary", type=Path, default=Path(".monitor/summary.json"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-error-rate", type=float, default=0.10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pages = load_pages(args.pages)
    urls = list(pages)
    previous_state = load_state(args.state)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_snapshot, url, args.timeout, args.attempts): url for url in urls}
        snapshots = [future.result() for future in concurrent.futures.as_completed(futures)]
    fetched_records = {snapshot.url: asdict(snapshot) for snapshot in snapshots}
    records = dict(sorted(fetched_records.items()))
    error_count = sum(row["error"] is not None for row in records.values())
    transient_count = sum(is_transient_snapshot(row) for row in records.values())
    generated_at = utc_now()
    allowed_errors = max(5, int(len(urls) * args.max_error_rate))
    if transient_count > allowed_errors:
        report = {
            "generated_at": generated_at,
            "aborted": True,
            "reason": f"{transient_count} unavailable fetches exceeded safety threshold {allowed_errors}",
            "page_count": len(urls),
            "error_count": error_count,
            "transient_count": transient_count,
            "change_count": 0,
            "changes": [],
        }
        atomic_write_json(args.report, report)
        print(report["reason"], file=sys.stderr)
        return 2
    previous_records = (previous_state or {}).get("records", {})
    records, deferred_count = preserve_transient_results(previous_records, records)
    records = {url: stable_record(row) for url, row in sorted(records.items())}
    changes = (
        []
        if previous_state is None
        else compare_records(previous_records, records, pages=pages, generated_at=generated_at)
    )
    baseline_addition_count = len(set(records) - set(previous_records))
    migrated = previous_state is not None and previous_state.get("version") != STATE_VERSION
    state = {"version": STATE_VERSION, "generated_at": generated_at, "records": records}
    history = read_history(args.history)
    if changes:
        history.extend(changes)
    should_save = previous_state is None or bool(changes) or bool(baseline_addition_count) or migrated
    if should_save:
        summary = build_summary(history, pages, records, generated_at)
        atomic_write_json(args.state, state)
        write_history(args.history, history)
        atomic_write_json(args.summary, summary)
    elif args.summary.exists():
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
    else:
        summary = build_summary(history, pages, records, generated_at)
    headline_counts = {
        classification: summary["classifications"][classification]
        for classification in SUMMARY_CLASSIFICATIONS
    }
    report = {
        "generated_at": generated_at,
        "aborted": False,
        "initialized": previous_state is None,
        "migrated": migrated,
        "page_count": len(urls),
        "error_count": error_count,
        "transient_count": transient_count,
        "deferred_count": deferred_count,
        "baseline_addition_count": baseline_addition_count,
        "change_count": len(changes),
        "changes": changes,
        "cumulative_classifications": headline_counts,
    }
    atomic_write_json(args.report, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "initialized",
                    "migrated",
                    "page_count",
                    "error_count",
                    "deferred_count",
                    "baseline_addition_count",
                    "change_count",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
