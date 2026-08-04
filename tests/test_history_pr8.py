"""history pane / summarize tests."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.sessionize import SessionStats  # noqa: 
from local_ai_monitor.store import AccumStore, HistoryStore, rollup_and_clear  # noqa: 
from local_ai_monitor.tui import footer_live_status, render_history  # noqa: 


def _sess(app="Grok", sid="u1", label="proj · u1", pcpu=50.0, rss=100_000):
    s = SessionStats(app=app, session_id=sid, label=label, pcpu=pcpu, rss_kb=rss)
    s.pids.add(1)
    return s


class TestHistoryRender(unittest.TestCase):
    def test_render_includes_summary_and_labels(self):
        rows = [
            {
                "bucket_start": "2026-07-29T10:00:00",
                "app": "Grok",
                "session_id": "abc",
                "label": "bloomberg · abc",
                "cpu_seconds": 12.5,
                "peak_rss_kb": 200_000,
            }
        ]
        summary = [
            {
                "app": "Grok",
                "cpu_seconds": 12.5,
                "peak_rss_kb": 200_000,
                "buckets": 1,
            }
        ]
        text = render_history(rows, summary=summary, use_color=False)
        self.assertIn("history", text.lower())
        self.assertIn("Grok", text)
        self.assertIn("bloomberg", text)
        self.assertIn("12.5", text)
        self.assertIn("h back", text)

    def test_empty_history(self):
        text = render_history([], summary=[], use_color=False)
        self.assertTrue(
            "no rollup" in text.lower() or "collector" in text.lower()
        )


class TestSummarize(unittest.TestCase):
    def test_summarize_by_app_after_rollup(self):
        td = tempfile.mkdtemp(prefix="local-ai-monitor-h8-")
        try:
            accum = AccumStore(td)
            hist = HistoryStore(td)
            mid = 1_000_000.0  # arbitrary epoch
            # Use store helpers with real wall times via on_sample
            import time
            from local_ai_monitor.store import floor_bucket_start, format_local_iso

            now = time.time()
            prev = now - 10
            accum.on_sample(prev, now, [_sess()], sample_interval=10.0, write=True)
            n = rollup_and_clear(accum, hist)
            self.assertGreaterEqual(n, 1)
            summary = hist.summarize_by_app(hours=24 * 365)
            self.assertTrue(any(s.get("app") == "Grok" for s in summary))
            rows = hist.query(hours=24 * 365, limit=10)
            self.assertTrue(rows)
            # label may be present from session_dim
            self.assertIn("label", rows[0])
            series = hist.series_by_app(hours=12.0)
            self.assertIn("Grok", series)
            self.assertEqual(len(series["Grok"]), 24)
            self.assertTrue(any(v > 0 for v in series["Grok"]))
            hist.close()
        finally:
            shutil.rmtree(td, ignore_errors=True)


class TestFooter(unittest.TestCase):
    def test_footer_no_crash(self):
        # May or may not have live.json; must not raise
        s = footer_live_status(use_color=False)
        self.assertIsInstance(s, str)


if __name__ == "__main__":
    unittest.main()
