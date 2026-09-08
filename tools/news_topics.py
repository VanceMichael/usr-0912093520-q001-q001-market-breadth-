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
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402

DEFAULT_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://www.theguardian.com/world/rss",
)
ATOM = "http://www.w3.org/2005/Atom"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None:
            return clean(" ".join(child.itertext()))
    return ""


def parse_feed(source_url: str, payload: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(payload)
    entries = list(root.findall(".//item"))
    if not entries:
        entries = list(root.findall(f".//{{{ATOM}}}entry"))
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
