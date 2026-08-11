#!/usr/bin/env python3
"""Browser cleanup contract tests."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor import browser_advice as ba  # noqa: 


class TestBrowserAdvice(unittest.TestCase):
    def test_rolls_up_browser_helpers_and_protects_frontmost(self):
        rows = [
            {
                "pid": 1,
                "ppid": 0,
                "cpu_pct": 0.0,
                "rss_kb": 90 * 1024,
                "rss_mb": 90,
                "comm": "Brave Browser",
                "args": "Brave Browser",
                "app": "Brave",
                "kind": "browser process",
                "category": "browser",
            },
            {
                "pid": 2,
                "ppid": 1,
                "cpu_pct": 0.0,
                "rss_kb": 40 * 1024,
                "rss_mb": 40,
                "comm": "Brave Browser Helper (Renderer)",
                "args": "Brave Browser Helper (Renderer) --type=renderer",
                "app": "Brave",
                "kind": "tab/renderer",
                "category": "browser",
            },
            {
                "pid": 3,
                "ppid": 0,
                "cpu_pct": 0.0,
                "rss_kb": 50 * 1024,
                "rss_mb": 50,
                "comm": "Safari",
                "args": "Safari",
                "app": "Safari",
                "kind": "browser process",
                "category": "browser",
            },
        ]
        with mock.patch.object(ba, "_ps_app_process_rows", return_value=rows), \
             mock.patch.object(ba, "_frontmost_app_name", return_value="Safari"), \
             mock.patch.object(ba, "_counts_for", return_value=None), \
             mock.patch.object(ba, "top_quitable_apps", return_value=[]):
            out = ba.browser_advice(min_rss_kb=20 * 1024)

        self.assertEqual(out["candidates"][0]["app"], "Brave")
        self.assertEqual(out["candidates"][0]["rss_mb"], 130)
        self.assertEqual(out["candidates"][0]["renderer_processes"], 1)
        self.assertEqual(out["protected"][0]["app"], "Safari")
        self.assertIn("Review Brave", out["message"])

    def test_frontmost_unknown_protects_everything(self):
        rows = [
            {
                "pid": 1,
                "ppid": 0,
                "cpu_pct": 0.0,
                "rss_kb": 90 * 1024,
                "rss_mb": 90,
                "comm": "Brave Browser",
                "args": "Brave Browser",
                "app": "Brave",
                "kind": "browser process",
                "category": "browser",
            }
        ]
        with mock.patch.object(ba, "_ps_app_process_rows", return_value=rows), \
             mock.patch.object(ba, "_frontmost_app_name", return_value=None), \
             mock.patch.object(ba, "_counts_for", return_value=None), \
             mock.patch.object(ba, "top_quitable_apps", return_value=[]):
            out = ba.browser_advice(min_rss_kb=20 * 1024)

        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["protected"][0]["app"], "Brave")
        self.assertFalse(out["protected"][0]["closeable"])

    def test_quit_refuses_when_frontmost_unknown(self):
        with mock.patch.object(ba, "_frontmost_app_name", return_value=None):
            out = ba.quit_inactive_app("Brave")
        self.assertFalse(out["acted"])
        self.assertIn("refusing", out["message"].lower())


if __name__ == "__main__":
    unittest.main()
