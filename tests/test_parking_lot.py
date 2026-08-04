#!/usr/bin/env python3
"""Parking Lot status contract tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.parking_lot import parking_status  # noqa: 


class TestParkingLot(unittest.TestCase):
    def test_idle_helper_is_parkable_active_work_is_protected(self):
        with tempfile.TemporaryDirectory() as td:
            live = {
                "sessions": [
                    {
                        "app": "Buzz",
                        "session_id": "buzz:sample-app|tunnel",
                        "label": "sample-app · tunnel",
                        "kind": "agent",
                        "activity_state": "idle",
                        "rss_kb": 18432,
                        "pids": [12345],
                    },
                    {
                        "app": "Claude CLI",
                        "session_id": "pid:99",
                        "label": "volumes",
                        "kind": "cli",
                        "activity_state": "active",
                        "rss_kb": 400000,
                        "pids": [99],
                    },
                ]
            }
            with open(os.path.join(td, "live.min.json"), "w", encoding="utf-8") as f:
                json.dump(live, f)

            status = parking_status(state=td)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["parkable_count"], 1)
        self.assertEqual(status["parkable"][0]["app"], "Buzz")
        self.assertEqual(status["parkable"][0]["action_label"], "Lower priority")
        self.assertEqual(status["protected"][0]["app"], "Claude CLI")
        self.assertIn("will not park active work", status["protected"][0]["reason"].lower())


if __name__ == "__main__":
    unittest.main()
