# Public QGenda Landing Page Monitor

This repository checks a curated list of public QGenda landing pages every hour. When a page's meaningful public content changes, the workflow:

1. updates the saved page hash and metadata;
2. opens an issue assigned to the repository owner, which uses normal GitHub notification email; and
3. optionally sends a direct SMTP email when email secrets are configured.

The workflow runs at minute 17 of every hour. GitHub Actions schedules are best-effort and may start later during periods of high demand.

## What counts as a change

The monitor checks HTTP status, final URL, title, robots directive, section count, link count, and a SHA-256 hash of the landing page's visible headings, labels, and link targets. It ignores scripts and other dynamic material outside the public link collection to avoid false alerts from deployment hashes or telemetry timestamps.

## Privacy and scope

Only hashes and high-level metadata are committed. The repository does **not** save page HTML, schedule contents, staff names, contact lists, or link labels. All requests are limited to public pages and do not authenticate to QGenda.

This is an independent public-source monitor and is not affiliated with QGenda or any healthcare organization. Public landing pages may declare `noindex, nofollow`, so the page list is best-effort rather than guaranteed exhaustive.

## Email alerts

Change issues mention and are assigned to the repository owner. To receive those by email, enable GitHub email notifications for assigned issues and mentions.

For a separate direct email, create these Actions repository secrets:

- `ALERT_EMAIL`
- `SMTP_HOST`
- `SMTP_PORT` (usually `587` for STARTTLS or `465` for SSL)
- `SMTP_USERNAME`
- `SMTP_PASSWORD` (use an app password, never your normal mailbox password)
- `SMTP_FROM` (optional; defaults to `SMTP_USERNAME`)
- `SMTP_SECURITY` (`starttls`, `ssl`, or `none`; defaults to `starttls`)

## Run locally

```bash
python -m unittest discover -s tests -v
python monitor.py --pages pages.json --state .monitor/state.json --report .monitor/report.json
```

The first run creates a baseline and intentionally sends no alert.

## Maintenance

Edit `pages.json` to add or remove public landing pages. A weekly heartbeat commit keeps the hourly schedule active because GitHub may disable scheduled workflows in public repositories after prolonged inactivity.
