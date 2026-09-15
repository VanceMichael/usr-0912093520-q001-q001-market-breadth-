import tempfile
import unittest
from pathlib import Path

from tools.batch_pipeline import connect
from tools.news_topics import (
    FeedItems, canonical_article_url, ingest, ingest_report, parse_feed, parse_next_page,
)


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

    def test_existing_topics_do_not_turn_a_healthy_feed_into_an_error(self):
        items = [{
            "source_url": "https://example.test/rss",
            "article_url": "https://example.test/a",
            "title": "A meaningful article title",
            "summary": "",
            "published_at": "",
        }]
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "production.sqlite3"
            import tools.news_topics as news_topics
            news_topics.fetch = lambda *_args, **_kwargs: items
            self.assertEqual(ingest(database, ["https://example.test/rss"]), (1, []))
            self.assertEqual(ingest(database, ["https://example.test/rss"]), (0, []))

    def test_tracking_url_and_mirrored_content_are_deduplicated(self):
        first = FeedItems([{
            "source_url": "https://news.example/list",
            "article_url": "https://news.example/a?utm_source=feed&id=7#top",
            "title": "A meaningful shared business event",
            "summary": "The same business facts",
            "published_at": "",
        }])
        mirror = FeedItems([{
            "source_url": "https://mirror.example/list",
            "article_url": "https://mirror.example/copied-a",
            "title": "A meaningful shared business event",
            "summary": "The same business facts",
            "published_at": "",
        }])
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "production.sqlite3"
            import tools.news_topics as news_topics
            original_fetch = news_topics.fetch
            try:
                news_topics.fetch = lambda url, *_args, **_kwargs: first if "news.example" in url else mirror
                report = ingest_report(database, ["https://news.example/list", "https://mirror.example/list"])
            finally:
                news_topics.fetch = original_fetch
            self.assertEqual(report["added"], 1)
            self.assertEqual(report["duplicates"], 1)
        self.assertEqual(
            canonical_article_url("HTTPS://News.Example/a?utm_source=feed&id=7#top"),
            "https://news.example/a?id=7",
        )

    def test_ingest_report_explains_duplicates_when_nothing_is_added(self):
        items = [{
            "source_url": "https://example.test/rss",
            "article_url": "https://example.test/a",
            "title": "A meaningful article title",
            "summary": "",
            "published_at": "",
        }]
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "production.sqlite3"
            import tools.news_topics as news_topics
            news_topics.fetch = lambda *_args, **_kwargs: items
            self.assertEqual(ingest(database, ["https://example.test/rss"]), (1, []))
            report = ingest_report(database, ["https://example.test/rss"])
            self.assertEqual(report["parsed"], 1)
            self.assertEqual(report["added"], 0)
            self.assertEqual(report["duplicates"], 1)
            self.assertEqual(report["feeds"][0]["duplicates"], 1)

    def test_parse_china_news_embedded_docarr(self):
        html = b'''<script>var docArr=[{"title":"Embedded article title","content":"Summary","pubtime":"2026-09-08 10:00:00","url":"http:\\/\\/www.chinanews.com.cn\\/gn\\/2026\\/09-08\\/123.shtml"}];</script>'''
        items = parse_feed("https://channel.chinanews.com.cn/cns/cl/gn-js.shtml", html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Embedded article title")
        self.assertEqual(items[0]["article_url"], "http://www.chinanews.com.cn/gn/2026/09-08/123.shtml")

    def test_parse_next_page_accepts_only_same_site_navigation(self):
        html = b'''<html><body>
        <a href="https://other.example/index2.html">next</a>
        <a href="index2.html">next page</a>
        </body></html>'''
        self.assertEqual(
            parse_next_page("https://news.example/list/index.html", html),
            "https://news.example/list/index2.html",
        )

    def test_ingest_follows_next_page_until_new_topic_target_is_met(self):
        first = FeedItems([{
            "source_url": "https://news.example/list/index.html",
            "article_url": "https://news.example/2026/known.html",
            "title": "A known meaningful article title",
            "summary": "",
            "published_at": "",
        }], "https://news.example/list/index2.html")
        second = FeedItems([{
            "source_url": "https://news.example/list/index2.html",
            "article_url": "https://news.example/2026/new.html",
            "title": "A new meaningful article title",
            "summary": "",
            "published_at": "",
        }], "https://news.example/list/index3.html")
        third = FeedItems([{
            "source_url": "https://news.example/list/index3.html",
            "article_url": "https://news.example/2026/unused.html",
            "title": "An unused meaningful article title",
            "summary": "",
            "published_at": "",
        }])
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "production.sqlite3"
            import tools.news_topics as news_topics
            original_fetch = news_topics.fetch
            calls: list[str] = []
            pages = {
                "https://news.example/list/index.html": first,
                "https://news.example/list/index2.html": second,
                "https://news.example/list/index3.html": third,
            }
            try:
                news_topics.fetch = lambda url, *_args, **_kwargs: calls.append(url) or pages[url]
                ingest(database, ["https://news.example/list/index.html"])
                calls.clear()
                report = ingest_report(
                    database, ["https://news.example/list/index.html"], target_new=1,
                )
            finally:
                news_topics.fetch = original_fetch
            self.assertEqual(calls, [
                "https://news.example/list/index.html",
                "https://news.example/list/index2.html",
            ])
            self.assertEqual(report["added"], 1)
            self.assertEqual(report["duplicates"], 1)
            self.assertEqual(report["feeds"][0]["pages_fetched"], 2)
            self.assertEqual(report["feeds"][0]["stop_reason"], "target_reached")
            with connect(database) as connection:
                rows = connection.execute(
                    "SELECT source_url,article_url FROM news_topics ORDER BY id"
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(
                row["source_url"] == "https://news.example/list/index.html" for row in rows
            ))


if __name__ == "__main__":
    unittest.main()
