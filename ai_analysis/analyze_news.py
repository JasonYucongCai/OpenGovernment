#!/usr/bin/env python3
"""
ai_analysis/analyze_news.py — DeepSeek-powered news analysis.

Reads headlines from ../news_fetcher/news.db, sends batches to DeepSeek API
(openai-compatible), and stores AI summaries, sentiment, and topics back to the DB.

Usage:
  python analyze_news.py                  # analyze all articles without AI content
  python analyze_news.py --recent 20       # analyze 20 most recent
  python analyze_news.py --source npr      # only NPR
  python analyze_news.py --full            # include full body text in prompt
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR.parent / "news_fetcher" / "news.db"
LOG_PATH = BASE_DIR / "analyze_news.log"

load_dotenv(ENV_PATH)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    print("❌ DEEPSEEK_API_KEY not found in .env")
    sys.exit(1)

# DeepSeek v4 — uses openai-compatible API at api.deepseek.com
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

MODEL = "deepseek-v4-pro"  # or deepseek-v4-flash for faster/cheaper
BATCH_SIZE = 10
RATE_LIMIT_SLEEP = 1.0  # seconds between API calls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("analyze_news")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    # Add AI columns if they don't exist
    for col, typ in [
        ("ai_summary", "TEXT"),
        ("ai_sentiment", "TEXT"),
        ("ai_topics", "TEXT"),
        ("ai_analyzed_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn

def get_articles(conn, source=None, limit=None, use_body=False) -> list[dict]:
    """Get articles that haven't been AI-analyzed yet."""
    where = "WHERE (ai_analyzed_at IS NULL)"
    params = []
    if source:
        where += " AND source = ?"
        params.append(source)
    query = f"SELECT id, source, title, url, snippet, content FROM articles {where} ORDER BY first_seen_at DESC"
    if limit:
        query += f" LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()

    articles = []
    for r in rows:
        text = r["title"]
        if use_body and r["content"] and len(r["content"]) > 100:
            text += "\n\n" + r["content"][:3000]  # cap at 3000 chars
        elif r["snippet"]:
            text += "\n\n" + r["snippet"]
        articles.append({"id": r["id"], "source": r["source"], "title": r["title"], "url": r["url"], "text": text})
    return articles

def save_ai_result(conn, article_id: int, summary: str, sentiment: str, topics: str):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE articles SET ai_summary=?, ai_sentiment=?, ai_topics=?, ai_analyzed_at=? WHERE id=?",
        (summary, sentiment, topics, now, article_id),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# AI Analysis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a professional news analyst. For each news article, return a JSON object with exactly these three fields:

- "summary": A one-sentence summary of the article in plain English.
- "sentiment": One of: "positive", "negative", "neutral", or "mixed".
- "topics": A comma-separated list of 1-3 relevant topic tags (e.g. "politics, economy, technology").

Return ONLY valid JSON, nothing else."""

def analyze_batch(articles: list[dict]) -> list[dict]:
    """Send a batch of articles to DeepSeek for analysis."""
    # Build a numbered list of articles
    article_list = ""
    for i, a in enumerate(articles, 1):
        article_list += f"[{i}] [{a['source'].upper()}] {a['title']}\n"
        if len(a.get("text", "")) > len(a["title"]):
            snippet = a["text"][len(a["title"]):].strip()[:500]
            if snippet:
                article_list += f"    {snippet}\n"

    user_msg = f"Analyze each of the following {len(articles)} news articles. Return a JSON array with one object per article, in the same order:\n\n{article_list}"

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content

        # Parse JSON response
        import json
        result = json.loads(content)

        # DeepSeek may return {"articles": [...]} or a direct array
        if isinstance(result, dict) and "articles" in result:
            result = result["articles"]
        if not isinstance(result, list):
            result = [result]

        # Pad if shorter
        while len(result) < len(articles):
            result.append({"summary": "", "sentiment": "neutral", "topics": ""})

        # Merge back into article dicts
        for i, a in enumerate(articles):
            if i < len(result):
                a["ai_summary"] = result[i].get("summary", "")[:500]
                a["ai_sentiment"] = result[i].get("sentiment", "neutral")[:20]
                a["ai_topics"] = result[i].get("topics", "")[:200]

        return articles

    except Exception as exc:
        log.error("DeepSeek API error: %s", exc)
        return articles

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DeepSeek-powered news analysis")
    parser.add_argument("--recent", type=int, default=None, help="Analyze N most recent articles")
    parser.add_argument("--source", choices=["npr","bbc","cnn"], help="Only analyze one source")
    parser.add_argument("--full", action="store_true", help="Include full article body text")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be analyzed without calling API")
    args = parser.parse_args()

    conn = init_db()
    conn.row_factory = sqlite3.Row

    articles = get_articles(conn, source=args.source, limit=args.recent, use_body=args.full)
    total_pending = conn.execute("SELECT COUNT(*) FROM articles WHERE ai_analyzed_at IS NULL").fetchone()[0]

    log.info("=" * 55)
    log.info("📰 %d articles pending AI analysis (total DB)", total_pending)
    log.info("📋 %d selected for this run", len(articles))
    log.info("🤖 Model: %s", MODEL)
    log.info("📊 Batch size: %d", BATCH_SIZE)
    log.info("=" * 55)

    if args.dry_run:
        log.info("DRY RUN — showing first 5 articles:")
        for a in articles[:5]:
            log.info("  [%s] %s", a["source"].upper(), a["title"][:80])
        conn.close()
        return

    if not articles:
        log.info("✅ All articles already analyzed. Nothing to do.")
        conn.close()
        return

    # Process in batches
    analyzed = 0
    total = len(articles)
    for i in range(0, total, BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        log.info("--- Batch %d-%d / %d ---", i + 1, min(i + BATCH_SIZE, total), total)
        batch = analyze_batch(batch)

        for a in batch:
            summary = a.get("ai_summary", "")
            sentiment = a.get("ai_sentiment", "neutral")
            topics = a.get("ai_topics", "")
            save_ai_result(conn, a["id"], summary, sentiment, topics)
            analyzed += 1
            log.debug("  ✓ %s: %s [%s]", a["source"].upper(), summary[:60], sentiment)

        log.info("  → %d analyzed, %d total\n", analyzed, total)
        time.sleep(RATE_LIMIT_SLEEP)

    # Stats
    total_done = conn.execute("SELECT COUNT(*) FROM articles WHERE ai_analyzed_at IS NOT NULL").fetchone()[0]
    by_sent = conn.execute(
        "SELECT ai_sentiment, COUNT(*) FROM articles GROUP BY ai_sentiment ORDER BY COUNT(*) DESC"
    ).fetchall()
    log.info("=" * 55)
    log.info("✅ Analysis complete: %d/%d articles analyzed", total_done, total_pending + total_done)
    log.info("Sentiment breakdown:")
    for s, c in by_sent:
        log.info("  %-10s: %d", s, c)
    log.info("=" * 55)

    conn.close()

if __name__ == "__main__":
    main()
