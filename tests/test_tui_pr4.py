"""TUI contract tests: session default, threads off, filter, palette."""

from __future__ import annotations

import json
import os
import sys
import unittest
from io import StringIO
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.classify import AppStats  # noqa: 
from local_ai_monitor.cli import main  # noqa: 
from local_ai_monitor.sessionize import SessionStats  # noqa: 
from local_ai_monitor.tui import (  # noqa: 
    PALETTE,
    app_fg,
    apply_app_filter_sessions,
    filter_apps_cycle,
    render,
    render_sessions,
    sessions_json_payload,
)


def _sess(app="Grok", sid="u1", label="lab", pcpu=10.0, threads=None):
    s = SessionStats(
        app=app,
        session_id=sid,
        label=label,
        pcpu=pcpu,
        rss_kb=102400,
        threads=threads,
    )
    s.pids.add(1)
    return s


class TestPalette(unittest.TestCase):
    def test_night_instrument_palette(self):
        # Product marks stay distinct; chrome is cool gray; brand is violet
        self.assertEqual(app_fg("Grok"), PALETTE["Grok"])
        self.assertEqual(app_fg("Buzz"), PALETTE["Buzz"])
        self.assertEqual(app_fg("OpenClaw"), PALETTE["OpenClaw"])
        self.assertEqual(app_fg("Claude CLI"), PALETTE["Claude CLI"])
        self.assertEqual(app_fg("ChatGPT"), PALETTE["ChatGPT"])
        self.assertEqual(PALETTE["header"], 245)
        self.assertEqual(PALETTE["brand"], 147)
        self.assertIn("heat_hot", PALETTE)


class TestFilterCycle(unittest.TestCase):
    """I-TUI-01 related: filter cycle."""

    def test_cycle_all_then_apps(self):
        from local_ai_monitor.classify import APPS

        cur = None
        seen = []
        # one full pass through catalog order
        for _ in range(len(APPS) + 2):
            cur = filter_apps_cycle(cur)
            seen.append(cur)
        self.assertEqual(seen[0], APPS[0])
        self.assertIn("Grok", seen)
        self.assertIn("Codex", seen)
        # full cycle returns to None: len(APPS) steps after leaving None
        cur = None
        for _ in range(len(APPS) + 1):
            cur = filter_apps_cycle(cur)
        self.assertIsNone(cur)

    def test_apply_filter(self):
        sessions = [_sess("Grok"), _sess("Buzz", "b1", "buzz")]
        f = apply_app_filter_sessions(sessions, "Grok")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].app, "Grok")


class TestRender(unittest.TestCase):
    def test_session_render_aligned_table(self):
        text = render_sessions(
            [_sess(threads=None)],
            show_threads=False,
            use_color=False,
        )
        self.assertIn("session", text.lower())
        self.assertIn("Grok", text)
        self.assertIn("NAME", text)
        self.assertIn("CPU", text)
        self.assertIn("TOKENS", text)
        self.assertIn("q", text)
        # no bars / dual-view chrome
        self.assertNotIn("█", text)
        self.assertNotIn("view=session", text)

    def test_app_render_same_columns(self):
        stats = {"Grok": AppStats(name="Grok")}
        stats["Grok"].pids.add(1)
        stats["Grok"].pcpu = 5.0
        stats["Grok"].rss_kb = 1000
        text = render(stats, app_filter="Grok", use_color=False)
        self.assertIn("Grok", text)
        self.assertIn("by app", text)
        self.assertIn("NAME", text)
        self.assertIn("TOKENS", text)


class TestJsonThreadsNull(unittest.TestCase):
    def test_sessions_json_null_threads(self):
        payload = sessions_json_payload([_sess(threads=None)])
        self.assertIsNone(payload["sessions"][0]["threads"])


class TestCliDefaults(unittest.TestCase):
    """Default view=session, threads off → threads null in --json."""

    def test_default_json_session_threads_null(self):
        fake = [_sess("Grok", "abc", "x", pcpu=1.0, threads=None)]

        def _fake_collect_sessions(include_threads=False, **kwargs):
            # When threads off, leave None
            out = []
            for s in fake:
                s2 = SessionStats(
                    app=s.app,
                    session_id=s.session_id,
                    label=s.label,
                    pcpu=s.pcpu,
                    rss_kb=s.rss_kb,
                    threads=(3 if include_threads else None),
                )
                s2.pids = set(s.pids)
                out.append(s2)
            return out

        with mock.patch("local_ai_monitor.tui.collect_sessions", side_effect=_fake_collect_sessions):
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                rc = main(["once", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data.get("view"), "session")
            self.assertIsNone(data["sessions"][0]["threads"])

    def test_threads_flag_enables(self):
        fake = [_sess("Grok", "abc", "x", pcpu=1.0)]

        def _fake_collect_sessions(include_threads=False, **kwargs):
            s = SessionStats(
                app="Grok",
                session_id="abc",
                label="x",
                pcpu=1.0,
                rss_kb=1024,
                threads=(9 if include_threads else None),
            )
            s.pids.add(1)
            return [s]

        with mock.patch("local_ai_monitor.tui.collect_sessions", side_effect=_fake_collect_sessions):
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                rc = main(["once", "--json", "--threads"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["sessions"][0]["threads"], 9)

    def test_no_threads_is_noop_still_null(self):
        def _fake_collect_sessions(include_threads=False, **kwargs):
            s = SessionStats(
                app="Grok",
                session_id="abc",
                label="x",
                pcpu=1.0,
                rss_kb=1024,
                threads=(9 if include_threads else None),
            )
            s.pids.add(1)
            return [s]

        with mock.patch("local_ai_monitor.tui.collect_sessions", side_effect=_fake_collect_sessions):
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                # --no-threads alone must NOT enable threads
                rc = main(["once", "--json", "--no-threads"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertIsNone(data["sessions"][0]["threads"])

    def test_view_app_flag(self):
        def _fake_collect_sessions(include_threads=False, **kwargs):
            s = SessionStats(
                app="Grok",
                session_id="abc",
                label="x",
                pcpu=2.0,
                rss_kb=2048,
                threads=(0 if include_threads else None),
            )
            s.pids.add(1)
            return [s]

        with mock.patch(
            "local_ai_monitor.tui.collect_sessions", side_effect=_fake_collect_sessions
        ):
            buf = StringIO()
            with mock.patch("sys.stdout", buf):
                rc = main(["once", "--json", "--view", "app"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertIn("Grok", data)
            self.assertIsNone(data["Grok"]["threads"])


if __name__ == "__main__":
    unittest.main()
