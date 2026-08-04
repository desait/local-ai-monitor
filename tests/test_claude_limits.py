#!/usr/bin/env python3
"""Claude session/weekly limit parsing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.claude_limits import (  # noqa: 
    parse_limit_message,
    parse_rate_limit_info,
    scan_jsonl_for_limits,
    _pick_best_limit,
    _to_attention_item,
    transcript_resumed_after_limit,
    collect_claude_limit_attention,
)
from local_ai_monitor.attention import collect_attention_from_sessions  # noqa: 


class TestParseLimitMessage(unittest.TestCase):
    def test_weekly_3pm(self):
        # Hit at late evening → reset is next calendar 3pm
        hit = "2026-07-29T04:03:58.912Z"  # 11:03pm CDT Jul 28
        r = parse_limit_message(
            "You've hit your weekly limit · resets 3pm (America/Chicago)",
            event_ts=hit,
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r["limit_kind"], "weekly")
        # 3pm Chicago Jul 29
        chi = ZoneInfo("America/Chicago")
        dt = datetime.fromisoformat(r["resets_at"])
        self.assertEqual(dt.astimezone(chi).hour, 15)
        self.assertEqual(dt.astimezone(chi).day, 29)

    def test_session_1150pm(self):
        hit = "2026-07-24T02:57:28.987Z"  # ~9:57pm CDT Jul 23
        r = parse_limit_message(
            "You've hit your session limit · resets 11:50pm (America/Chicago)",
            event_ts=hit,
        )
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r["limit_kind"], "session")
        chi = ZoneInfo("America/Chicago")
        dt = datetime.fromisoformat(r["resets_at"])
        local = dt.astimezone(chi)
        self.assertEqual(local.hour, 23)
        self.assertEqual(local.minute, 50)

    def test_seven_day_info(self):
        # 3pm Chicago 2026-07-29
        info = {
            "status": "allowed_warning",
            "resetsAt": 1785355200,
            "rateLimitType": "seven_day",
            "utilization": 0.91,
        }
        r = parse_rate_limit_info(info)
        self.assertIsNotNone(r)
        assert r is not None
        self.assertEqual(r["limit_kind"], "weekly")
        self.assertEqual(r["resets_at_unix"], 1785355200)


class TestScanJsonl(unittest.TestCase):
    def test_scan_429_weekly(self):
        row = {
            "type": "assistant",
            "timestamp": "2026-07-29T04:03:58.912Z",
            "error": "rate_limit",
            "apiErrorStatus": 429,
            "isApiErrorMessage": True,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "You've hit your weekly limit · resets 3pm (America/Chicago)",
                    }
                ],
            },
            "sessionId": "20db6ad9-1f5c-49e6-82fc-747202bb5617",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(row) + "\n")
            path = f.name
        try:
            events = scan_jsonl_for_limits(path)
            self.assertTrue(events)
            best = _pick_best_limit(events)
            self.assertIsNotNone(best)
            assert best is not None
            self.assertEqual(best["limit_kind"], "weekly")
            self.assertTrue(best.get("hard") or best.get("source") == "rate_limit_429")
        finally:
            os.unlink(path)


class TestResumeClearsReady(unittest.TestCase):
    """ready_to_resume must clear once the transcript continues after the limit."""

    def _write_jsonl(self, rows: list) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        return f.name

    def test_expired_with_no_followup_is_ready(self):
        past = time.time() - 3600
        hit_ts = datetime.utcfromtimestamp(past - 7200).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        # resets_at already past → expired
        chi = ZoneInfo("America/Chicago")
        reset_local = datetime.fromtimestamp(past, tz=chi)
        msg = (
            f"You've hit your weekly limit · resets "
            f"{reset_local.strftime('%-I%p').lower().lstrip('0') or reset_local.strftime('%I%p').lower().lstrip('0')} "
            f"(America/Chicago)"
        )
        # Use structured path: inject resets via rate_limit_info hard reject
        rows = [
            {
                "type": "assistant",
                "timestamp": hit_ts,
                "error": "rate_limit",
                "apiErrorStatus": 429,
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "You've hit your weekly limit · resets 3pm (America/Chicago)",
                        }
                    ],
                },
            }
        ]
        path = self._write_jsonl(rows)
        try:
            events = scan_jsonl_for_limits(path)
            best = _pick_best_limit(events)
            self.assertIsNotNone(best)
            assert best is not None
            # Force expired for unit isolation
            best = dict(best)
            best["expired"] = True
            best["resets_at_unix"] = int(past)
            item = _to_attention_item(
                best,
                {"session_id": "s-ready", "cwd": "/tmp", "name": "t", "alive": False},
                jsonl_path=path,
            )
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item["kind"], "ready_to_resume")
        finally:
            os.unlink(path)

    def test_expired_with_successful_assistant_clears(self):
        past = time.time() - 7200
        hit_ts = datetime.utcfromtimestamp(past).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        after_ts = datetime.utcfromtimestamp(past + 100).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows = [
            {
                "type": "assistant",
                "timestamp": hit_ts,
                "error": "rate_limit",
                "apiErrorStatus": 429,
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "You've hit your weekly limit · resets 3pm (America/Chicago)",
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": after_ts,
                "message": {"role": "user", "content": "Continue"},
            },
            {
                "type": "assistant",
                "timestamp": after_ts,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Continuing work."}],
                },
            },
        ]
        path = self._write_jsonl(rows)
        try:
            self.assertTrue(
                transcript_resumed_after_limit(path, after_unix=past - 1)
            )
            events = scan_jsonl_for_limits(path)
            best = _pick_best_limit(events)
            self.assertIsNotNone(best)
            assert best is not None
            best = dict(best)
            best["expired"] = True
            best["resets_at_unix"] = int(past - 60)
            best["event_ts"] = hit_ts
            item = _to_attention_item(
                best,
                {"session_id": "s-clear", "cwd": "/tmp", "name": "t", "alive": False},
                jsonl_path=path,
            )
            self.assertIsNone(item, msg="must clear ready_to_resume after successful turn")
        finally:
            os.unlink(path)

    def test_busy_live_process_clears_ready(self):
        past = time.time() - 3600
        hit_ts = datetime.utcfromtimestamp(past - 100).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows = [
            {
                "type": "assistant",
                "timestamp": hit_ts,
                "error": "rate_limit",
                "apiErrorStatus": 429,
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "You've hit your weekly limit · resets 3pm (America/Chicago)",
                        }
                    ],
                },
            }
        ]
        path = self._write_jsonl(rows)
        try:
            events = scan_jsonl_for_limits(path)
            best = _pick_best_limit(events)
            assert best is not None
            best = dict(best)
            best["expired"] = True
            best["resets_at_unix"] = int(past)
            best["event_ts"] = hit_ts
            item = _to_attention_item(
                best,
                {
                    "session_id": "s-busy",
                    "cwd": "/tmp",
                    "name": "t",
                    "alive": True,
                    "status": "busy",
                },
                jsonl_path=path,
            )
            self.assertIsNone(item)
        finally:
            os.unlink(path)

    def test_still_limited_when_reset_in_future(self):
        future = time.time() + 3600
        hit_ts = datetime.utcfromtimestamp(time.time() - 60).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        rows = [
            {
                "type": "assistant",
                "timestamp": hit_ts,
                "error": "rate_limit",
                "apiErrorStatus": 429,
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "You've hit your session limit · resets 11:50pm (America/Chicago)",
                        }
                    ],
                },
            }
        ]
        path = self._write_jsonl(rows)
        try:
            events = scan_jsonl_for_limits(path)
            best = _pick_best_limit(events)
            assert best is not None
            best = dict(best)
            best["expired"] = False
            best["resets_at_unix"] = int(future)
            item = _to_attention_item(
                best,
                {"session_id": "s-lim", "cwd": "/tmp", "name": "t", "alive": True, "status": "idle"},
                jsonl_path=path,
            )
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item["kind"], "limited")
        finally:
            os.unlink(path)


class TestLiveStuckSession(unittest.TestCase):
    """Live industry encyclopedia session — assert attention matches resumed truth."""

    def test_real_stuck_if_present(self):
        path = os.path.expanduser(
            "~/.claude/projects/-Users-alice-Claude-Projects-sample-project/"
            "20db6ad9-1f5c-49e6-82fc-747202bb5617.jsonl"
        )
        if not os.path.isfile(path):
            self.skipTest("stuck session jsonl not present")
        events = scan_jsonl_for_limits(path)
        best = _pick_best_limit(events)
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best["limit_kind"], "weekly")
        self.assertIsNotNone(best.get("resets_at_unix"))

    def test_collect_attention_matches_live_resume(self):
        """After post-limit activity, do not keep ready_to_resume for that session."""
        path = os.path.expanduser(
            "~/.claude/projects/-Users-alice-Claude-Projects-sample-project/"
            "20db6ad9-1f5c-49e6-82fc-747202bb5617.jsonl"
        )
        if not os.path.isfile(path):
            self.skipTest("session not present")
        items = collect_attention_from_sessions([])
        claude_ready = [
            i
            for i in items
            if i.get("app") == "Claude CLI"
            and i.get("session_id") == "20db6ad9-1f5c-49e6-82fc-747202bb5617"
            and i.get("kind") == "ready_to_resume"
        ]
        # Live transcript has successful assistant after the 429 → must be clear
        events = scan_jsonl_for_limits(path)
        best = _pick_best_limit(events)
        if best and transcript_resumed_after_limit(
            path, after_unix=float(_iso := (__import__("local_ai_monitor.claude_limits", fromlist=["_iso_to_unix"])._iso_to_unix(best.get("event_ts")) or 0))
        ):
            self.assertEqual(
                claude_ready,
                [],
                msg=f"stale ready_to_resume still present: {claude_ready}",
            )


if __name__ == "__main__":
    unittest.main()
