#!/usr/bin/env python3
"""Send an optional direct SMTP email for a monitor change report."""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def report_body(report: dict) -> str:
    lines = [
        f"QGenda public landing-page changes detected: {report['change_count']}",
        f"Checked at: {report['generated_at']}",
        "",
    ]
    for change in report["changes"]:
        lines.extend(
            [
                change["url"],
                "Changed: " + ", ".join(change["changed_fields"]),
                f"Before: {json.dumps(change['before'], sort_keys=True)}",
                f"After:  {json.dumps(change['after'], sort_keys=True)}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path(".monitor/report.json"))
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not report.get("change_count"):
        print("No changes; no email sent.")
        return 0

    config = {
        "to": os.environ.get("ALERT_EMAIL", "").strip(),
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
    }
    if not any(config.values()):
        print("SMTP not configured; the GitHub issue notification remains active.")
        return 0
    missing = [key for key, value in config.items() if not value]
    if missing:
        raise SystemExit("Incomplete SMTP configuration; missing: " + ", ".join(missing))

    port = int(os.environ.get("SMTP_PORT") or "587")
    security = (os.environ.get("SMTP_SECURITY") or "starttls").strip().lower()
    sender = (os.environ.get("SMTP_FROM") or config["username"]).strip()
    message = EmailMessage()
    message["Subject"] = f"QGenda alert: {report['change_count']} landing page change(s)"
    message["From"] = sender
    message["To"] = config["to"]
    message.set_content(report_body(report))

    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(config["host"], port, context=context, timeout=30) as smtp:
            smtp.login(config["username"], config["password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config["host"], port, timeout=30) as smtp:
            if security == "starttls":
                smtp.starttls(context=context)
            elif security != "none":
                raise SystemExit("SMTP_SECURITY must be starttls, ssl, or none")
            smtp.login(config["username"], config["password"])
            smtp.send_message(message)
    print("Alert email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
