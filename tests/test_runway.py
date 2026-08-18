#!/usr/bin/env python3
"""Runway control plane — first-principles product card + safety."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.config import ensure_state_dir  # noqa: E402
from local_ai_monitor.resource.config import DEFAULT_RESOURCE  # noqa: E402
from local_ai_monitor.resource.forecast import (  # noqa: E402
    REC_AVOID_NEW_HEAVY,
    REC_DO_NOTHING,
    REC_PROTECT_ACTIVE,
    REC_RECLAIM_IDLE,
    REC_REFUSE,
    STATE_CAUTION,
    STATE_FREEZE_RISK,
    STATE_OK,
    STATE_STOP_START_GATE,
    STATE_UNKNOWN,
    build_forecast,
)
from local_ai_monitor.resource.policy import PolicyDecision, SessionCandidate, evaluate  # noqa: E402
from local_ai_monitor.runway.actions import pause_idle  # noqa: E402
from local_ai_monitor.runway.compose import card_from_decision, compose_card  # noqa: E402
from local_ai_monitor.runway.copy import PROMISE, assert_clean, watch_lines  # noqa: E402
from local_ai_monitor.runway.model import (  # noqa: E402
    FORECAST_TO_STATE,
    REC_TO_ACTION,
    brand,
    chip_for,
)
from local_ai_monitor.store import atomic_write_json, live_path  # noqa: E402


def _decision(
    *,
    band: str,
    action: str = "none",
    forecast_state: str,
    recommendation: str,
    can_start: bool,
    headroom_mb: float = 2400.0,
    thrash: float = 0.0,
    candidate: SessionCandidate | None = None,
    ai_rss_kb: int = 200_000,
) -> PolicyDecision:
    return PolicyDecision(
        ok=band != "unknown",
        band=band,
        action=action,
        reason="test",
        free_pages=8000,
        free_mb=128.0,
        waterline_warn=8000,
        waterline_hard=4000,
        ai_session_count=1,
        ai_rss_kb=ai_rss_kb,
        candidate=candidate,
        protected_skipped=[],
        physics={
            "memsize_bytes": 8 * 1024 * 1024 * 1024,
            "headroom": {
                "headroom_mb": headroom_mb,
                "thrash_score": thrash,
                "headroom_ok_mb": 1500.0,
                "headroom_warn_mb": 600.0,
            },
            "forecast": {
                "state": forecast_state,
                "recommendation": recommendation,
                "can_start_heavy": can_start,
            },
        },
        live_ts="test",
    )


class TestOracleMapping(unittest.TestCase):
    """Runway states are the existing forecast, renamed for people."""

    def test_every_forecast_state_has_a_runway_state(self):
        for src, dest in FORECAST_TO_STATE.items():
            self.assertIn(dest, ("open", "watch", "hold", "protect", "unknown"))
            self.assertTrue(src)

    def test_forecast_builder_maps_to_runway(self):
        fixtures = [
            build_forecast(band="ok", headroom_mb=2400, thrash_score=0),
            build_forecast(band="warn", headroom_mb=2400, thrash_score=0),
            build_forecast(
                band="warn",
                headroom_mb=400,
                thrash_score=9,
                headroom_warn_mb=1200,
                has_safe_candidate=True,
            ),
            build_forecast(
                band="hard",
                headroom_mb=300,
                thrash_score=30,
                active_work_only=True,
            ),
            build_forecast(band="unknown", headroom_mb=None, thrash_score=0),
        ]
        expected = [
            ("open", "nothing", True),
            ("watch", "avoid_start", True),
            ("hold", "pause_idle", False),
            ("protect", "protect_work", False),
            ("unknown", "wait", False),
        ]
        for fc, (rstate, ract, can) in zip(fixtures, expected):
            d = _decision(
                band="ok" if fc.state == STATE_OK else (
                    "unknown" if fc.state == STATE_UNKNOWN else (
                        "hard" if fc.state == STATE_FREEZE_RISK else "warn"
                    )
                ),
                action="suggest_end" if fc.recommendation == REC_RECLAIM_IDLE else "none",
                forecast_state=fc.state,
                recommendation=fc.recommendation,
                can_start=fc.can_start_heavy,
                headroom_mb=fc.headroom_mb or 400,
                thrash=fc.thrash_score,
                candidate=(
                    SessionCandidate("OpenClaw", "svc:gw", 600_000, False)
                    if fc.recommendation == REC_RECLAIM_IDLE
                    else None
                ),
            )
            card = card_from_decision(d)
            self.assertEqual(FORECAST_TO_STATE[fc.state], rstate, fc)
            self.assertEqual(REC_TO_ACTION[fc.recommendation], ract, fc)
            self.assertEqual(card.state, rstate)
            self.assertEqual(card.action, ract)
            self.assertEqual(card.can_start, can)
            self.assertEqual(card.can_start, fc.can_start_heavy)


class TestSafety(unittest.TestCase):
    def test_assert_clean_rejects_pids(self):
        with self.assertRaises(ValueError):
            assert_clean("stop pid:1234")

    def test_copy_never_leaks_jargon(self):
        cand = SessionCandidate("OpenClaw", "svc:gateway", 700_000, False)
        d = _decision(
            band="hard",
            action="suggest_end",
            forecast_state=STATE_FREEZE_RISK,
            recommendation=REC_RECLAIM_IDLE,
            can_start=False,
            headroom_mb=300,
            thrash=40,
            candidate=cand,
        )
        card = card_from_decision(d)
        for text in (card.title, card.sentence, card.detail, card.next_step, card.chip, card.promise):
            assert_clean(text)
            self.assertNotIn("pid:", text.lower())
            self.assertNotIn(card.candidate_session_id or "svc:gateway", text)

    def test_promise_always_present(self):
        d = _decision(
            band="ok",
            forecast_state=STATE_OK,
            recommendation=REC_DO_NOTHING,
            can_start=True,
        )
        self.assertEqual(card_from_decision(d).promise, PROMISE)

    def test_pause_without_yes_refuses(self):
        cand = SessionCandidate("OpenClaw", "svc:gw", 600_000, False)
        d = _decision(
            band="hard",
            action="suggest_end",
            forecast_state=STATE_FREEZE_RISK,
            recommendation=REC_RECLAIM_IDLE,
            can_start=False,
            candidate=cand,
        )
        card = card_from_decision(d)
        with mock.patch("local_ai_monitor.end_session.end_session") as es:
            out = pause_idle(yes=False, card=card)
        self.assertFalse(out["ok"])
        self.assertFalse(out["acted"])
        es.assert_not_called()

    def test_pause_does_not_run_when_action_is_not_pause(self):
        d = _decision(
            band="ok",
            forecast_state=STATE_OK,
            recommendation=REC_DO_NOTHING,
            can_start=True,
        )
        card = card_from_decision(d)
        with mock.patch("local_ai_monitor.end_session.end_session") as es:
            out = pause_idle(yes=True, card=card)
        self.assertTrue(out["ok"])
        self.assertFalse(out["acted"])
        es.assert_not_called()

    def test_pause_yes_calls_end_session_not_heaviest(self):
        cand = SessionCandidate("OpenClaw", "svc:gw", 600_000, False)
        d = _decision(
            band="hard",
            action="suggest_end",
            forecast_state=STATE_FREEZE_RISK,
            recommendation=REC_RECLAIM_IDLE,
            can_start=False,
            candidate=cand,
        )
        card = card_from_decision(d)
        with mock.patch(
            "local_ai_monitor.end_session.end_session",
            return_value={"ok": True, "app": "OpenClaw", "session_id": "svc:gw"},
        ) as es:
            with mock.patch("local_ai_monitor.runway.actions.append_audit"):
                out = pause_idle(yes=True, card=card)
        es.assert_called_once_with("OpenClaw", "svc:gw", state=None, dry_run=False)
        self.assertTrue(out["acted"])

    def test_grok_only_hard_has_no_pause_target(self):
        """Same law as evaluate: interactive-only → protect, not pause."""
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
                    "ts": "2000-01-01T00:00:00",
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
            card = card_from_decision(d, live={"sessions": []}, stale=False)
            self.assertEqual(d.band, "hard")
            self.assertIsNone(d.candidate)
            self.assertNotEqual(card.action, "pause_idle")
            self.assertIsNone(card.candidate_session_id)


class TestCopyAndSurface(unittest.TestCase):
    def test_chip_uses_brand(self):
        self.assertEqual(chip_for("open"), f"{brand()} · Open")
        self.assertEqual(chip_for("hold"), f"{brand()} · Hold")
        self.assertTrue(chip_for("open", stale=True).endswith("—"))

    def test_watch_lines_hide_session_ids(self):
        cand = SessionCandidate("OpenClaw", "svc:secret-id", 100_000, False)
        d = _decision(
            band="warn",
            action="suggest_end",
            forecast_state=STATE_STOP_START_GATE,
            recommendation=REC_RECLAIM_IDLE,
            can_start=False,
            headroom_mb=500,
            candidate=cand,
        )
        text = watch_lines(card_from_decision(d))
        self.assertNotIn("svc:secret-id", text)
        self.assertIn("Press Q", text)
        self.assertNotIn("tmux", text.lower())

    def test_stale_refuses_to_guess(self):
        d = _decision(
            band="ok",
            forecast_state=STATE_OK,
            recommendation=REC_DO_NOTHING,
            can_start=True,
        )
        card = card_from_decision(d, stale=True)
        self.assertEqual(card.state, "unknown")
        self.assertEqual(card.action, "wait")
        self.assertFalse(card.can_start)
        self.assertIn("not updating", card.sentence.lower())

    def test_resource_block_keeps_menubar_keys(self):
        d = _decision(
            band="ok",
            forecast_state=STATE_OK,
            recommendation=REC_DO_NOTHING,
            can_start=True,
        )
        block = card_from_decision(d).to_resource_block()
        for key in (
            "band",
            "chip",
            "title",
            "detail",
            "action_label",
            "can_start_heavy",
            "plain_state",
            "next_step",
            "promise",
        ):
            self.assertIn(key, block)

    def test_cli_status_json(self):
        from local_ai_monitor.runway.cli import cmd_runway

        d = _decision(
            band="ok",
            forecast_state=STATE_OK,
            recommendation=REC_DO_NOTHING,
            can_start=True,
        )
        card = card_from_decision(d)
        with mock.patch("local_ai_monitor.runway.cli.compose_card", return_value=card):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                rc = cmd_runway(["json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "open")
        self.assertEqual(payload["action"], "nothing")

    def test_parent_cli_dispatches_runway(self):
        from local_ai_monitor.cli import main

        with mock.patch("local_ai_monitor.runway.cli.cmd_runway", return_value=0) as cr:
            rc = main(["runway", "status"])
        self.assertEqual(rc, 0)
        cr.assert_called_once()

    def test_checkout_runner_help(self):
        import subprocess

        script = os.path.join(_SRC, "scripts", "runway")
        self.assertTrue(os.path.isfile(script), script)
        self.assertTrue(os.access(script, os.X_OK), script)
        r = subprocess.run(
            [script, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("room", r.stdout.lower())

    def test_checkout_runner_follows_symlink(self):
        import subprocess

        script = os.path.join(_SRC, "scripts", "runway")
        with tempfile.TemporaryDirectory() as td:
            link = os.path.join(td, "runway")
            os.symlink(script, link)
            r = subprocess.run(
                [link, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("room", r.stdout.lower())

    def test_local_ai_monitor_runner_help(self):
        import subprocess

        script = os.path.join(_SRC, "scripts", "local-ai-monitor")
        self.assertTrue(os.access(script, os.X_OK), script)
        r = subprocess.run(
            [script, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Runway", r.stdout)

    def test_refresh_writes_live_and_opens(self):
        from local_ai_monitor.runway.compose import compose_card, snapshot_live

        with tempfile.TemporaryDirectory() as td:
            ensure_state_dir(td)
            snap = snapshot_live(td)
            self.assertTrue(snap)
            card = compose_card(state=td, live=snap, stale=False)
            self.assertIn(card.state, ("open", "watch", "hold", "protect", "unknown"))
            self.assertFalse(card.stale)

    def test_module_entry_help(self):
        import subprocess

        r = subprocess.run(
            [sys.executable, "-m", "local_ai_monitor.runway", "--help"],
            cwd=_SRC,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "PYTHONPATH": _SRC},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("room", r.stdout.lower())


class TestEvaluateOracle(unittest.TestCase):
    def test_idle_service_under_hard_is_pause(self):
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
                    "ts": "2000-01-01T00:00:00",
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
            card = compose_card(state=td, decision=d, live={"sessions": []}, stale=False)
            self.assertEqual(d.action, "suggest_end")
            self.assertEqual(d.candidate.app, "OpenClaw")
            self.assertEqual(card.action, "pause_idle")
            self.assertEqual(card.candidate_app, "OpenClaw")
            self.assertFalse(card.can_start)


if __name__ == "__main__":
    unittest.main()
