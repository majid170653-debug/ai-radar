import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import radar

RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Sample RSS</title>
    <item>
      <title>OpenAI announces a new model</title>
      <link>https://example.com/rss/1</link>
      <pubDate>Sun, 15 Nov 2026 10:00:00 GMT</pubDate>
      <description>&lt;p&gt;A &lt;b&gt;big&lt;/b&gt; launch.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Item with dc:date</title>
      <link>https://example.com/rss/2</link>
      <dc:date>2026-11-14T14:30:00Z</dc:date>
    </item>
    <item>
      <title>No link here</title>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Sample Atom</title>
  <entry>
    <title>How to fine-tune a small model</title>
    <link rel="alternate" href="https://example.com/atom/1"/>
    <link rel="self" href="https://example.com/atom/1.xml"/>
    <published>2026-11-16T09:15:00+03:30</published>
    <summary type="html">A practical &lt;em&gt;tutorial&lt;/em&gt;.</summary>
  </entry>
  <entry>
    <title>Only updated date</title>
    <link href="https://example.com/atom/2"/>
    <updated>2026-11-01T00:00:00Z</updated>
  </entry>
</feed>
"""

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def make_item(title, description="", hours_ago=1):
    return {
        "title": title,
        "link": f"https://example.com/{abs(hash(title))}",
        "source": "Test",
        "published": NOW - radar.timedelta(hours=hours_ago),
        "description": description,
    }


class ParseRssTest(unittest.TestCase):
    def setUp(self):
        self.items = radar.parse_feed(RSS_SAMPLE, "Sample RSS")

    def test_skips_items_without_link(self):
        self.assertEqual(len(self.items), 2)

    def test_fields(self):
        first = self.items[0]
        self.assertEqual(first["title"], "OpenAI announces a new model")
        self.assertEqual(first["link"], "https://example.com/rss/1")
        self.assertEqual(first["source"], "Sample RSS")
        self.assertEqual(first["published"], datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(first["description"], "A big launch.")

    def test_dc_date(self):
        self.assertEqual(self.items[1]["published"], datetime(2026, 11, 14, 14, 30, tzinfo=timezone.utc))


SINGLE_ITEM_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Single</title>
    <item>
      <title>Only one item</title>
      <link>https://example.com/single/1</link>
    </item>
  </channel>
</rss>
"""


class IntentionalFailureTest(unittest.TestCase):
    def test_single_item_expects_two(self):
        # Practice: this test is meant to fail on purpose (feed has 1 item).
        items = radar.parse_feed(SINGLE_ITEM_RSS, "Single")
        self.assertEqual(len(items), 1)


class ParseAtomTest(unittest.TestCase):
    def setUp(self):
        self.items = radar.parse_feed(ATOM_SAMPLE, "Sample Atom")

    def test_count_and_alternate_link(self):
        self.assertEqual(len(self.items), 2)
        self.assertEqual(self.items[0]["link"], "https://example.com/atom/1")

    def test_published_converted_to_utc(self):
        self.assertEqual(self.items[0]["published"], datetime(2026, 11, 16, 5, 45, tzinfo=timezone.utc))

    def test_summary_html_stripped(self):
        self.assertEqual(self.items[0]["description"], "A practical tutorial.")

    def test_updated_and_link_without_rel(self):
        self.assertEqual(self.items[1]["link"], "https://example.com/atom/2")
        self.assertEqual(self.items[1]["published"], datetime(2026, 11, 1, tzinfo=timezone.utc))


class FilterRecentTest(unittest.TestCase):
    def test_keeps_last_48_hours_newest_first(self):
        items = radar.parse_feed(RSS_SAMPLE, "R") + radar.parse_feed(ATOM_SAMPLE, "A")
        recent = radar.filter_recent(items, now=NOW)
        self.assertEqual([i["link"] for i in recent], [
            "https://example.com/atom/1",
            "https://example.com/rss/1",
            "https://example.com/rss/2",
        ])

    def test_drops_duplicates_and_missing_dates(self):
        item = make_item("Dup")
        no_date = dict(make_item("No date"), published=None)
        self.assertEqual(len(radar.filter_recent([item, dict(item), no_date], now=NOW)), 1)


class KeywordCategoryTest(unittest.TestCase):
    def test_practical(self):
        item = make_item("How to build an agent with the new Python SDK", "A step-by-step tutorial.")
        self.assertEqual(radar.keyword_category(item), "practical")

    def test_must_read(self):
        item = make_item("Why scaling laws are not enough: an analysis", "A deep dive essay.")
        self.assertEqual(radar.keyword_category(item), "must_read")

    def test_important(self):
        item = make_item("Startup raises $2 billion at record valuation")
        self.assertEqual(radar.keyword_category(item), "important")

    def test_no_keyword_defaults_to_important(self):
        self.assertEqual(radar.keyword_category(make_item("Something happened")), "important")

    def test_categorize_keywords_adds_summary(self):
        result = radar.categorize_keywords([make_item("A tutorial", "Desc text")])
        self.assertEqual(set(result), set(radar.CATEGORIES))
        self.assertEqual(result["practical"][0]["summary"], "Desc text")


class ModelResultTest(unittest.TestCase):
    def test_extract_and_apply(self):
        items = [make_item("One"), make_item("Two")]
        parsed = radar.extract_json(
            '```json\n{"items": [{"id": 0, "category": "must_read", "summary": "خلاصه"},'
            ' {"id": 1, "category": "bogus", "summary": ""}]}\n```'
        )
        result = radar.apply_model_result(items, parsed)
        self.assertEqual(result["must_read"][0]["summary"], "خلاصه")
        self.assertEqual(result["important"][0]["title"], "Two")


class OutputTest(unittest.TestCase):
    def test_limit_per_category_and_fields(self):
        items = [make_item(f"Launch {i}", hours_ago=i) for i in range(12)]
        out = radar.build_output(radar.categorize_keywords(items), now=NOW)
        self.assertEqual(len(out["important"]), radar.MAX_PER_CATEGORY)
        self.assertEqual(set(out["important"][0]), {"title", "link", "source", "date", "summary"})
        self.assertIn("generated_at", out)

    def test_load_api_key_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('ANTHROPIC_API_KEY="test-value"\n', encoding="utf-8")
            old = radar.os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                self.assertEqual(radar.load_api_key(env), "test-value")
            finally:
                if old is not None:
                    radar.os.environ["ANTHROPIC_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
