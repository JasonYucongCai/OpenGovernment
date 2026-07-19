#!/usr/bin/env python3
"""
sentiment_tool.py — Sentiment analysis & visualization for the news DB.

Provides functions to:
  1. Run DeepSeek sentiment analysis on pending articles
  2. Generate sentiment distribution charts (pie + stacked bar by source)
  3. Generate keyword / topic frequency charts
  4. Export results as CSV

Usage (module):
    from sentiment_tool import SentimentAnalyzer
    sa = SentimentAnalyzer(db_path, api_key)
    results = sa.analyze_batch(limit=10)
    sa.plot_sentiment_chart()

Usage (standalone):
    python sentiment_tool.py   # runs full analysis + saves plots to tools/ folder
"""

import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# SentimentAnalyzer
# ---------------------------------------------------------------------------

class SentimentAnalyzer:
    def __init__(self, db_path: str, api_key: str, model: str = "deepseek-v4-pro"):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.model = model
        self.db_path = db_path

    # ---- Database helpers ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def pending_count(self) -> int:
        conn = self._connect()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE ai_analyzed_at IS NULL"
        ).fetchone()[0]
        conn.close()
        return cnt

    def analyzed_count(self) -> int:
        conn = self._connect()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE ai_analyzed_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return cnt

    # ---- Analysis ----

    SYSTEM_PROMPT = (
        "You are a professional news analyst. Return a JSON object with an "
        "'articles' key containing an array. Each element must have exactly three "
        "fields: summary (one-sentence), sentiment (positive/negative/neutral/mixed), "
        "and topics (comma-separated list of 1-3 tags). Return ONLY valid JSON."
    )

    def analyze_batch(self, limit: int = 10, source: str = None) -> list[dict]:
        """Analyze pending articles via DeepSeek. Returns list of result dicts."""
        conn = self._connect()

        where = "WHERE ai_analyzed_at IS NULL"
        params = []
        if source:
            where += " AND source = ?"
            params.append(source)

        rows = conn.execute(
            f"SELECT id, source, title, snippet FROM articles {where} "
            f"ORDER BY first_seen_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        if not rows:
            conn.close()
            return []

        # Build prompt
        lines = []
        for i, r in enumerate(rows, 1):
            line = f"[{i}] [{r['source'].upper()}] {r['title']}"
            snippet = r["snippet"] if r["snippet"] else ""
            if snippet:
                line += f"\n    {snippet[:300]}"
            lines.append(line)

        prompt = (
            f"Analyze these {len(rows)} news articles. Return {{\"articles\": [list of objects]}} "
            f"with summary, sentiment, topics for each in order:\n\n" + "\n\n".join(lines)
        )

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3, max_tokens=2048,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        result = json.loads(content)
        if isinstance(result, dict) and "articles" in result:
            result = result["articles"]
        if not isinstance(result, list):
            result = [result]

        # Save to DB
        now = datetime.now(timezone.utc).isoformat()
        output = []
        for i, r in enumerate(rows):
            entry = result[i] if i < len(result) else {}
            summary = str(entry.get("summary", ""))[:500]
            sentiment = str(entry.get("sentiment", "neutral"))[:20]
            topics = str(entry.get("topics", ""))[:200]
            conn.execute(
                "UPDATE articles SET ai_summary=?, ai_sentiment=?, ai_topics=?, ai_analyzed_at=? WHERE id=?",
                (summary, sentiment, topics, now, r["id"]),
            )
            output.append({
                "id": r["id"], "source": r["source"], "title": r["title"],
                "summary": summary, "sentiment": sentiment, "topics": topics,
            })
        conn.commit()
        conn.close()
        return output

    # ---- Charts ----

    def get_sentiment_data(self) -> dict:
        """Return sentiment counts: overall dict + by-source dict."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT ai_sentiment, source FROM articles WHERE ai_analyzed_at IS NOT NULL"
        ).fetchall()
        conn.close()

        overall = Counter()
        by_source = {}
        for r in rows:
            s = r["ai_sentiment"] or "unknown"
            src = r["source"]
            overall[s] += 1
            by_source.setdefault(src, Counter())[s] += 1
        return {"overall": dict(overall), "by_source": {k: dict(v) for k, v in by_source.items()}}

    def get_topic_data(self) -> list[tuple]:
        """Return top N topic tags and their counts."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT ai_topics FROM articles WHERE ai_analyzed_at IS NOT NULL AND ai_topics IS NOT NULL"
        ).fetchall()
        conn.close()

        counter = Counter()
        for (topics_str,) in rows:
            for t in topics_str.split(","):
                t = t.strip().lower()
                if t:
                    counter[t] += 1
        return counter.most_common(20)

    def plot_sentiment_chart(self, save_path: str = None) -> str:
        """Generate sentiment pie + stacked bar chart. Returns path to saved PNG."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        data = self.get_sentiment_data()
        overall = data["overall"]
        by_source = data["by_source"]

        fig, axes = plt.subplots(1, 2, figsize=(14, 5))
        colors_map = {"positive": "#2a9d8f", "negative": "#e63946", "neutral": "#457b9d", "mixed": "#e9c46a", "unknown": "#999"}

        # Pie
        labels = [k.capitalize() for k in overall]
        sizes = list(overall.values())
        pie_colors = [colors_map.get(k, "#999") for k in overall]
        axes[0].pie(sizes, labels=labels, autopct="%1.1f%%", colors=pie_colors, startangle=90)
        axes[0].set_title("Overall Sentiment Distribution", fontweight="bold")

        # Stacked bar by source
        sources = sorted(by_source.keys())
        sentiments = ["positive", "negative", "neutral", "mixed"]
        bottom = None
        for sent in sentiments:
            vals = [by_source[s].get(sent, 0) for s in sources]
            axes[1].bar(sources, vals, bottom=bottom, label=sent.capitalize(),
                        color=colors_map.get(sent, "#999"))
            bottom = vals if bottom is None else [b + v for b, v in zip(bottom, vals)]

        axes[1].set_title("Sentiment by Source", fontweight="bold")
        axes[1].set_ylabel("Count")
        axes[1].legend(loc="upper right")
        plt.tight_layout()

        path = save_path or str(OUTPUT_DIR / "sentiment_chart.png")
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return path

    def plot_topic_chart(self, save_path: str = None) -> str:
        """Generate topic frequency bar chart. Returns path to saved PNG."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        topics = self.get_topic_data()
        if not topics:
            return ""

        fig, ax = plt.subplots(figsize=(10, 6))
        labels = [t for t, c in topics]
        counts = [c for t, c in topics]
        colors = plt.cm.viridis([i / len(counts) for i in range(len(counts))])
        ax.barh(labels, counts, color=colors, edgecolor="white")
        ax.invert_yaxis()
        ax.set_xlabel("Frequency")
        ax.set_title("Top AI-Assigned Topics", fontsize=14, fontweight="bold")
        for i, (t, c) in enumerate(topics):
            ax.text(c + 0.5, i, str(c), va="center", fontsize=9)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        plt.tight_layout()

        path = save_path or str(OUTPUT_DIR / "topic_chart.png")
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return path

    def export_csv(self, output_path: str = None) -> str:
        """Export all analyzed articles to CSV."""
        conn = self._connect()
        import pandas as pd
        df = pd.read_sql_query(
            "SELECT id, source, title, url, ai_summary, ai_sentiment, ai_topics, ai_analyzed_at "
            "FROM articles WHERE ai_analyzed_at IS NOT NULL ORDER BY ai_analyzed_at DESC",
            conn,
        )
        conn.close()
        path = output_path or str(OUTPUT_DIR / "sentiment_export.csv")
        df.to_csv(path, index=False)
        return path


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from dotenv import load_dotenv

    env_path = Path(__file__).parent.parent / "ai_analysis" / ".env"
    load_dotenv(env_path)
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("❌ DEEPSEEK_API_KEY not found")
        sys.exit(1)

    db = Path(__file__).parent.parent / "news_fetcher" / "news.db"
    sa = SentimentAnalyzer(str(db), api_key)

    print("=" * 50)
    print("Sentiment Analysis Tool")
    print(f"  Pending: {sa.pending_count()}   Analyzed: {sa.analyzed_count()}")
    print("=" * 50)

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print(f"\n📤 Analyzing {limit} articles…")
    results = sa.analyze_batch(limit=limit)
    print(f"✅ {len(results)} articles analyzed")

    # Charts
    chart_path = sa.plot_sentiment_chart()
    print(f"📊 Sentiment chart saved: {chart_path}")

    topic_path = sa.plot_topic_chart()
    print(f"📊 Topic chart saved: {topic_path}")

    csv_path = sa.export_csv()
    print(f"💾 CSV exported: {csv_path}")
