import tempfile
import unittest
from pathlib import Path

from tools.batch_pipeline import connect
from tools.news_topics import ingest, parse_feed


RSS = b'''<?xml version="1.0"?><rss><channel>
<item><title>Example &amp; event</title><link>https://example.test/a</link><description>Useful <b>summary</b></description></item>
<item><title>Example &amp; event</title><link>https://example.test/a</link></item>
</channel></rss>'''


class NewsTopicTest(unittest.TestCase):
    def test_parse_and_deduplicate_feed_items(self):
        items = parse_feed("https://example.test/rss", RSS)
        self.assertEqual(items[0]["title"], "Example & event")
        self.assertEqual(items[0]["summary"], "Useful summary")
        self.assertEqual(len(items), 2)
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "production.sqlite3"
            import tools.news_topics as news_topics
            news_topics.fetch = lambda *_args, **_kwargs: items
            added, errors = ingest(database, ["https://example.test/rss"])
            self.assertEqual((added, errors), (1, []))
            with connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM news_topics").fetchone()[0], 1)

    def test_parse_html_channel_page(self):
        html = b'''<html><body><h1>News</h1>
        <a href="/world/202609/08-article.shtml">A meaningful article title</a>
        <a href="/world/202609/08-article.shtml">A meaningful article title</a>
        <a href="https://other.example/x">External link should be ignored</a>
        <a href="/about">About us</a>
        </body></html>'''
        items = parse_feed("https://channel.example/news.shtml", html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["article_url"], "https://channel.example/world/202609/08-article.shtml")


if __name__ == "__main__":
    unittest.main()
