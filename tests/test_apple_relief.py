#!/usr/bin/env python3
"""Apple relief safety tests — opt-in path must not close active work."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


class TestAppleRelief(unittest.TestCase):
    def test_browser_only_and_frontmost_skipped(self):
        from local_ai_monitor import apple_relief

        rows = [
            ("Safari", 10, 1_500_000),
            ("Google Chrome Helper", 11, 1_300_000),
            ("Mail", 12, 2_000_000),
        ]
        with mock.patch.object(apple_relief, "_frontmost_app_name", return_value="Safari"):
            with mock.patch.object(apple_relief, "_ps_rss_by_comm", return_value=rows):
                apps = apple_relief.top_quitable_apps(min_rss_kb=200_000)

        self.assertEqual([a.name for a in apps], ["Google Chrome"])

    def test_unknown_frontmost_fails_closed(self):
        from local_ai_monitor import apple_relief

        rows = [("Safari", 10, 1_500_000)]
        with mock.patch.object(apple_relief, "_frontmost_app_name", return_value=None):
            with mock.patch.object(apple_relief, "_ps_rss_by_comm", return_value=rows):
                apps = apple_relief.top_quitable_apps(min_rss_kb=200_000)

        self.assertEqual(apps, [])


if __name__ == "__main__":
    unittest.main()
