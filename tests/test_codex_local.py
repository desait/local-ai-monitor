"""Codex local discovery + project labels + snapshot sparks."""

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

from local_ai_monitor.codex_local import (  # noqa: 
    codex_session_for_cwd,
    latest_codex_session,
)
from local_ai_monitor.humanize import activity_display  # noqa: 
from local_ai_monitor.sessionize import label_project  # noqa: 


class TestLabelProject(unittest.TestCase):
    def test_local_ai_monitor(self):
        self.assertEqual(label_project("/Users/u/Projects/local-ai-monitor", 1), "Local AI Monitor")

    def test_pid_fallback(self):
        self.assertEqual(label_project(None, 42), "pid42")


class TestCodexLocal(unittest.TestCase):
    def test_session_for_cwd_from_rollout(self):
        with tempfile.TemporaryDirectory() as td:
            sess_root = os.path.join(td, "sessions", "2026", "07", "28")
            os.makedirs(sess_root)
            cwd = "/Users/u/src/local-ai-monitor"
            sid = "019fa9d2-be5d-71e3-8315-c0b87dff5d7c"
            path = os.path.join(sess_root, f"rollout-2026-07-28T12-43-08-{sid}.jsonl")
            meta = {
                "timestamp": "2026-07-28T17:43:09.164Z",
                "type": "session_meta",
                "payload": {
                    "session_id": sid,
                    "id": sid,
                    "cwd": cwd,
                },
            }
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(meta) + "\n")
            with mock.patch("local_ai_monitor.codex_local.CODEX_SESSIONS", os.path.join(td, "sessions")):
                hit = codex_session_for_cwd(cwd)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit["session_id"], sid)
            self.assertEqual(hit["path"], path)

    def test_latest_when_present(self):
        # Live host may or may not have sessions; must not raise
        latest_codex_session()  # noqa: B018


class TestHumanCopy(unittest.TestCase):
    def test_codex_activity(self):
        s = {"app": "Codex", "label": "local-ai-monitor", "session_id": "x"}
        self.assertIn("Codex", activity_display(s))
        self.assertIn("Local AI Monitor", activity_display(s) or "local-ai-monitor" in activity_display(s).lower())


if __name__ == "__main__":
    unittest.main()
