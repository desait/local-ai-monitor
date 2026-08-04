#!/usr/bin/env python3
"""Activity classification + reclaim candidate tests."""

from __future__ import annotations

import os
import sys
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.activity import classify_session, pick_reclaim_candidate  # noqa: 


class TestActivity(unittest.TestCase):
    def test_high_cpu_active_not_reclaim(self):
        cls = classify_session(
            {"app": "Grok", "session_id": "x", "cpu_pct": 40, "rss_kb": 500_000, "kind": "cli"},
            tool_activity={},
            attention_apps=set(),
        )
        self.assertEqual(cls["activity"], "active")
        self.assertFalse(cls["auto_reclaim"])
        self.assertIn("mid-stream", cls["reason"])

    def test_openclaw_idle_service_reclaim(self):
        cls = classify_session(
            {
                "app": "OpenClaw",
                "session_id": "svc:gateway",
                "cpu_pct": 0.1,
                "rss_kb": 200_000,
                "kind": "service",
            },
            tool_activity={},
            attention_apps=set(),
        )
        self.assertEqual(cls["activity"], "idle_service")
        self.assertTrue(cls["auto_reclaim"])

    def test_needs_you_blocks(self):
        cls = classify_session(
            {"app": "Claude CLI", "session_id": "s", "cpu_pct": 0.0, "rss_kb": 900_000},
            tool_activity={},
            attention_apps={"Claude CLI"},
        )
        self.assertEqual(cls["activity"], "active")
        self.assertFalse(cls["auto_reclaim"])

    def test_pick_prefers_service(self):
        sessions = [
            {
                "app": "Grok",
                "session_id": "g",
                "rss_kb": 900_000,
                "auto_reclaim": True,
                "activity_state": "idle",
                "kind": "cli",
            },
            {
                "app": "OpenClaw",
                "session_id": "svc:gateway",
                "rss_kb": 100_000,
                "auto_reclaim": True,
                "activity_state": "idle_service",
                "kind": "service",
            },
        ]
        c = pick_reclaim_candidate(sessions, prefer_services=True)
        self.assertIsNotNone(c)
        assert c is not None
        self.assertEqual(c["app"], "OpenClaw")

    def test_grok_quiet_never_auto_reclaim(self):
        """False-idle kill regression: low CPU + stale stamp must not reclaim Grok."""
        cls = classify_session(
            {
                "app": "Grok",
                "session_id": "019fb4ac-905a-70f3-acde-36d7337a834a",
                "cpu_pct": 0.0,
                "rss_kb": 200_000,
                "kind": "cli",
            },
            tool_activity={},
            attention_apps=set(),
        )
        self.assertIn(cls["activity"], ("interactive", "user_work"))
        self.assertFalse(cls["auto_reclaim"])

    def test_any_user_cli_never_auto_reclaim(self):
        """Law: all user work — not only Grok — is barred from sudden auto-stop."""
        for app, kind in (
            ("Codex", "cli"),
            ("Cursor", "cli"),
            ("SomeNewAgent", "agent"),
            ("ChatGPT", "desktop"),
        ):
            cls = classify_session(
                {
                    "app": app,
                    "session_id": "s1",
                    "cpu_pct": 0.0,
                    "rss_kb": 900_000,
                    "kind": kind,
                },
                tool_activity={},
                attention_apps=set(),
            )
            self.assertFalse(cls["auto_reclaim"], msg=f"{app}/{kind} {cls}")


if __name__ == "__main__":
    unittest.main()
