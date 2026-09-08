#!/usr/bin/env python3
"""Fetch and persist deduplicated news topics for autonomous authoring."""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sqlite3
import sys
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402

DEFAULT_FEEDS = (
    "https://channel.chinanews.com.cn/cns/cl/gn-js.shtml",
    "https://channel.chinanews.com.cn/cns/cl/gn-kjww.shtml",
    "https://www.chinanews.com/finance/",
)
ATOM = "http://www.w3.org/2005/Atom"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    # Some China News list pages prefix headlines with a navigation marker.
    return re.sub(r"^(?:[-\u2013\u2014]\s*)+", "", text).strip()


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None:
            return clean(" ".join(child.itertext()))
    return ""


def parse_feed(source_url: str, payload: bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return parse_html(source_url, payload)
    entries = list(root.findall(".//item"))
    if not entries:
        entries = list(root.findall(f".//{{{ATOM}}}entry"))
    if not entries and root.tag.lower() in {"html", "body"}:
        return parse_html(source_url, payload)
    parsed: list[dict[str, str]] = []
    for entry in entries:
        title = child_text(entry, ("title", f"{{{ATOM}}}title"))
        link = child_text(entry, ("link", f"{{{ATOM}}}link"))
        if not link:
            link_node = entry.find(f"{{{ATOM}}}link")
            link = str(link_node.get("href", "")) if link_node is not None else ""
        summary = child_text(entry, ("description", "summary", f"{{{ATOM}}}summary", f"{{{ATOM}}}content"))
        published = child_text(entry, ("pubDate", "published", "updated", f"{{{ATOM}}}published", f"{{{ATOM}}}updated"))
        if title and link:
            parsed.append({"source_url": source_url, "article_url": link, "title": title, "summary": summary, "published_at": published})
    return parsed


class _ChannelParser(HTMLParser):
    """Extract likely article links from a news channel page without scraping body text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_href = ""
        self.current_text: list[str] = []
        self.in_heading = False
        self.items: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.current_href = str(attributes["href"])
            self.current_text = []
        if tag in {"h1", "h2", "h3", "h4"}:
            self.in_heading = True

    def handle_data(self, data: str) -> None:
        if self.current_href or self.in_heading:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_href:
            text = clean(" ".join(self.current_text))
            if text:
                self.items.append((self.current_href, text))
            self.current_href = ""
            self.current_text = []
        if tag in {"h1", "h2", "h3", "h4"}:
            self.in_heading = False


def parse_html(source_url: str, payload: bytes) -> list[dict[str, str]]:
    parser = _ChannelParser()
    charset = "utf-8"
    head = payload[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.IGNORECASE)
    if match:
        charset = match.group(1)
    text = payload.decode(charset, errors="replace")
    parser.feed(text)
    source_host = urlparse(source_url).netloc
    seen: set[str] = set()
    parsed: list[dict[str, str]] = []
    for raw_link, title in parser.items:
        article_url = urljoin(source_url, raw_link).split("#", 1)[0]
        parsed_url = urlparse(article_url)
        if parsed_url.scheme not in {"http", "https"} or parsed_url.netloc != source_host:
            continue
        path = parsed_url.path.lower()
        blocked_titles = {"about us", "home", "首页", "联系我们", "登录", "注册", "更多", "下一页", "上一页"}
        article_hint = any(token in path for token in ("/202", ".shtml", "/article", "/content", "/finance/"))
        if article_url == source_url or title.casefold() in blocked_titles or len(title) < 10 or len(title) > 180 or not article_hint:
            continue
        if article_url in seen:
            continue
        seen.add(article_url)
        parsed.append({
            "source_url": source_url,
            "article_url": article_url,
            "title": title,
            "summary": "",
            "published_at": "",
        })
    return parsed[:100]


def fetch(source_url: str, timeout: int = 20) -> list[dict[str, str]]:
    request = urllib.request.Request(source_url, headers={"User-Agent": "CCUSR-NewsTopics/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_feed(source_url, response.read())


def topic_hash(item: dict[str, str]) -> str:
    value = re.sub(r"\s+", " ", f"{item['title']} {item['article_url']}".lower()).strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ingest(database: Path, feeds: list[str], timeout: int = 20) -> tuple[int, list[str]]:
    added = 0
    errors: list[str] = []
    with connect(database.resolve()) as connection:
        for source_url in feeds:
            try:
                items = fetch(source_url, timeout)
            except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the cycle
                errors.append(f"{source_url}: {exc}")
                continue
            for item in items:
                timestamp = now()
                result = connection.execute(
                    "INSERT OR IGNORE INTO news_topics(source_url,article_url,title,summary,published_at,topic_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (item["source_url"], item["article_url"], item["title"], item["summary"], item["published_at"], topic_hash(item), timestamp, timestamp),
                )
                added += int(result.rowcount == 1)
            # A feed that was successfully parsed but only contained topics
            # already present in SQLite is healthy; do not report it as an
            # outage on subsequent daemon cycles.  Warn only when parsing
            # yielded no article-like entries at all.
            if not items:
                errors.append(f"{source_url}: no article-like topics found")
        connection.commit()
    return added, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--feeds", default=os.environ.get("NEWS_FEEDS", ",".join(DEFAULT_FEEDS)))
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    feeds = [value.strip() for value in args.feeds.split(",") if value.strip()]
    if args.list:
        with connect(args.db.resolve()) as connection:
            for row in connection.execute("SELECT id,title,source_url,status,used_batch FROM news_topics ORDER BY id DESC LIMIT 100"):
                print(f"{row['id']}\t{row['status']}\t{row['title']}\t{row['source_url']}")
        return 0
    added, errors = ingest(args.db, feeds, args.timeout)
    print(f"Fetched {len(feeds)} feeds; added {added} new topics.")
    for error in errors:
        print(f"Feed warning: {error}", file=sys.stderr)
    return 0 if not errors or added else 1


if __name__ == "__main__":
    raise SystemExit(main())
