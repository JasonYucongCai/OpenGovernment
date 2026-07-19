#!/usr/bin/env python3
"""
high_alert.py — Background async high-alert checker for news headlines.

Reads new (unchecked) articles from news.db and sends them to DeepSeek
to determine urgency. If a headline scores >= URGENCY_THRESHOLD, it's
flagged as high alert and logged to a JSON file + returned to the caller.

Usage (module):
    from high_alert import HighAlertChecker
    checker = HighAlertChecker(db_path, api_key)
    alerts = await checker.check_new()

Usage (standalone):
    python high_alert.py   # runs in continuous loop, logs to alerts.json
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ALERT_LOG_PATH = Path(__file__).parent / "alerts.json"
URGENCY_THRESHOLD = 7  # 1-10, 7+ triggers alert
CHECK_INTERVAL = 10     # seconds between checks when running standalone
BATCH_SIZE = 3          # headlines per DeepSeek call

SYSTEM_PROMPT = (
    "You are a high-priority news alert system. For each headline, return a JSON object with "
    "exactly two fields:\n"
    '- "urgency": an integer 1-10, where 1 = trivial/entertainment, 5 = notable, 10 = major breaking crisis\n'
    '- "reason": one sentence explaining why this urgency score was assigned\n\n'
    "Guidelines:\n"
    "- Major military conflict / war escalation: 8-10\n"
    "- Mass casualties / natural disasters: 8-10\n"
    "- Government crisis / coup / major policy change: 7-9\n"
    "- Economic crash / major market moves: 5-7\n"
    "- Celebrity / entertainment / sports: 1-3\n"
    "- Technology / business updates: 2-4\n\n"
    "Return ONLY a JSON object with an 'articles' array containing one object per headline."
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [ALERT] %(message)s")
log = logging.getLogger("high_alert")

# ---------------------------------------------------------------------------
# HighAlertChecker
# ---------------------------------------------------------------------------

class HighAlertChecker:
    def __init__(self, db_path: str, api_key: str, model: str = "deepseek-v4-pro"):
        self.db_path = db_path
        self.client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.model = model
        self._last_checked_id = 0  # track which articles we've already scanned

    async def check_new(self) -> list[dict]:
        """Check newly added articles for high urgency. Returns list of alert dicts."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Ensure alert columns exist
        for col, typ in [("alert_urgency", "TEXT"), ("alert_reason", "TEXT"), ("alert_checked_at", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        conn.commit()

        # Fetch articles not yet checked by the alert system
        rows = conn.execute(
            "SELECT id, source, title FROM articles "
            "WHERE alert_checked_at IS NULL AND id > ? "
            "ORDER BY first_seen_at DESC LIMIT ?",
            (self._last_checked_id, BATCH_SIZE),
        ).fetchall()

        if not rows:
            conn.close()
            return []

        # Build prompt
        lines = [f"[{r['source'].upper()}] {r['title']}" for r in rows]
        prompt = (
            f"Rate the urgency of these {len(rows)} news headlines (1-10 scale). "
            f"Return {{\"articles\": [list of objects]}} with urgency and reason for each:\n\n"
            + "\n\n".join(f"{i+1}. {line}" for i, line in enumerate(lines))
        )

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            result = json.loads(content)
            if isinstance(result, dict) and "articles" in result:
                result = result["articles"]
            if not isinstance(result, list):
                result = [result]
        except Exception as exc:
            log.error("DeepSeek alert API error: %s", exc)
            conn.close()
            return []

        # Process results
        now = datetime.now(timezone.utc).isoformat()
        alerts = []

        for i, row in enumerate(rows):
            entry = result[i] if i < len(result) else {}
            urgency_text = entry.get("urgency", 5)
            try:
                urgency = int(urgency_text)
            except (ValueError, TypeError):
                urgency = 5
            reason = str(entry.get("reason", ""))[:300]

            conn.execute(
                "UPDATE articles SET alert_urgency=?, alert_reason=?, alert_checked_at=? WHERE id=?",
                (str(urgency), reason, now, row["id"]),
            )

            if urgency >= URGENCY_THRESHOLD:
                alert = {
                    "id": row["id"],
                    "source": row["source"],
                    "title": row["title"],
                    "urgency": urgency,
                    "reason": reason,
                    "checked_at": now,
                }
                alerts.append(alert)
                log.warning("🚨 ALERT [%d/10] [%s] %s", urgency, row["source"].upper(), row["title"][:80])

            self._last_checked_id = max(self._last_checked_id, row["id"])

        conn.commit()
        conn.close()
        return alerts

    def save_alerts(self, alerts: list[dict]):
        """Append alerts to the alerts.json log file."""
        existing = []
        if ALERT_LOG_PATH.exists():
            try:
                existing = json.loads(ALERT_LOG_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = []
        existing.extend(alerts)
        # Keep last 500 alerts
        existing = existing[-500:]
        ALERT_LOG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        """Read recent alerts from the log file."""
        if not ALERT_LOG_PATH.exists():
            return []
        try:
            data = json.loads(ALERT_LOG_PATH.read_text(encoding="utf-8"))
            return data[-limit:]
        except (json.JSONDecodeError, FileNotFoundError):
            return []


# ---------------------------------------------------------------------------
# Standalone continuous loop
# ---------------------------------------------------------------------------

async def standalone_loop(db_path: str):
    """Run the alert checker continuously, logging alerts to alerts.json."""
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / "ai_analysis" / ".env"
    load_dotenv(env_path)
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("❌ DEEPSEEK_API_KEY not found")
        return

    checker = HighAlertChecker(db_path, api_key)
    print(f"🚨 High Alert Checker started — threshold {URGENCY_THRESHOLD}/10, checking every {CHECK_INTERVAL}s")
    print(f"   DB: {db_path}")
    print(f"   Log: {ALERT_LOG_PATH}")

    while True:
        try:
            alerts = await checker.check_new()
            if alerts:
                checker.save_alerts(alerts)
                for a in alerts:
                    print(f"  🚨 [{a['urgency']}/10] [{a['source'].upper()}] {a['title'][:80]}")
                    print(f"      Reason: {a['reason']}")
            else:
                # Silently continue
                pass
        except Exception as exc:
            log.error("Check loop error: %s", exc)

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    DB = Path(__file__).parent.parent / "news_fetcher" / "news.db"
    asyncio.run(standalone_loop(str(DB)))
