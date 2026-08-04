#!/usr/bin/env python3
"""Tests for plain-English humanize layer."""

from __future__ import annotations

import os
import sys
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.humanize import (  # noqa: 
    activity_display,
    agent_display_name,
    channel_display_name,
    chip_text,
    load_phrase,
    mem_phrase,
    status_sentence,
    summarize_live,
    tool_display_name,
)


class TestNames(unittest.TestCase):
    def test_tools(self):
        self.assertEqual(tool_display_name("Claude CLI"), "Anthropic CLI")
        self.assertEqual(tool_display_name("Claude Desktop"), "Co-Work")
        self.assertEqual(tool_display_name("Grok"), "Grok")

    def test_channel_agent(self):
        self.assertEqual(channel_display_name("sample-app"), "Sample App")
        self.assertEqual(agent_display_name("operator/grok"), "Operator (Grok)")
        self.assertEqual(agent_display_name("tunnel"), "Network tunnel")

    def test_activity_buzz(self):
        s = {
            "app": "Buzz",
            "session_id": "buzz:sample-app|tunnel",
            "label": "sample-app · tunnel",
        }
        self.assertIn("Sample App", activity_display(s))
        self.assertIn("tunnel", activity_display(s).lower())

    def test_activity_grok_hides_pid(self):
        s = {"app": "Grok", "session_id": "pid:1:start:0", "label": "pid34002"}
        self.assertEqual(activity_display(s), "Grok chat")


class TestPhrases(unittest.TestCase):
    def test_load(self):
        self.assertEqual(load_phrase(0), "Quiet")
        self.assertEqual(load_phrase(12), "Working")
        self.assertEqual(load_phrase(60), "Very busy")

    def test_mem(self):
        self.assertIn("MB", mem_phrase(270 * 1024))

    def test_chip(self):
        self.assertEqual(chip_text(0), "AI · Quiet")
        self.assertEqual(chip_text(0, stale=True), "AI · —")
        self.assertIn("Busy", chip_text(40))

    def test_sentence(self):
        self.assertIn("fine", status_sentence(total_cpu=1, total_rss_kb=100_000).lower())
        s = status_sentence(
            total_cpu=20, total_rss_kb=100_000, top_tool="Grok", top_cpu=45
        )
        self.assertIn("Grok", s)


class TestSummarize(unittest.TestCase):
    def test_summary_shape(self):
        live = {
            "totals": {"cpu_pct": 12.0, "rss_kb": 500_000},
            "top": {"app": "Grok", "label": "x", "cpu_pct": 10, "rss_kb": 100},
            "sessions": [
                {
                    "app": "Grok",
                    "cpu_pct": 10,
                    "rss_kb": 200_000,
                    "alive": True,
                    "label": "polymath · abc",
                    "session_id": "u1",
                },
                {
                    "app": "Buzz",
                    "cpu_pct": 1,
                    "rss_kb": 20_000,
                    "alive": True,
                    "label": "sample-app · tunnel",
                    "session_id": "buzz:sample-app|tunnel",
                },
            ],
        }
        s = summarize_live(live, stale=False)
        self.assertTrue(s["tools"])
        self.assertTrue(s["buzz_projects"])
        self.assertEqual(s["buzz_projects"][0]["project"], "Sample App")


if __name__ == "__main__":
    unittest.main()
