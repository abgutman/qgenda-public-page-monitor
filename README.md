# Public QGenda Landing Page Monitor

This repository checks a curated list of public QGenda landing pages every hour. Routine checks are silent. Only a stable page change triggers an alert, and each detected transition is alerted once. When that happens, the workflow:

1. updates the saved page hash and metadata;
2. appends the transition to `.monitor/history.jsonl` and refreshes `.monitor/summary.json`;
3. opens an issue assigned to the repository owner, which uses normal GitHub notification email; and
4. optionally sends a direct SMTP email when email secrets are configured.

The workflow runs at minute 17 of every hour. GitHub Actions schedules are best-effort and may start later during periods of high demand. The scheduled command uses 24 workers, a 10-second request timeout, and one retry so the expanded inventory remains inside the job budget; transient results keep their last known baseline.

## What counts as a change

The monitor checks HTTP status, normalized redirect destination, access state, title, robots directive, section count, public-schedule link count, and a SHA-256 hash of the landing page's visible headings, labels, and link targets. It ignores scripts and other dynamic material outside the public link collection.

SSO query values such as `SAMLRequest` are deliberately removed before comparison. They are one-time authentication tokens, not page changes, and previously caused repetitive hourly alerts. A one-off network error or 5xx response also leaves the last known baseline in place.

New URLs added to `pages.json` are baselined silently. Later changes to those pages generate one alert per transition.

## Change classifications and history

Every alert has one primary classification. The three headline restriction classifications are:

- `schedule_content_removed`: the page remains live, but previously public schedule links disappear without an SSO redirect;
- `page_removed`: a previously available page returns HTTP 404 or 410; and
- `sso_added`: a previously non-SSO page begins redirecting to an institutional identity provider.

The summary reports cumulative event, page, organization, and canonical health-system counts for each classification. Other classifications record restorations, newly public schedule content, and ordinary content or metadata changes. The history stores hashes and metadata only, not schedule contents.

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

To refresh the tracked inventory from the consolidated research index:

```bash
python sync_pages.py ../qgenda/outputs/all_qgenda_resources_20260904/all_page_index.json
```

## Maintenance

Edit `pages.json` to add or remove public landing pages. A weekly heartbeat commit keeps the hourly schedule active because GitHub may disable scheduled workflows in public repositories after prolonged inactivity.
