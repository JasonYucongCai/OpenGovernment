#!/usr/bin/env python3
"""
live_ai_analyzer.py — Continuously reads from news.db, analyzes pending
articles via DeepSeek v4, and writes AI results back. Designed to run
alongside fetch_news.py in a separate terminal — WAL mode handles concurrency.
"""

import json
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
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

DB_PATH = BASE.parent / "news_fetcher" / "news.db"
MODEL = "deepseek-v4-pro"
BATCH_SIZE = 5
POLL_INTERVAL = 30  # seconds

SYSTEM_PROMPT = (
    "You are a professional news analyst. Return a JSON object with an "
    "'articles' key containing an array. Each element must have exactly three "
    "fields: summary (one-sentence), sentiment (positive/negative/neutral/mixed), "
    "and topics (comma-separated list of 1-3 tags). Return ONLY valid JSON."
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    print("❌ DEEPSEEK_API_KEY not found in .env")
    sys.exit(1)

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

print("=" * 55)
print("🤖 Live AI Analyzer")
print(f"   Model:    {MODEL}")
print(f"   DB:       {DB_PATH}")
print(f"   Batch:    {BATCH_SIZE} articles")
print(f"   Interval: {POLL_INTERVAL}s between checks")
print(f"   Running alongside fetch_news.py (WAL mode = safe concurrency)")
print("=" * 55)

# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------
cycle = 0

while True:
    cycle += 1
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Ensure AI columns exist
    for col, typ in [
        ("ai_summary", "TEXT"), ("ai_sentiment", "TEXT"),
        ("ai_topics", "TEXT"), ("ai_analyzed_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    analyzed = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE ai_analyzed_at IS NOT NULL"
    ).fetchone()[0]
    pending = total - analyzed

    print(f"\n[{cycle}] {analyzed}/{total} analyzed, {pending} pending", end="", flush=True)

    if pending == 0:
        print(" — waiting for new articles…")
        conn.close()
        time.sleep(POLL_INTERVAL)
        continue

    # Fetch batch
    rows = conn.execute(
        "SELECT id, source, title, snippet FROM articles "
        "WHERE ai_analyzed_at IS NULL ORDER BY first_seen_at LIMIT ?",
        (BATCH_SIZE,),
    ).fetchall()

    if not rows:
        conn.close()
        time.sleep(POLL_INTERVAL)
        continue

    # Build prompt
    article_text = []
    for i, r in enumerate(rows, 1):
        line = f"[{i}] [{r['source'].upper()}] {r['title']}"
        if r["snippet"]:
            line += f"\n    {r['snippet'][:200]}"
        article_text.append(line)

    prompt = (
        f"Analyze these {len(rows)} news articles. Return {{\"articles\": [list of objects]}} "
        f"with summary, sentiment, topics for each in order:\n\n"
        + "\n\n".join(article_text)
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        result = json.loads(content)

        # Normalize
        if isinstance(result, dict) and "articles" in result:
            result = result["articles"]
        if not isinstance(result, list):
            result = [result]

        now = datetime.now(timezone.utc).isoformat()
        for i, r in enumerate(rows):
            entry = result[i] if i < len(result) else {}
            conn.execute(
                "UPDATE articles SET ai_summary=?, ai_sentiment=?, ai_topics=?, ai_analyzed_at=? WHERE id=?",
                (
                    entry.get("summary", "")[:500],
                    entry.get("sentiment", "neutral")[:20],
                    entry.get("topics", "")[:200],
                    now,
                    r["id"],
                ),
            )
        conn.commit()

        print(f" → analyzed {len(rows)}")
        for i, r in enumerate(rows[:3]):
            entry = result[i] if i < len(result) else {}
            print(
                f"   [{r['source'].upper():6s}] {entry.get('summary', '')[:70]} "
                f"[{entry.get('sentiment', '?')}] "
                f"#{entry.get('topics', 'none')}"
            )

    except Exception as exc:
        print(f" → ERROR: {exc}")

    conn.close()
    time.sleep(POLL_INTERVAL)
