#!/usr/bin/env python3
"""
news_monitor_ui.py — Tabbed monitoring UI (tkinter).

Tabs:
  📰 News Monitoring   — Fetch control, High Alert, Sentiment Analysis
  🚦 City Traffic      — Placeholder
  ⚡ Electricity Net   — Placeholder

Dependencies: tkinter (built-in), PIL (optional, for images), dotenv, openai
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[1]
FETCH_SCRIPT = BASE_DIR / "news_fetcher" / "fetch_news.py"
DB_PATH = BASE_DIR / "news_fetcher" / "news.db"
ENV_PATH = BASE_DIR / "ai_analysis" / ".env"
TOOLS_DIR = BASE_DIR / "tools"

# Load API key
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
    DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
except ImportError:
    DEEPSEEK_KEY = ""

# ---------------------------------------------------------------------------
# Background fetch worker (runs fetch_news.py as subprocess)
# ---------------------------------------------------------------------------

class FetchWorker:
    """Manages the fetch_news.py subprocess."""
    def __init__(self, log_queue: queue.Queue):
        self.process = None
        self.log_queue = log_queue
        self.running = False
        self.interval = 30

    def start(self, interval: int = 30):
        if self.running:
            return
        self.interval = interval
        self.running = True
        cmd = [
            sys.executable, str(FETCH_SCRIPT),
            "--interval", str(interval),
        ]
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(BASE_DIR / "news_fetcher"),
        )
        self.log_queue.put(("log", f"🚀 News fetcher started (interval: {interval}s)\n"))
        threading.Thread(target=self._read_output, daemon=True).start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        self.log_queue.put(("log", "⏹ Fetcher stopped.\n"))

    def _read_output(self):
        for line in self.process.stdout:
            if not self.running:
                break
            if "INFO" in line or "ERROR" in line or "new" in line.lower():
                self.log_queue.put(("log", line))

    def is_running(self) -> bool:
        return self.running and self.process is not None and self.process.poll() is None


# ---------------------------------------------------------------------------
# High Alert background checker (async, runs in a thread)
# ---------------------------------------------------------------------------

class AlertWorker:
    """Runs the high alert checker in a background thread."""
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue
        self.running = False
        self.thread = None
        self.interval = 10

    def start(self, interval: int = 10):
        if self.running:
            return
        self.running = True
        self.interval = interval
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.log_queue.put(("log", f"🚨 High Alert checker started (interval: {interval}s)\n"))

    def stop(self):
        self.running = False
        self.log_queue.put(("log", "⏹ High Alert checker stopped.\n"))

    def _run(self):
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def check_loop():
            from high_alert import HighAlertChecker, URGENCY_THRESHOLD
            checker = HighAlertChecker(str(DB_PATH), DEEPSEEK_KEY)
            self.log_queue.put(("log", f"   Threshold: {URGENCY_THRESHOLD}/10\n"))

            while self.running:
                try:
                    alerts = await checker.check_new()
                    if alerts:
                        checker.save_alerts(alerts)
                        for a in alerts[:10]:  # max 10 shown
                            # Send to alert panel
                            alert_line = f"[{a['urgency']}/10] [{a['source'].upper()}] {a['title'][:90]}"
                            self.log_queue.put(("alert", alert_line))
                            # Also log full detail
                            self.log_queue.put(("log",
                                f"🚨 [{a['urgency']}/10] [{a['source'].upper()}] {a['title'][:100]}\n"
                                f"   → {a['reason'][:150]}\n"
                            ))
                except Exception as exc:
                    self.log_queue.put(("log", f"⚠ Alert error: {exc}\n"))
                await asyncio.sleep(self.interval)

        loop.run_until_complete(check_loop())


# ---------------------------------------------------------------------------
# Sentiment analysis worker
# ---------------------------------------------------------------------------

class SentimentWorker:
    """Runs sentiment analysis on demand."""
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue
        self.running = False

    def run(self, limit: int = 20):
        if self.running:
            self.log_queue.put(("log", "⚠ Analysis already in progress.\n"))
            return
        self.running = True
        threading.Thread(target=self._run, args=(limit,), daemon=True).start()

    def _run(self, limit):
        try:
            from sentiment_tool import SentimentAnalyzer
            sa = SentimentAnalyzer(str(DB_PATH), DEEPSEEK_KEY)
            pending = sa.pending_count()
            analyzed = sa.analyzed_count()
            self.log_queue.put(("log", f"\n📊 Starting sentiment analysis…\n"))
            self.log_queue.put(("log", f"   Previously analyzed: {analyzed} | Pending: {pending}\n"))
            self.log_queue.put(("log", f"   This batch: {min(limit, pending)} articles\n\n"))

            actual = min(limit, pending)
            if actual == 0:
                self.log_queue.put(("log", "✅ All articles already analyzed!\n"))
                self.running = False
                return

            results = sa.analyze_batch(limit=actual)
            self.log_queue.put(("log", f"✅ {len(results)} articles analyzed.\n"))

            # Build sentiment result summary for the result panel
            sentiments = {}
            topics_all = []
            for r in results:
                s = r.get("sentiment", "unknown")
                sentiments[s] = sentiments.get(s, 0) + 1
                if r.get("topics"):
                    topics_all.extend(t.strip() for t in r["topics"].split(","))

            # Push structured sentiment result to the result panel
            lines = [f"✅ Analyzed {len(results)} articles — sentiment breakdown:"]
            for s, c in sorted(sentiments.items(), key=lambda x: -x[1]):
                bar = "█" * min(c, 20)
                lines.append(f"   {s:10s}  {bar}  ({c})")
            if topics_all:
                from collections import Counter
                top5 = Counter(topics_all).most_common(5)
                lines.append(f"\n   Top topics: {', '.join(t for t, _ in top5)}")
            self.log_queue.put(("sentiment", "\n".join(lines)))

            # Log breakdown
            for s, c in sorted(sentiments.items(), key=lambda x: -x[1]):
                self.log_queue.put(("log", f"   {s:10s}: {'█' * min(c, 30)} ({c})\n"))

            # Generate charts if matplotlib is available
            try:
                chart_path = sa.plot_sentiment_chart(str(TOOLS_DIR / "sentiment_chart.png"))
                topic_path = sa.plot_topic_chart(str(TOOLS_DIR / "topic_chart.png"))
                self.log_queue.put(("log", f"\n📊 Charts saved:\n"))
                self.log_queue.put(("log", f"   {chart_path}\n"))
                self.log_queue.put(("log", f"   {topic_path}\n"))
                self.log_queue.put(("sentiment",
                    f"\n📈 Charts ready:\n   Sentiment: {chart_path}\n   Topics:    {topic_path}"))
            except Exception as exc:
                self.log_queue.put(("log", f"⚠ Chart generation failed: {exc}\n"))

        except Exception as exc:
            self.log_queue.put(("log", f"❌ Sentiment analysis failed: {exc}\n"))
        finally:
            self.running = False


# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------

class NewsMonitorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("OpenGovernment — Monitoring Dashboard")
        self.root.geometry("1000x700")
        self.root.minsize(800, 500)

        # Queue for thread-safe log updates
        self.log_queue = queue.Queue()
        self.root.after(100, self._poll_log_queue)

        # Workers
        self.fetch_worker = FetchWorker(self.log_queue)
        self.alert_worker = AlertWorker(self.log_queue)
        self.sentiment_worker = SentimentWorker(self.log_queue)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # --- Style ---
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background="#1e1e2e")
        style.configure("TNotebook.Tab", padding=[16, 6], font=("Segoe UI", 10))
        style.configure("TFrame", background="#1e1e2e")
        style.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=[10, 4])
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=[12, 6])
        style.configure("Red.TButton", font=("Segoe UI", 10, "bold"), padding=[12, 6],
                        background="#e63946")
        style.configure("Green.TButton", font=("Segoe UI", 10, "bold"), padding=[12, 6])

        # --- Notebook (tabs) ---
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=0, pady=0)

        # Tab 1: News Monitoring
        news_frame = ttk.Frame(notebook)
        notebook.add(news_frame, text="  📰 News Monitoring  ")
        self._build_news_tab(news_frame)

        # Tab 2: City Traffic (placeholder)
        traffic_frame = ttk.Frame(notebook)
        notebook.add(traffic_frame, text="  🚦 City Traffic  ")
        self._build_placeholder_tab(traffic_frame, "City Traffic Monitoring",
                                     "Real-time traffic congestion data, route analysis, and incident alerts.\n\nComing soon.")

        # Tab 3: Electricity Network (placeholder)
        power_frame = ttk.Frame(notebook)
        notebook.add(power_frame, text="  ⚡ Electricity Network  ")
        self._build_placeholder_tab(power_frame, "Electricity Network Monitoring",
                                     "Grid load monitoring, outage detection, renewable generation tracking.\n\nComing soon.")

    def _build_news_tab(self, parent):
        # ---- Row 0: Header ----
        header = ttk.Label(parent, text="📰 News Monitoring Dashboard",
                           font=("Segoe UI", 14, "bold"), foreground="#f5c2e7")
        header.pack(pady=(12, 4))

        # ---- Row 1: Fetch Control Panel ----
        fetch_panel = ttk.Frame(parent)
        fetch_panel.pack(fill="x", padx=12, pady=(4, 0))

        ttk.Label(fetch_panel, text="Poll Interval (seconds):").pack(side="left", padx=(0, 4))
        self.interval_var = tk.StringVar(value="30")
        interval_entry = ttk.Entry(fetch_panel, textvariable=self.interval_var, width=6)
        interval_entry.pack(side="left", padx=(0, 8))

        self.fetch_btn = ttk.Button(fetch_panel, text="▶ Start Fetch",
                                     command=self._toggle_fetch, style="Accent.TButton")
        self.fetch_btn.pack(side="left", padx=(0, 8))

        self.fetch_status = ttk.Label(fetch_panel, text="⏹ Stopped", foreground="#f38ba8")
        self.fetch_status.pack(side="left", padx=(0, 16))

        # Separator
        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # ---- Row 2: High Alert Panel ----
        alert_panel = ttk.Frame(parent)
        alert_panel.pack(fill="x", padx=12, pady=(0, 4))

        ttk.Label(alert_panel, text="High Alert — background urgency checker",
                  font=("Segoe UI", 11, "bold"), foreground="#fab387").pack(anchor="w")

        alert_btn_row = ttk.Frame(parent)
        alert_btn_row.pack(fill="x", padx=12, pady=4)

        ttk.Label(alert_btn_row, text="Check Interval (s):").pack(side="left", padx=(0, 4))
        self.alert_interval_var = tk.StringVar(value="10")
        ttk.Entry(alert_btn_row, textvariable=self.alert_interval_var, width=6).pack(side="left", padx=(0, 8))

        self.alert_btn = ttk.Button(alert_btn_row, text="▶ Start High Alert",
                                     command=self._toggle_alert, style="Accent.TButton")
        self.alert_btn.pack(side="left", padx=(0, 8))

        self.alert_status = ttk.Label(alert_btn_row, text="⏹ Stopped", foreground="#f38ba8")
        self.alert_status.pack(side="left")

        # Alert threshold info
        ttk.Label(alert_btn_row, text="   (alerts when urgency ≥ 7/10)", foreground="#6c7086").pack(
            side="left", padx=(12, 0))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # ---- Row 3: Sentiment Analysis Panel ----
        sentiment_panel = ttk.Frame(parent)
        sentiment_panel.pack(fill="x", padx=12, pady=(0, 4))

        ttk.Label(sentiment_panel, text="Sentiment Analysis — AI-powered topic & sentiment extraction",
                  font=("Segoe UI", 11, "bold"), foreground="#a6e3a1").pack(anchor="w")

        sent_btn_row = ttk.Frame(parent)
        sent_btn_row.pack(fill="x", padx=12, pady=4)

        ttk.Label(sent_btn_row, text="Articles to analyze:").pack(side="left", padx=(0, 4))
        self.sentiment_count_var = tk.StringVar(value="10")
        ttk.Entry(sent_btn_row, textvariable=self.sentiment_count_var, width=6).pack(side="left", padx=(0, 8))

        self.sentiment_btn = ttk.Button(sent_btn_row, text="▶ Run Analysis",
                                         command=self._run_sentiment, style="Accent.TButton")
        self.sentiment_btn.pack(side="left", padx=(0, 8))

        self.sentiment_status = ttk.Label(sent_btn_row, text="Idle", foreground="#a6adc8")
        self.sentiment_status.pack(side="left")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # ---- Row 3: High Alert Results (dedicated panel) ----
        alert_results_header = ttk.Frame(parent)
        alert_results_header.pack(fill="x", padx=12, pady=(0, 0))
        ttk.Label(alert_results_header, text="🚨 Recent High Alerts:",
                  font=("Segoe UI", 10, "bold"), foreground="#fab387").pack(anchor="w")

        self.alert_listbox = tk.Listbox(
            parent, height=4, bg="#1a1a2e", fg="#f38ba8",
            font=("Consolas", 9), relief="flat", borderwidth=2,
            selectbackground="#45475a", highlightthickness=0,
        )
        self.alert_listbox.pack(fill="x", padx=12, pady=(2, 0))
        self.alert_listbox.insert("end", "  (no alerts yet — click Start High Alert)")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # ---- Row 4: Sentiment Results (dedicated panel) ----
        sentiment_results_header = ttk.Frame(parent)
        sentiment_results_header.pack(fill="x", padx=12, pady=(0, 0))
        ttk.Label(sentiment_results_header, text="📊 Sentiment Analysis Results:",
                  font=("Segoe UI", 10, "bold"), foreground="#a6e3a1").pack(anchor="w")

        self.sentiment_result_text = tk.Text(
            parent, height=5, bg="#1a1a2e", fg="#a6e3a1",
            font=("Consolas", 9), relief="flat", borderwidth=2,
            wrap=tk.WORD, state="normal",
        )
        self.sentiment_result_text.pack(fill="x", padx=12, pady=(2, 0))
        self.sentiment_result_text.insert("1.0",
            "  No analysis run yet. Click 'Run Analysis' to start.\n"
            "  Charts are saved to: tools/sentiment_chart.png & tools/topic_chart.png")
        self.sentiment_result_text.config(state="disabled")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=12, pady=8)

        # ---- Row 5: Log Output ----
        ttk.Label(parent, text="Activity Log:", font=("Segoe UI", 10, "bold"),
                  foreground="#cdd6f4").pack(anchor="w", padx=12, pady=(4, 0))

        self.log_text = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD, height=18,
            bg="#11111b", fg="#cdd6f4",
            insertbackground="#f5c2e7",
            font=("Consolas", 9),
            relief="flat", borderwidth=4,
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        self.log_text.insert("end", "📋 Activity log ready.\n"
                             "   Use the buttons above to start monitoring.\n"
                             "   — Start Fetch: launches news poller in background\n"
                             "   — High Alert: AI urgency checker for breaking news\n"
                             "   — Sentiment Analysis: AI topic & sentiment extraction\n\n")

    def _build_placeholder_tab(self, parent, title: str, description: str):
        inner = ttk.Frame(parent)
        inner.place(relx=0.5, rely=0.4, anchor="center")
        ttk.Label(inner, text=title, font=("Segoe UI", 16, "bold"),
                  foreground="#f5c2e7").pack(pady=(0, 8))
        ttk.Label(inner, text=description, font=("Segoe UI", 11),
                  foreground="#9399b2", justify="center").pack()

    # ------------------------------------------------------------------
    # Button Actions
    # ------------------------------------------------------------------

    def _toggle_fetch(self):
        if self.fetch_worker.is_running():
            self.fetch_worker.stop()
            self.fetch_btn.config(text="▶ Start Fetch")
            self.fetch_status.config(text="⏹ Stopped", foreground="#f38ba8")
        else:
            try:
                interval = int(self.interval_var.get())
                if interval < 5:
                    messagebox.showwarning("Interval", "Minimum interval is 5 seconds.")
                    return
            except ValueError:
                messagebox.showwarning("Interval", "Please enter a valid number.")
                return
            self.fetch_worker.start(interval)
            self.fetch_btn.config(text="⏹ Stop Fetch")
            self.fetch_status.config(text=f"▶ Running (every {interval}s)", foreground="#a6e3a1")

    def _toggle_alert(self):
        if self.alert_worker.running:
            self.alert_worker.stop()
            self.alert_btn.config(text="▶ Start High Alert")
            self.alert_status.config(text="⏹ Stopped", foreground="#f38ba8")
        else:
            if not DEEPSEEK_KEY:
                messagebox.showerror("API Key Missing",
                                     "DEEPSEEK_API_KEY not found in ai_analysis/.env")
                return
            try:
                interval = int(self.alert_interval_var.get())
                if interval < 3:
                    messagebox.showwarning("Interval", "Minimum alert interval is 3 seconds.")
                    return
            except ValueError:
                messagebox.showwarning("Interval", "Please enter a valid number.")
                return
            self.alert_worker.start(interval)
            self.alert_btn.config(text="⏹ Stop High Alert")
            self.alert_status.config(text=f"▶ Running (every {interval}s)", foreground="#a6e3a1")

    def _run_sentiment(self):
        if not DEEPSEEK_KEY:
            messagebox.showerror("API Key Missing",
                                 "DEEPSEEK_API_KEY not found in ai_analysis/.env")
            return
        try:
            limit = int(self.sentiment_count_var.get())
            if limit < 1:
                messagebox.showwarning("Count", "Minimum is 1 article.")
                return
        except ValueError:
            messagebox.showwarning("Count", "Please enter a valid number.")
            return

        self.sentiment_status.config(text="⏳ Running…", foreground="#f9e2af")
        self.sentiment_btn.config(state="disabled")
        self.sentiment_worker.run(limit)
        # Re-enable after worker finishes (checked in poll)
        self.root.after(2000, self._check_sentiment_done)

    def _check_sentiment_done(self):
        if self.sentiment_worker.running:
            self.root.after(1000, self._check_sentiment_done)
        else:
            self.sentiment_btn.config(state="normal")
            self.sentiment_status.config(text="✅ Done", foreground="#a6e3a1")

    # ------------------------------------------------------------------
    # Thread-safe log polling
    # ------------------------------------------------------------------

    def _poll_log_queue(self):
        """Route messages: ('log',msg)→Activity Log, ('alert',msg)→Alert Listbox, ('sentiment',msg)→Sentiment Panel."""
        while True:
            try:
                msg = self.log_queue.get_nowait()
                if isinstance(msg, tuple) and len(msg) == 2:
                    msg_type, msg_text = msg
                    if msg_type == "alert":
                        # Add to alert listbox (keep last 50)
                        self.alert_listbox.insert("end", f"  {msg_text}")
                        if self.alert_listbox.size() > 50:
                            self.alert_listbox.delete(0)
                        self.alert_listbox.see("end")
                    elif msg_type == "sentiment":
                        self.sentiment_result_text.config(state="normal")
                        self.sentiment_result_text.delete("1.0", "end")
                        self.sentiment_result_text.insert("1.0", msg_text)
                        self.sentiment_result_text.config(state="disabled")
                    else:
                        self.log_text.insert("end", msg_text)
                else:
                    self.log_text.insert("end", str(msg))
                self.log_text.see("end")
            except queue.Empty:
                break
        # Update fetch status label
        if self.fetch_worker.is_running():
            self.fetch_status.config(text=f"▶ Running (every {self.fetch_worker.interval}s)", foreground="#a6e3a1")
        else:
            if self.fetch_worker.running:
                self.fetch_status.config(text="⏹ Stopped (crashed?)", foreground="#f38ba8")
        self.root.after(500, self._poll_log_queue)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()

    # Dark theme background
    root.configure(bg="#1e1e2e")

    app = NewsMonitorApp(root)

    # Center window on screen
    root.update_idletasks()
    w = root.winfo_width()
    h = root.winfo_height()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.mainloop()


if __name__ == "__main__":
    main()
