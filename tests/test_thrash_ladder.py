#!/usr/bin/env python3
"""Thrash ladder unit tests (no live SIGTERM)."""

from __future__ import annotations

import os
import sys
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.resource.thrash_ladder import (  # noqa: 
    FORCE_HOG_SUBSTRINGS,
    PAUSE_HOG_SUBSTRINGS,
    STEP_MUTE,
    STEP_PAUSE,
    _is_sacred,
    _matches,
    next_step_after,
    pause_hogs,
)


class TestThrashLadder(unittest.TestCase):
    def test_sacred_user_work(self):
        self.assertTrue(_is_sacred("/Users/x/.local/bin/grok --resume abc"))
        self.assertTrue(_is_sacred("WindowServer"))
        self.assertTrue(_is_sacred("local-ai-monitord --loop"))
        self.assertTrue(_is_sacred("/usr/local/bin/claude"))

    def test_force_not_whole_browser(self):
        self.assertNotIn("Safari", FORCE_HOG_SUBSTRINGS)
        self.assertTrue(_matches("com.apple.quicklook.ThumbnailsAgent", FORCE_HOG_SUBSTRINGS))
        self.assertTrue(_matches("Google Chrome Helper (Renderer)", PAUSE_HOG_SUBSTRINGS))

    def test_escalate(self):
        self.assertEqual(next_step_after(None, True), STEP_PAUSE)
        self.assertEqual(next_step_after(STEP_PAUSE, True), STEP_MUTE)
        self.assertEqual(next_step_after(STEP_MUTE, False), STEP_PAUSE)

    def test_pause_dry(self):
        r = pause_hogs(dry_run=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["step"], "pause")


if __name__ == "__main__":
    unittest.main()
