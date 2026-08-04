#!/usr/bin/env python3
"""Phase 2: attention, budget, launch recipes, CCM observer status."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.attention import scan_transcript_for_attention  # noqa: 
from local_ai_monitor.budget import build_budgets, format_week_tokens  # noqa: 
from local_ai_monitor.launch import LAUNCH_RECIPES, resolve_cwd  # noqa: 
from local_ai_monitor.ccm_observer import ccm_status_meta, run_observer_once  # noqa: 
from local_ai_monitor.store import build_live_payload  # noqa: 
from local_ai_monitor.sessionize import SessionStats  # noqa: 


class TestAttention(unittest.TestCase):
    def test_needs_you_from_assistant_question(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Which option do you prefer?",
                                }
                            ],
                        },
                    }
                )
                + "\n"
            )
            path = f.name
        try:
            items = scan_transcript_for_attention(
                path, app="Claude CLI", session_id="s1", title="Test"
            )
            kinds = {i["kind"] for i in items}
            self.assertIn("needs_you", kinds)
            self.assertEqual(items[0]["confidence"], "high")
        finally:
            os.unlink(path)

    def test_limit_with_reset(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": "Hit usage limit. Resets in 45 minutes.",
                        },
                    }
                )
                + "\n"
            )
            path = f.name
        try:
            items = scan_transcript_for_attention(
                path, app="Claude CLI", session_id="s2", title="Lim"
            )
            self.assertTrue(any(i["kind"] in ("limited", "ready_to_resume") for i in items))
            lim = next(i for i in items if i["kind"] in ("limited", "ready_to_resume"))
            self.assertIsNotNone(lim.get("resets_at"))
        finally:
            os.unlink(path)


class TestBudget(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_week_tokens(500), "500")
        self.assertIn("K", format_week_tokens(12_400))
        self.assertIn("M", format_week_tokens(12_400_000))

    def test_no_fake_remaining_percent(self):
        b = build_budgets([], [], ["Grok"])
        # Never invent "47% remaining" style labels without limit events
        for app, row in b.items():
            lab = row.get("remaining_label") or ""
            self.assertNotIn("%", lab)
            self.assertNotIn("remaining", lab.lower())


class TestLaunch(unittest.TestCase):
    def test_recipes_include_codex(self):
        self.assertIn("Codex", LAUNCH_RECIPES)
        self.assertIn("Grok", LAUNCH_RECIPES)
        self.assertEqual(LAUNCH_RECIPES["Codex"]["kind"], "terminal")

    def test_resolve_cwd_default(self):
        d = resolve_cwd("Grok")
        self.assertTrue(os.path.isdir(d))

    def test_claude_resume_uuid_helpers(self):
        from local_ai_monitor.launch import _is_conversation_uuid, _resolve_bin, _shell_quote

        sid = "20db6ad9-1f5c-49e6-82fc-747202bb5617"
        self.assertTrue(_is_conversation_uuid(sid))
        self.assertFalse(_is_conversation_uuid("pid:1:start:0"))
        self.assertFalse(_is_conversation_uuid(""))
        # Command shape for resume (no Terminal open)
        bin_path = _resolve_bin("claude")
        cmd = f"{_shell_quote(bin_path)} --resume {_shell_quote(sid)}"
        self.assertIn("--resume", cmd)
        self.assertIn(sid, cmd)

    def test_codex_resume_command_shape(self):
        from local_ai_monitor.launch import _is_conversation_uuid, _resolve_bin, _shell_quote

        sid = "019fa9d2-be5d-71e3-8315-c0b87dff5d7c"
        self.assertTrue(_is_conversation_uuid(sid))
        bin_path = _resolve_bin("codex")
        cmd = f"{_shell_quote(bin_path)} resume {_shell_quote(sid)}"
        self.assertIn(" resume ", f" {cmd} ")
        self.assertIn(sid, cmd)
        cmd_last = f"{_shell_quote(bin_path)} resume --last"
        self.assertIn("--last", cmd_last)


class TestLivePayloadPhase2(unittest.TestCase):
    def test_payload_has_attention_budgets_ccm(self):
        s = SessionStats(
            app="Grok",
            session_id="pid:1:start:0",
            label="test",
            detail="/tmp",
        )
        s.pids.add(1)
        s.pcpu = 1.0
        s.rss_kb = 1000
        payload = build_live_payload([s], collector_pid=1, sample_interval_s=10)
        self.assertIn("attention", payload)
        self.assertIn("budgets", payload)
        self.assertIn("ccm", payload)
        self.assertIsInstance(payload["attention"], list)


class TestCcmObserver(unittest.TestCase):
    def test_status_shape(self):
        m = ccm_status_meta()
        self.assertIn("note", m)
        self.assertIn("enabled", m)

    def test_once_writes_pack(self):
        r = run_observer_once()
        self.assertIn("pack", r)
        self.assertTrue(os.path.isfile(r["pack"]))
        st = ccm_status_meta()
        note = (st.get("note") or "").lower()
        # no user-facing jargon
        self.assertNotIn("learn only", note)
        self.assertNotIn("efficiency", note)


if __name__ == "__main__":
    unittest.main()
