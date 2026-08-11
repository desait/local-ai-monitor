#!/usr/bin/env python3
"""local-ai-rm physics + policy + CLI safety (no auto-kill)."""

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

from local_ai_monitor.config import ensure_state_dir  # noqa: 
from local_ai_monitor.resource.config import (  # noqa: 
    DEFAULT_RESOURCE,
    load_resource_config,
    write_default_resource_config,
)
from local_ai_monitor.resource.physics import parse_vm_stat, sample_physics  # noqa: 
from local_ai_monitor.resource.forecast import (  # noqa: 
    REC_AVOID_NEW_HEAVY,
    REC_PROTECT_ACTIVE,
    REC_RECLAIM_IDLE,
    STATE_CAUTION,
    STATE_FREEZE_RISK,
    STATE_STOP_START_GATE,
    build_forecast,
)
from local_ai_monitor.resource.policy import (  # noqa: 
    band_for_free_pages,
    evaluate,
    preflight_ok,
    rank_candidates,
)
from local_ai_monitor.resource.cli import cmd_resource  # noqa: 
from local_ai_monitor.store import atomic_write_json, live_path  # noqa: 

_VM_FIXTURE = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               3663.
Pages active:                            87518.
Pages speculative:                        2346.
Pages purgeable:                             2.
Pages occupied by compressor:           157694.
Swapins:                               4249546.
Swapouts:                              5761467.
"""


class TestPhysics(unittest.TestCase):
    def test_parse_vm_stat(self):
        m = parse_vm_stat(_VM_FIXTURE)
        self.assertEqual(m["pages free"], 3663)
        self.assertEqual(m["pages occupied by compressor"], 157694)
        self.assertEqual(m["swapins"], 4249546)

    def test_sample_override(self):
        p = sample_physics(free_pages_override=5000, page_size=16384)
        self.assertTrue(p.ok)
        self.assertEqual(p.free_pages, 5000)
        self.assertAlmostEqual(p.free_mb or 0, 5000 * 16384 / (1024 * 1024), places=0)

    def test_sample_from_vm_stat_text(self):
        p = sample_physics(vm_stat_text=_VM_FIXTURE, page_size=16384)
        # may also pick sysctl; free pages should be present
        self.assertTrue(p.ok)
        self.assertIsNotNone(p.free_pages)


class TestPolicy(unittest.TestCase):
    def test_forecast_static_swap_caution(self):
        f = build_forecast(
            band="ok",
            headroom_mb=2400,
            thrash_score=0,
            swap_used_mb=1800,
            swap_total_mb=3072,
        )
        self.assertEqual(f.state, STATE_CAUTION)
        self.assertEqual(f.recommendation, REC_AVOID_NEW_HEAVY)
        self.assertTrue(f.can_start_heavy)

    def test_forecast_warn_without_thrash_is_watch_not_gate(self):
        f = build_forecast(
            band="warn",
            headroom_mb=2600,
            thrash_score=0,
            swap_used_mb=0,
            swap_total_mb=3072,
        )
        self.assertEqual(f.state, STATE_CAUTION)
        self.assertEqual(f.recommendation, REC_AVOID_NEW_HEAVY)
        self.assertTrue(f.can_start_heavy)

    def test_forecast_thrashing_with_room_is_watch_not_freeze(self):
        f = build_forecast(
            band="hard",
            headroom_mb=2800,
            headroom_warn_mb=983,
            thrash_score=104,
            swap_used_mb=2800,
            swap_total_mb=4096,
        )
        self.assertEqual(f.state, STATE_CAUTION)
        self.assertTrue(f.can_start_heavy)

    def test_forecast_warn_gates_new_starts(self):
        f = build_forecast(
            band="warn",
            headroom_mb=1000,
            thrash_score=9,
            swap_used_mb=1200,
            swap_total_mb=3072,
            has_safe_candidate=True,
        )
        self.assertEqual(f.state, STATE_STOP_START_GATE)
        self.assertEqual(f.recommendation, REC_RECLAIM_IDLE)
        self.assertFalse(f.can_start_heavy)

    def test_forecast_freeze_risk_protects_active_only(self):
        f = build_forecast(
            band="hard",
            headroom_mb=300,
            thrash_score=30,
            active_work_only=True,
        )
        self.assertEqual(f.state, STATE_FREEZE_RISK)
        self.assertEqual(f.recommendation, REC_PROTECT_ACTIVE)
        self.assertFalse(f.can_start_heavy)

    def test_bands(self):
        self.assertEqual(band_for_free_pages(9000, warn=8000, hard=4000, physics_ok=True), "ok")
        self.assertEqual(band_for_free_pages(5000, warn=8000, hard=4000, physics_ok=True), "warn")
        self.assertEqual(band_for_free_pages(3000, warn=8000, hard=4000, physics_ok=True), "hard")
        self.assertEqual(band_for_free_pages(None, warn=8000, hard=4000, physics_ok=False), "unknown")

    def test_rank_skips_protected(self):
        sessions = [
            {"app": "OpenClaw", "session_id": "gw", "rss_kb": 900_000},
            {"app": "Grok", "session_id": "big", "rss_kb": 400_000},
            {"app": "Buzz", "session_id": "small", "rss_kb": 50_000},
        ]
        best, skipped = rank_candidates(sessions, protect=["OpenClaw"], min_rss_kb=1)
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best.app, "Grok")
        self.assertEqual(best.session_id, "big")
        self.assertTrue(any("OpenClaw" in s for s in skipped))

    def test_evaluate_suggest_under_hard(self):
        from local_ai_monitor.resource import headroom as hr_mod
        from local_ai_monitor.resource.headroom import HeadroomSample

        fake = HeadroomSample(
            ok=True,
            band="hard",
            headroom_mb=400.0,
            thrash_score=30.0,
            free_mb=50.0,
            speculative_mb=10.0,
            purgeable_mb=20.0,
            file_backed_mb=100.0,
            anonymous_mb=4000.0,
            wired_mb=1000.0,
            compressor_mb=500.0,
            cheap_mb=80.0,
            swapins=0,
            swapouts=100,
            pageouts=50,
            decompressions=1000,
            compressions=2000,
            load_1=2.0,
            memsize_mb=8192.0,
            reason="test thrash",
            physics={},
            prev_age_s=60.0,
        )
        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(
                live_path(td),
                {
                    "ts": "test",
                    "sessions": [
                        {
                            "app": "Grok",
                            "session_id": "h",
                            "rss_kb": 900_000,
                            "cpu_pct": 0,
                            "kind": "cli",
                            "pids": [99999],
                        },
                        {
                            "app": "OpenClaw",
                            "session_id": "svc:gateway",
                            "rss_kb": 600_000,
                            "cpu_pct": 0.1,
                            "kind": "service",
                            "pids": [99998],
                        },
                    ],
                },
            )
            cfg = dict(DEFAULT_RESOURCE)
            cfg["protect_tools"] = []
            cfg["min_rss_kb"] = 1
            with mock.patch.object(hr_mod, "sample_headroom", return_value=fake):
                d = evaluate(state=td, config=cfg, free_pages_override=2000)
            self.assertEqual(d.band, "hard")
            self.assertEqual(d.action, "suggest_end")
            self.assertIsNotNone(d.candidate)
            assert d.candidate is not None
            # Interactive Grok never suggested; idle service is the candidate
            self.assertEqual(d.candidate.app, "OpenClaw")

    def test_evaluate_no_suggest_grok_only(self):
        """Grok alone under hard → no auto-kill candidate (interactive)."""
        from local_ai_monitor.resource import headroom as hr_mod
        from local_ai_monitor.resource.headroom import HeadroomSample

        fake = HeadroomSample(
            ok=True,
            band="hard",
            headroom_mb=300.0,
            thrash_score=40.0,
            free_mb=40.0,
            speculative_mb=0.0,
            purgeable_mb=0.0,
            file_backed_mb=50.0,
            anonymous_mb=5000.0,
            wired_mb=1000.0,
            compressor_mb=800.0,
            cheap_mb=40.0,
            swapins=10,
            swapouts=50,
            pageouts=100,
            decompressions=5000,
            compressions=8000,
            load_1=4.0,
            memsize_mb=8192.0,
            reason="test thrash",
            physics={},
            prev_age_s=60.0,
        )
        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(
                live_path(td),
                {
                    "ts": "test",
                    "sessions": [
                        {
                            "app": "Grok",
                            "session_id": "h",
                            "rss_kb": 900_000,
                            "cpu_pct": 0,
                            "kind": "cli",
                            "pids": [99999],
                        }
                    ],
                },
            )
            cfg = dict(DEFAULT_RESOURCE)
            cfg["protect_tools"] = []
            cfg["min_rss_kb"] = 1
            with mock.patch.object(hr_mod, "sample_headroom", return_value=fake):
                d = evaluate(state=td, config=cfg, free_pages_override=2000)
            self.assertEqual(d.band, "hard")
            self.assertIsNone(d.candidate)

    def test_headroom_fail_does_not_use_free_page_band(self):
        """Exception / failed headroom → unknown, never free-page waterlines."""
        from local_ai_monitor.resource import headroom as hr_mod
        from local_ai_monitor.resource.headroom import HeadroomSample

        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(live_path(td), {"ts": "t", "sessions": []})
            # sample_headroom raises → unknown (not free-page hard at 2000 pages)
            with mock.patch.object(hr_mod, "sample_headroom", side_effect=RuntimeError("boom")):
                d = evaluate(state=td, config=dict(DEFAULT_RESOURCE), free_pages_override=2000)
            self.assertEqual(d.band, "unknown")
            # Not-ok sample also refuses free-page band
            failed = HeadroomSample(
                ok=False,
                band="unknown",
                headroom_mb=None,
                thrash_score=0.0,
                free_mb=None,
                speculative_mb=None,
                purgeable_mb=None,
                file_backed_mb=None,
                anonymous_mb=None,
                wired_mb=None,
                compressor_mb=None,
                cheap_mb=None,
                swapins=None,
                swapouts=None,
                pageouts=None,
                decompressions=None,
                compressions=None,
                load_1=None,
                memsize_mb=None,
                reason="fail",
                physics={},
                prev_age_s=None,
                error="no free pages",
            )
            with mock.patch.object(hr_mod, "sample_headroom", return_value=failed):
                d2 = evaluate(state=td, config=dict(DEFAULT_RESOURCE), free_pages_override=2000)
            self.assertEqual(d2.band, "unknown")

    def test_low_free_pages_can_still_be_ok_band(self):
        """Product pivot: low free pages alone must not force hard."""
        from local_ai_monitor.resource import headroom as hr_mod
        from local_ai_monitor.resource.headroom import HeadroomSample

        fake = HeadroomSample(
            ok=True,
            band="ok",
            headroom_mb=2400.0,
            thrash_score=0.0,
            free_mb=62.0,
            speculative_mb=20.0,
            purgeable_mb=180.0,
            file_backed_mb=3000.0,
            anonymous_mb=2800.0,
            wired_mb=1100.0,
            compressor_mb=500.0,
            cheap_mb=262.0,
            swapins=0,
            swapouts=0,
            pageouts=100,
            decompressions=1000,
            compressions=2000,
            load_1=1.0,
            memsize_mb=8192.0,
            reason="headroom healthy despite low free",
            physics={},
            prev_age_s=60.0,
        )
        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(live_path(td), {"sessions": []})
            with mock.patch.object(hr_mod, "sample_headroom", return_value=fake):
                d = evaluate(state=td, config=dict(DEFAULT_RESOURCE), free_pages_override=3000)
            self.assertEqual(d.band, "ok")
            self.assertEqual(d.action, "none")

    def test_evaluate_ok_no_action(self):
        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            atomic_write_json(
                live_path(td),
                {
                    "sessions": [
                        {
                            "app": "Grok",
                            "session_id": "h",
                            "rss_kb": 500_000,
                            "pids": [],
                        }
                    ],
                },
            )
            d = evaluate(
                state=td,
                config=DEFAULT_RESOURCE,
                free_pages_override=50_000,
            )
            self.assertEqual(d.band, "ok")
            self.assertEqual(d.action, "none")

    def test_preflight_fail_hard(self):
        from local_ai_monitor.resource import headroom as hr_mod
        from local_ai_monitor.resource.headroom import HeadroomSample

        fake = HeadroomSample(
            ok=True,
            band="hard",
            headroom_mb=200.0,
            thrash_score=50.0,
            free_mb=30.0,
            speculative_mb=0.0,
            purgeable_mb=0.0,
            file_backed_mb=50.0,
            anonymous_mb=6000.0,
            wired_mb=1200.0,
            compressor_mb=900.0,
            cheap_mb=30.0,
            swapins=100,
            swapouts=200,
            pageouts=500,
            decompressions=10000,
            compressions=20000,
            load_1=8.0,
            memsize_mb=8192.0,
            reason="test thrash",
            physics={},
            prev_age_s=60.0,
        )
        with mock.patch.object(hr_mod, "sample_headroom", return_value=fake):
            d = preflight_ok(
                config=DEFAULT_RESOURCE,
                require_band="ok",
                free_pages_override=2000,
            )
        self.assertFalse(d.ok)
        self.assertEqual(d.action, "preflight_fail")

    def test_preflight_pass_ok(self):
        from local_ai_monitor.resource import headroom as hr_mod
        from local_ai_monitor.resource.headroom import HeadroomSample

        fake = HeadroomSample(
            ok=True,
            band="ok",
            headroom_mb=2500.0,
            thrash_score=0.0,
            free_mb=200.0,
            speculative_mb=50.0,
            purgeable_mb=100.0,
            file_backed_mb=2800.0,
            anonymous_mb=2500.0,
            wired_mb=1000.0,
            compressor_mb=400.0,
            cheap_mb=350.0,
            swapins=0,
            swapouts=0,
            pageouts=0,
            decompressions=0,
            compressions=0,
            load_1=1.0,
            memsize_mb=8192.0,
            reason="test ok",
            physics={},
            prev_age_s=60.0,
        )
        with mock.patch.object(hr_mod, "sample_headroom", return_value=fake):
            d = preflight_ok(
                config=DEFAULT_RESOURCE,
                require_band="ok",
                free_pages_override=50_000,
            )
        self.assertTrue(d.ok)


class TestConfig(unittest.TestCase):
    def test_defaults_idle_only_auto(self):
        # Auto-end OFF by default after false Grok kill; never auto_end_active
        self.assertFalse(DEFAULT_RESOURCE["auto_end"])
        self.assertFalse(DEFAULT_RESOURCE.get("auto_end_active"))
        self.assertFalse(DEFAULT_RESOURCE.get("auto_end_cli"))
        self.assertIn("Grok", DEFAULT_RESOURCE["protect_tools"])
        self.assertGreaterEqual(DEFAULT_RESOURCE["min_rss_kb"], 200 * 1024)
        self.assertGreaterEqual(DEFAULT_RESOURCE["heavy_rss_mb"], 1024)

    def test_waterlines_scale_8gb(self):
        from local_ai_monitor.resource.config import waterlines_for_host

        warn, hard = waterlines_for_host(memsize_bytes=8 * 1024**3, page_size=16384)
        self.assertEqual(hard, 4000)
        self.assertEqual(warn, 8000)

    def test_write_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "resource.json")
            write_default_resource_config(path)
            cfg = load_resource_config(path)
            self.assertFalse(cfg["auto_end"])
            self.assertFalse(cfg.get("auto_end_active"))
            self.assertIn("Grok", cfg["protect_tools"])
            self.assertGreater(cfg["waterline_free_pages_hard"], 0)


class TestHuman(unittest.TestCase):
    def test_humanize_hard_show(self):
        from local_ai_monitor.resource.human import humanize_decision
        from local_ai_monitor.resource.policy import PolicyDecision, SessionCandidate

        d = PolicyDecision(
            ok=True,
            band="hard",
            action="suggest_end",
            reason="test",
            free_pages=2000,
            free_mb=31.0,
            waterline_warn=8000,
            waterline_hard=4000,
            ai_session_count=2,
            ai_rss_kb=300_000,
            candidate=SessionCandidate("Claude CLI", "s1", 150_000, False),
            protected_skipped=[],
            physics={
                "headroom": {
                    "headroom_mb": 350.0,
                    "thrash_score": 0.0,
                }
            },
            live_ts=None,
        )
        h = humanize_decision(d)
        self.assertTrue(h["show"])
        self.assertEqual(h["chip"], "AI · Headroom low")
        self.assertIn("Anthropic CLI", h["candidate_label"] or "")
        self.assertNotIn("pid:", h["detail"] or "")
        self.assertIn("mid-stream", h["detail"] or "")
        self.assertNotIn("almost out of free memory", (h["title"] or "").lower())
        self.assertNotIn("memory is getting tight", (h["title"] or "").lower())
        self.assertEqual(h["action_label"], "Reclaim idle")
        self.assertNotEqual(h["action_label"], "Free RAM")


class TestCliSafety(unittest.TestCase):
    def test_apply_without_yes_refuses(self):
        from local_ai_monitor.resource.policy import PolicyDecision, SessionCandidate

        fake = PolicyDecision(
            ok=True,
            band="hard",
            action="suggest_end",
            reason="test",
            free_pages=1000,
            free_mb=16.0,
            waterline_warn=8000,
            waterline_hard=4000,
            ai_session_count=1,
            ai_rss_kb=500_000,
            candidate=SessionCandidate("Grok", "x", 500_000, False),
            protected_skipped=[],
            physics={},
            live_ts=None,
        )
        with mock.patch("local_ai_monitor.resource.cli.evaluate", return_value=fake):
            with mock.patch("local_ai_monitor.resource.cli.end_session") as es:
                with mock.patch("local_ai_monitor.resource.cli.end_heaviest") as eh:
                    rc = cmd_resource(["apply"])
                    self.assertEqual(rc, 2)
                    es.assert_not_called()
                    eh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
