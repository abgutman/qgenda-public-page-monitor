import unittest
from dataclasses import asdict

from monitor import build_summary, compare_records, preserve_transient_results, snapshot_from_html


HTML = """<!doctype html><html><head>
<title>Example Medical Group</title><meta name="robots" content="noindex, nofollow">
</head><body><div id="linkCollections">
<div><h5>Call schedules</h5><a href="/link/view?linkKey=abc">On Call</a></div>
</div><script>const generated = %r;</script></body></html>"""


class SnapshotTests(unittest.TestCase):
    def test_ignores_dynamic_content_outside_landing_collection(self):
        first = snapshot_from_html("https://example/", "https://example/", 200, HTML % "one")
        second = snapshot_from_html("https://example/", "https://example/", 200, HTML % "two")
        self.assertEqual(first.content_hash, second.content_hash)

    def test_detects_meaningful_link_change(self):
        first = snapshot_from_html("https://example/", "https://example/", 200, HTML % "one")
        changed = (HTML % "one").replace("linkKey=abc", "linkKey=def")
        second = snapshot_from_html("https://example/", "https://example/", 200, changed)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_records_only_counts_not_page_content(self):
        snapshot = snapshot_from_html("https://example/", "https://example/", 200, HTML % "one")
        self.assertEqual(snapshot.title, "Example Medical Group")
        self.assertEqual(snapshot.section_count, 1)
        self.assertEqual(snapshot.link_count, 1)
        self.assertEqual(snapshot.schedule_link_count, 1)
        self.assertEqual(snapshot.access_state, "public_schedule")
        self.assertFalse(hasattr(snapshot, "links"))

    def test_check_timestamp_alone_is_not_a_change(self):
        row = {
            "status": 200,
            "final_url": "https://example/",
            "title": "Example",
            "robots": "noindex",
            "section_count": 1,
            "link_count": 1,
            "content_hash": "abc",
            "error": None,
            "checked_at": "2026-08-24T00:00:00Z",
        }
        later = dict(row, checked_at="2026-08-24T01:00:00Z")
        self.assertEqual(compare_records({"https://example/": row}, {"https://example/": later}), [])

    def test_saml_request_rotation_is_not_a_change(self):
        body = "<html><head><title>Sign in to your account</title></head></html>"
        first = snapshot_from_html(
            "https://app.qgenda.com/landingpage/example",
            "https://login.microsoftonline.com/tenant/saml2?SAMLRequest=one",
            200,
            body,
        )
        second = snapshot_from_html(
            "https://app.qgenda.com/landingpage/example",
            "https://login.microsoftonline.com/tenant/saml2?SAMLRequest=two",
            200,
            body,
        )
        self.assertEqual(first.final_url, second.final_url)
        self.assertEqual(first.access_state, "sso")
        self.assertEqual(
            compare_records({first.url: asdict(first)}, {second.url: asdict(second)}),
            [],
        )

    def test_classifies_schedule_content_removed(self):
        before = snapshot_from_html("https://example/", "https://example/", 200, HTML % "one")
        after_html = "<html><head><title>QGenda</title></head><body>Log in required!</body></html>"
        after = snapshot_from_html("https://example/", "https://example/", 200, after_html)
        pages = {
            "https://example/": {
                "organization": "Example Health",
                "grouping_basis": "Canonical system rule",
                "system": "Example Health",
                "title": "Example",
            }
        }
        changes = compare_records(
            {before.url: asdict(before)},
            {after.url: asdict(after)},
            pages=pages,
            generated_at="2026-09-04T00:00:00Z",
        )
        self.assertEqual(changes[0]["classification"], "schedule_content_removed")
        self.assertEqual(changes[0]["organization"], "Example Health")

    def test_classifies_removed_page_and_added_sso(self):
        before = snapshot_from_html("https://example/", "https://example/", 200, HTML % "one")
        removed = snapshot_from_html("https://example/", "https://example/", 404, "<title>Not found</title>")
        self.assertEqual(
            compare_records({before.url: asdict(before)}, {removed.url: asdict(removed)})[0]["classification"],
            "page_removed",
        )

        sso = snapshot_from_html(
            "https://example/",
            "https://login.example.org/saml?SAMLRequest=abc",
            200,
            "<title>Sign in</title>",
        )
        self.assertEqual(
            compare_records({before.url: asdict(before)}, {sso.url: asdict(sso)})[0]["classification"],
            "sso_added",
        )

    def test_new_page_is_silently_baselined(self):
        snapshot = snapshot_from_html("https://example/", "https://example/", 200, HTML % "one")
        self.assertEqual(compare_records({}, {snapshot.url: asdict(snapshot)}), [])

    def test_version_two_metadata_is_silently_migrated(self):
        current = asdict(
            snapshot_from_html(
                "https://example/",
                "https://example/",
                200,
                "<html><head><title>QGenda</title></head><body>Log in required!</body></html>",
            )
        )
        legacy = {key: value for key, value in current.items() if key not in {"schedule_link_count", "access_state", "sso_detected"}}
        self.assertEqual(compare_records({current["url"]: legacy}, {current["url"]: current}), [])

    def test_transient_error_preserves_last_known_record(self):
        before = asdict(snapshot_from_html("https://example/", "https://example/", 200, HTML % "one"))
        transient = dict(before, status=None, error="timed out", access_state="unavailable")
        records, deferred = preserve_transient_results({before["url"]: before}, {before["url"]: transient})
        self.assertEqual(deferred, 1)
        self.assertEqual(records[before["url"]], before)

    def test_summary_counts_unique_canonical_systems(self):
        event = {
            "url": "https://example/",
            "organization": "Example Health",
            "grouping_basis": "Canonical system rule",
            "system": "Example Health",
            "classification": "schedule_content_removed",
        }
        pages = {
            "https://example/": {
                "url": "https://example/",
                "organization": "Example Health",
                "grouping_basis": "Canonical system rule",
                "system": "Example Health",
            }
        }
        summary = build_summary([event, dict(event)], pages, {}, "2026-09-04T00:00:00Z")
        row = summary["classifications"]["schedule_content_removed"]
        self.assertEqual(row["event_count"], 2)
        self.assertEqual(row["page_count"], 1)
        self.assertEqual(row["system_count"], 1)


if __name__ == "__main__":
    unittest.main()
