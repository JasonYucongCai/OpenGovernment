#!/usr/bin/env python3
"""
news_fetcher — Poll NPR, BBC RSS, and CNN every 60 seconds and store to SQLite.

Sources:
  - NPR Text-Only : https://text.npr.org
  - BBC RSS        : https://feeds.bbci.co.uk/news/rss.xml
  - CNN Lite       : https://lite.cnn.com

Usage:
  python fetch_news.py              # poll forever
  python fetch_news.py --once       # fetch once and exit
  python fetch_news.py --interval 30   # poll every 30 seconds
"""

import argparse
import logging
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "news.db"
LOG_PATH = Path(__file__).parent / "fetch_news.log"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

TIMEOUT = 20  # seconds per HTTP request

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("news_fetcher")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Create / open the SQLite database and return a connection."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source        TEXT    NOT NULL,   -- 'npr', 'bbc', 'cnn'
            title         TEXT    NOT NULL,
            url           TEXT    NOT NULL UNIQUE,
            first_seen_at TEXT    NOT NULL,   -- when headline was FIRST captured (never changes)
            last_seen_at  TEXT    NOT NULL,   -- most recent time we saw this headline (updated each cycle)
            published_at  TEXT,               -- original publication date from publisher (if available)
            snippet       TEXT,               -- short description (BBC RSS only)
            content       TEXT,               -- full article body text (fetched ONCE, see has_body)
            has_body      INTEGER DEFAULT 0   -- 1 = body text has been fetched, 0 = not yet
        )
        """
    )
    # Migrate columns from older schema versions
    for col, col_type in [
        ("content", "TEXT"),
        ("has_body", "INTEGER DEFAULT 0"),
        ("first_seen_at", "TEXT"),
        ("last_seen_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # If upgrading from a version without first_seen_at, backfill it
    try:
        conn.execute(
            "UPDATE articles SET first_seen_at = fetched_at WHERE first_seen_at IS NULL"
        )
        conn.execute(
            "UPDATE articles SET last_seen_at = fetched_at WHERE last_seen_at IS NULL"
        )
        conn.execute(
            "UPDATE articles SET has_body = 1 WHERE content IS NOT NULL AND length(content) > 100"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen ON articles(first_seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_has_body ON articles(has_body)")
    conn.commit()
    return conn


def insert_articles(conn: sqlite3.Connection, articles: list[dict]) -> int:
    """Insert new articles. For existing URLs, update last_seen_at and title.
    Returns count of NEW articles inserted."""
    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    for a in articles:
        try:
            # Try INSERT — if URL is new, creates row with first_seen_at = now
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO articles
                    (source, title, url, first_seen_at, last_seen_at, published_at, snippet, content, has_body)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    a["source"], a["title"], a["url"],
                    now, now,
                    a.get("published_at"), a.get("snippet", ""), a.get("content", ""),
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
            else:
                # URL already exists — update last_seen_at and title
                conn.execute(
                    "UPDATE articles SET last_seen_at = ?, title = ? WHERE url = ?",
                    (now, a["title"], a["url"]),
                )
        except sqlite3.Error as exc:
            log.warning("DB insert error [%s]: %s", a.get("url", "?"), exc)
    conn.commit()
    return inserted

def enrich_bodies(conn: sqlite3.Connection):
    """Fetch article body text for recent articles that don't have it yet.
    Sets has_body = 1 on success."""
    for source in ["npr", "bbc", "cnn"]:
        rows = conn.execute(
            "SELECT id, url FROM articles WHERE source = ? AND has_body = 0 "
            "ORDER BY first_seen_at DESC LIMIT ?",
            (source, BODY_COUNT),
        ).fetchall()
        for row in rows:
            body = _BODY_EXTRACTORS[source](row["url"])
            if len(body) > 100:
                conn.execute(
                    "UPDATE articles SET content = ?, has_body = 1 WHERE id = ?",
                    (body, row["id"]),
                )
                conn.commit()
            time.sleep(0.25)

# ---------------------------------------------------------------------------
# Fetchers — one per source (headlines only, fast)
# ---------------------------------------------------------------------------

BODY_TIMEOUT = 8   # short timeout for article body fetches (prevents hanging)
BODY_COUNT = 3     # how many articles per source to enrich with body text per cycle


def _extract_npr_body(article_url: str) -> str:
    if not article_url.startswith("http"):
        return ""
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=BODY_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for sep in soup.select("hr, [role=separator]"):
            sep.decompose()
        article = soup.find("article")
        if not article:
            return ""
        paragraphs = []
        for p in article.find_all("p"):
            text = p.get_text(" ", strip=True)
            if text and len(text) > 40:
                paragraphs.append(text)
        return "\n\n".join(paragraphs)
    except Exception:
        return ""


def _extract_bbc_body(article_url: str) -> str:
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=BODY_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = []
        for block in soup.select("[data-component='text-block']"):
            text = block.get_text(" ", strip=True)
            if text and len(text) > 40:
                paragraphs.append(text)
        if not paragraphs:
            article = soup.find("article")
            if article:
                for p in article.find_all("p"):
                    text = p.get_text(" ", strip=True)
                    if text and len(text) > 40:
                        paragraphs.append(text)
        return "\n\n".join(paragraphs[:20])
    except Exception:
        return ""


def _extract_cnn_body(article_url: str) -> str:
    try:
        resp = requests.get(article_url, headers=HEADERS, timeout=BODY_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            if not text or len(text) <= 40:
                continue
            if text.lower().startswith(("source:", "updated:", "by ")):
                continue
            paragraphs.append(text)
        return "\n\n".join(paragraphs)
    except Exception:
        return ""


_BODY_EXTRACTORS = {"npr": _extract_npr_body, "bbc": _extract_bbc_body, "cnn": _extract_cnn_body}


def _enrich_bodies(conn: sqlite3.Connection):
    """Fetch article body text for recent articles that don't have it yet."""
    for source in ["npr", "bbc", "cnn"]:
        rows = conn.execute(
            "SELECT id, url FROM articles WHERE source = ? "
            "AND (content IS NULL OR length(content) < 100) "
            "ORDER BY fetched_at DESC LIMIT ?",
            (source, BODY_COUNT),
        ).fetchall()
        for row in rows:
            body = _BODY_EXTRACTORS[source](row["url"])
            if len(body) > 100:
                conn.execute("UPDATE articles SET content = ? WHERE id = ?", (body, row["id"]))
                conn.commit()
            time.sleep(0.25)


def fetch_npr() -> list[dict]:
    """Scrape headlines from text.npr.org."""
    articles = []
    try:
        resp = requests.get("https://text.npr.org", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            url = href
            if href.startswith("/"):
                url = "https://text.npr.org" + href
            elif not href.startswith("http"):
                continue
            if text and len(text) > 15 and len(text) < 300:
                articles.append({
                    "source": "npr", "title": text, "url": url,
                    "snippet": "", "content": "",
                })
    except Exception as exc:
        log.error("NPR fetch failed: %s", exc)
    return articles


def fetch_bbc_rss() -> list[dict]:
    """Parse the BBC News RSS feed."""
    articles = []
    try:
        resp = requests.get("https://feeds.bbci.co.uk/news/rss.xml", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            desc_el = item.find("description")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            url = link_el.text.strip() if link_el is not None and link_el.text else ""
            published = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
            snippet = desc_el.text.strip()[:300] if desc_el is not None and desc_el.text else ""
            if title and url:
                articles.append({
                    "source": "bbc", "title": title, "url": url,
                    "published_at": published, "snippet": snippet, "content": "",
                })
    except ET.ParseError as exc:
        log.error("BBC RSS parse error: %s", exc)
    except Exception as exc:
        log.error("BBC RSS fetch failed: %s", exc)
    return articles


def fetch_cnn() -> list[dict]:
    """Scrape headlines from lite.cnn.com."""
    articles = []
    try:
        resp = requests.get("https://lite.cnn.com", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            url = href
            if href.startswith("/"):
                url = "https://lite.cnn.com" + href
            elif not href.startswith("http"):
                continue
            if text and len(text) > 15 and len(text) < 300:
                articles.append({
                    "source": "cnn", "title": text, "url": url,
                    "snippet": "", "content": "",
                })
    except Exception as exc:
        log.error("CNN fetch failed: %s", exc)
    return articles


FETCHERS = {
    "npr": fetch_npr,
    "bbc": fetch_bbc_rss,
    "cnn": fetch_cnn,
}

# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

_shutdown = False

def _handle_shutdown(_signum, _frame):
    global _shutdown
    log.info("Shutdown signal received — finishing current cycle…")
    _shutdown = True


def poll_once(conn: sqlite3.Connection) -> dict[str, int]:
    """Fetch all three sources (headlines only) in parallel, store results, then enrich with body text.
    Returns per-source new-article counts."""
    counts = {}

    # Phase 1: Fetch headlines (fast, parallel)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fn): name for name, fn in FETCHERS.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                articles = future.result()
                n = insert_articles(conn, articles)
                counts[name] = n
                log.info("  %-4s → %3d articles fetched, %3d new", name.upper(), len(articles), n)
            except Exception as exc:
                log.error("  %-4s → FAILED: %s", name.upper(), exc)
                counts[name] = -1

    # Phase 2: Enrich bodies for articles that haven't been fetched yet
    try:
        enrich_bodies(conn)
    except Exception as exc:
        log.debug("  body enrich skipped: %s", exc)

    return counts


def poll_loop(conn: sqlite3.Connection, interval: int):
    """Run poll_once in a loop every `interval` seconds."""
    log.info("=" * 55)
    log.info("News Fetcher started — polling every %d seconds", interval)
    log.info("Sources: NPR (text.npr.org) | BBC (RSS) | CNN (lite.cnn.com)")
    log.info("Database: %s", DB_PATH.resolve())
    log.info("Strategy: headlines every cycle | body text once (has_body flag)")
    log.info("Press Ctrl+C to stop")
    log.info("=" * 55)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    cycle = 0
    while not _shutdown:
        cycle += 1
        log.info("Cycle #%d — %s UTC", cycle, datetime.now(timezone.utc).strftime("%H:%M:%S"))
        poll_once(conn)

        if _shutdown:
            break

        # Print DB stats every 10 cycles
        if cycle % 10 == 0:
            total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            by_source = conn.execute(
                "SELECT source, COUNT(*) FROM articles GROUP BY source"
            ).fetchall()
            with_body = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE has_body = 1"
            ).fetchone()[0]
            breakdown = " | ".join(f"{s.upper()}: {c}" for s, c in by_source)
            log.info("  📊 DB: %d total | %d with body | (%s)", total, with_body, breakdown)

        for remaining in range(interval, 0, -1):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Shutting down. Total cycles: %d", cycle)
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    log.info("Final article count: %d", total)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Poll NPR, BBC RSS, CNN and store to SQLite")
    parser.add_argument(
        "--once", action="store_true",
        help="Fetch once and exit (no polling loop)",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling interval in seconds (default: 60)",
    )
    args = parser.parse_args()

    conn = init_db()

    if args.once:
        log.info("Single-shot fetch…")
        poll_once(conn)
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        log.info("Total articles in DB: %d", total)
    else:
        poll_loop(conn, args.interval)

    conn.close()


if __name__ == "__main__":
    main()
