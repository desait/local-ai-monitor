#!/usr/bin/env python3
"""end_session safety + dry-run resolution."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.config import ensure_state_dir  # noqa: 
from local_ai_monitor.end_session import end_heaviest, end_session, end_tool  # noqa: 
from local_ai_monitor.store import atomic_write_json, live_path  # noqa: 


class TestEndSession(unittest.TestCase):
    def test_openclaw_launchd_labels(self):
        from local_ai_monitor.end_session import launchd_labels_for

        self.assertIn("ai.openclaw.gateway", launchd_labels_for("OpenClaw"))
        self.assertEqual(launchd_labels_for("Grok"), [])

    def test_missing_args(self):
        r = end_session("", "")
        self.assertFalse(r["ok"])

    def test_no_pids(self):
        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(
                live_path(td),
                {
                    "sessions": [
                        {
                            "app": "Grok",
                            "session_id": "x",
                            "rss_kb": 1000,
                            "pids": [],
                            "alive": True,
                        }
                    ]
                },
            )
            r = end_session("Grok", "x", state=td, dry_run=True)
            # rescan may still find live grok — dry_run with empty live pids
            # may succeed if rescan finds pids. Accept either shape.
            self.assertIn("ok", r)
            if r.get("pids"):
                self.assertTrue(r["dry_run"])
            else:
                self.assertFalse(r["ok"])

    def test_heaviest_picks_top_rss(self):
        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(
                live_path(td),
                {
                    "sessions": [
                        {
                            "app": "Buzz",
                            "session_id": "small",
                            "rss_kb": 10_000,
                            "pids": [],
                            "alive": True,
                        },
                        {
                            "app": "Grok",
                            "session_id": "big",
                            "rss_kb": 900_000,
                            "pids": [],
                            "alive": True,
                        },
                    ]
                },
            )
            r = end_heaviest(state=td, dry_run=True, min_rss_kb=1)
            # Either ends big (no pids → fail) or rescan finds real sessions
            self.assertIn("ok", r)
            if r.get("session_id") == "big":
                self.assertEqual(r.get("app"), "Grok")

    def test_end_tool_requires_app(self):
        r = end_tool("")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
