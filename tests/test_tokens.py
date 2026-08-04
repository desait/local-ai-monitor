#!/usr/bin/env python3
"""Tests for per-session token usage enrichment."""

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

from local_ai_monitor.sessionize import SessionStats  # noqa: 
from local_ai_monitor.tokens import (  # noqa: 
    TokenUsage,
    claude_project_dir,
    enrich_sessions_with_tokens,
    fmt_token_count,
    fmt_tokens_cell,
    grok_tokens,
)


class TestFmt(unittest.TestCase):
    def test_fmt_count(self):
        self.assertEqual(fmt_token_count(None), "—")
        self.assertEqual(fmt_token_count(42), "42")
        self.assertEqual(fmt_token_count(1500), "1.5k")
        self.assertEqual(fmt_token_count(25000), "25k")
        self.assertEqual(fmt_token_count(2_500_000), "2.5M")

    def test_fmt_cell(self):
        s = SessionStats("Grok", "u", "lab")
        self.assertIn("—", fmt_tokens_cell(s, 10))
        s.tokens_total = 2_100_000
        s.context_used = 164000
        s.context_window = 500000
        cell = fmt_tokens_cell(s, 14)
        self.assertIn("2.1M", cell)
        self.assertIn("%", cell)


class TestGrokTokens(unittest.TestCase):
    def test_grok_from_tmpdir(self):
        with tempfile.TemporaryDirectory() as td:
            uuid = "019fae27-d955-7e71-b0ca-45fe8250e2ee"
            sess = os.path.join(td, "enc", uuid)
            os.makedirs(sess)
            with open(os.path.join(sess, "signals.json"), "w") as f:
                json.dump(
                    {
                        "contextTokensUsed": 10000,
                        "contextWindowTokens": 500000,
                    },
                    f,
                )
            # one turn_completed
            line = {
                "params": {
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 1000,
                            "outputTokens": 200,
                            "totalTokens": 1200,
                            "cachedReadTokens": 100,
                        },
                    }
                }
            }
            with open(os.path.join(sess, "updates.jsonl"), "w") as f:
                f.write(json.dumps(line) + "\n")
            with mock.patch("local_ai_monitor.tokens.GROK_SESSIONS", td):
                with mock.patch("local_ai_monitor.tokens._grok_uuid_index", {uuid: sess}):
                    with mock.patch("local_ai_monitor.tokens._grok_index_built_at", 1e12):
                        u = grok_tokens(uuid, "")
            self.assertEqual(u.context_used, 10000)
            self.assertEqual(u.context_window, 500000)
            self.assertEqual(u.input_tokens, 1000)
            self.assertEqual(u.output_tokens, 200)
            self.assertEqual(u.total_tokens, 1200)
            self.assertEqual(u.source, "grok")


class TestClaudePath(unittest.TestCase):
    def test_project_dir_encoding(self):
        # only checks encoding shape when dir missing
        p = claude_project_dir("/Users/u/Claude Projects/sample_project")
        # may or may not exist on this machine
        if p:
            self.assertTrue(p.endswith("-Users-alice-Claude-Projects-sample_project") or "industry" in p)


class TestEnrich(unittest.TestCase):
    def test_enrich_skips_unknown(self):
        s = SessionStats("OpenClaw", "svc:gateway", "gateway")
        enrich_sessions_with_tokens([s])
        self.assertIsNone(s.tokens_total)


if __name__ == "__main__":
    unittest.main()
