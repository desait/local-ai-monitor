#!/usr/bin/env python3
"""Surface openability + quarantine unit tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.surface import enrich_live_row, session_surface  # noqa: 
from local_ai_monitor.quarantine import (  # noqa: 
    is_quarantined,
    load_quarantine,
    quarantine_tool,
    unquarantine_tool,
)


class TestSurface(unittest.TestCase):
    def test_openclaw_not_openable(self):
        s = session_surface(
            "OpenClaw", "svc:gateway", "gateway :18789", "", cpu_pct=0.1, rss_kb=80_000
        )
        self.assertFalse(s["openable"])
        self.assertEqual(s["kind"], "service")
        self.assertIn("Background gateway", s["open_denied"] or "")
        self.assertIn("18789", s["activity"] or "")
        self.assertIn("LaunchAgent", s["activity"] or "")

    def test_claude_desktop_openable(self):
        s = session_surface("Claude Desktop", "app:claude-desktop", "", "")
        self.assertTrue(s["openable"])
        self.assertEqual(s["kind"], "desktop")

    def test_cli_without_cwd_not_openable(self):
        s = session_surface("Grok", "pid:1:start:2", "pid:1", "", rss_kb=100_000)
        self.assertFalse(s["openable"])

    def test_cli_with_cwd_openable(self):
        s = session_surface(
            "Grok", "pid:1:start:2", "lab", "/Users/u/src/local-ai-monitor"
        )
        self.assertTrue(s["openable"])

    def test_enrich_row(self):
        row = enrich_live_row(
            {
                "app": "OpenClaw",
                "session_id": "svc:gateway",
                "label": "gateway :18789",
                "detail": "",
                "cpu_pct": 0.0,
                "rss_kb": 90000,
            }
        )
        self.assertFalse(row["openable"])
        self.assertEqual(row["kind"], "service")


class TestQuarantine(unittest.TestCase):
    def test_add_remove(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "q.json")
            with mock.patch("local_ai_monitor.quarantine.stop_launchd_for_app", create=True):
                pass
            with mock.patch(
                "local_ai_monitor.end_session.end_tool",
                return_value={"ok": True, "message": "ended"},
            ), mock.patch(
                "local_ai_monitor.end_session.stop_launchd_for_app",
                return_value=[{"label": "ai.openclaw.gateway", "bootout": True}],
            ):
                r = quarantine_tool("OpenClaw", path=path)
            self.assertTrue(r["ok"])
            self.assertTrue(is_quarantined("OpenClaw", path=path))
            data = load_quarantine(path)
            self.assertIn("OpenClaw", data["tools"])

            with mock.patch(
                "local_ai_monitor.end_session.launchd_labels_for",
                return_value=["ai.openclaw.gateway"],
            ), mock.patch("subprocess.run"):
                r2 = unquarantine_tool("OpenClaw", path=path)
            self.assertTrue(r2["ok"])
            self.assertFalse(is_quarantined("OpenClaw", path=path))


if __name__ == "__main__":
    unittest.main()
