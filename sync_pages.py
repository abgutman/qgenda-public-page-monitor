#!/usr/bin/env python3
"""Build pages.json from the consolidated QGenda page index."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


SYSTEM_OVERRIDES = (
    (r"^(?:penn|upenn|uphs)", "Penn Medicine"),
    (r"^uhc", "University Hospitals"),
    (r"^hm[a-z]*call$", "Houston Methodist"),
    (r"^(?:kmc|kmsqmc)$", "Hawaii Pacific Health"),
    (r"^(?:auhs|tacs)$", "Wellstar MCG Health"),
    (r"^rileyanesthesia$", "IU Health"),
    (r"^wvuhospitalists$", "WVU Medicine"),
    (r"^uvaneph$", "UVA Health"),
)


def mapped_system(row: dict) -> str:
    slug = str(row.get("slug_or_directory_id", "")).strip().lower()
    for pattern, system in SYSTEM_OVERRIDES:
        if re.search(pattern, slug):
            return system
    if row.get("grouping_basis") == "Canonical system rule":
        return str(row.get("organization", "")).strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--output", type=Path, default=Path("pages.json"))
    args = parser.parse_args()

    source = json.loads(args.inventory.read_text(encoding="utf-8"))
    if not isinstance(source, list):
        raise SystemExit("inventory must be a JSON list")

    pages: dict[str, dict[str, str]] = {}
    checked_dates: list[str] = []
    for row in source:
        url = str(row.get("qgenda_url", "")).strip()
        if not url:
            continue
        checked_date = str(row.get("checked_date", "")).strip()
        if checked_date:
            checked_dates.append(checked_date)
        pages[url] = {
            "slug": str(row.get("slug_or_directory_id", "")).strip(),
            "url": url,
            "title": str(row.get("current_title", "")).strip(),
            "organization": str(row.get("organization", "")).strip(),
            "grouping_basis": str(row.get("grouping_basis", "")).strip(),
            "system": mapped_system(row),
        }

    ordered = sorted(pages.values(), key=lambda row: row["url"].lower())
    payload = {
        "source_inventory": str(args.inventory),
        "source_inventory_checked_date": max(checked_dates, default=""),
        "page_count": len(ordered),
        "pages": ordered,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    print(json.dumps({"page_count": len(ordered), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
