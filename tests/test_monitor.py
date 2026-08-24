import unittest

from monitor import compare_records, snapshot_from_html


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


if __name__ == "__main__":
    unittest.main()
