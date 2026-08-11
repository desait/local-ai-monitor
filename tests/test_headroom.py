#!/usr/bin/env python3
"""Headroom / freeze-risk band unit tests."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.resource.headroom import (  # noqa: 
    band_for_headroom,
    compute_headroom_mb,
    thrash_score_from_deltas,
)
from local_ai_monitor.resource.host_profile import thresholds_for_memsize
from local_ai_monitor.resource.physics import sample_physics


class TestHeadroom(unittest.TestCase):
    def test_headroom_includes_file_cache(self):
        # free 80 + spec 20 + purge 100 + 0.75*3000 file ≈ 2450
        h = compute_headroom_mb(
            free_mb=80,
            speculative_mb=20,
            purgeable_mb=100,
            file_backed_mb=3000,
            file_reclaim=0.75,
        )
        self.assertGreater(h, 2000)
        self.assertLess(h, 2800)

    def test_low_free_still_ok_with_cache(self):
        h = compute_headroom_mb(
            free_mb=60,
            speculative_mb=20,
            purgeable_mb=150,
            file_backed_mb=2800,
        )
        band = band_for_headroom(h, thrash=0.0)
        self.assertEqual(band, "ok")

    def test_24gb_thresholds_are_bounded_not_linear(self):
        t = thresholds_for_memsize(24 * 1024**3)
        self.assertEqual(t.headroom_ok_mb, 3072.0)
        self.assertLess(t.headroom_ok_mb, 4500.0)
        self.assertGreaterEqual(t.headroom_warn_mb, 900.0)
        self.assertLessEqual(t.headroom_warn_mb, 1100.0)
        self.assertEqual(
            band_for_headroom(
                3300.0,
                thrash=0.0,
                ok_mb=t.headroom_ok_mb,
                warn_mb=t.headroom_warn_mb,
            ),
            "ok",
        )

    def test_large_hosts_hit_threshold_ceiling(self):
        t = thresholds_for_memsize(128 * 1024**3)
        self.assertEqual(t.headroom_ok_mb, 4096.0)
        self.assertEqual(t.headroom_warn_mb, 1400.0)

    def test_thrash_dominates(self):
        h = compute_headroom_mb(
            free_mb=2000,
            speculative_mb=0,
            purgeable_mb=0,
            file_backed_mb=2000,
        )
        self.assertEqual(band_for_headroom(h, thrash=30.0), "hard")
        self.assertEqual(band_for_headroom(h, thrash=10.0), "warn")

    def test_thin_headroom_hard(self):
        h = compute_headroom_mb(
            free_mb=20,
            speculative_mb=0,
            purgeable_mb=10,
            file_backed_mb=100,
            file_reclaim=0.75,
        )
        self.assertLess(h, 600)
        self.assertEqual(band_for_headroom(h, thrash=0.0), "hard")

    def test_thrash_score_rates(self):
        # 100 swapouts in 60s → score includes 2*100
        s = thrash_score_from_deltas(
            dt_s=60.0,
            d_swapins=0,
            d_swapouts=100,
            d_pageouts=0,
            d_decompressions=0,
        )
        self.assertGreaterEqual(s, 200.0)

    def test_decomp_alone_not_thrash(self):
        s = thrash_score_from_deltas(
            dt_s=60.0,
            d_swapins=0,
            d_swapouts=0,
            d_pageouts=0,
            d_decompressions=10_000,
        )
        self.assertEqual(s, 0.0)

    def test_short_interval_not_amplified_to_hard(self):
        """dt≈1s with tiny deltas must not explode to hard thrash (false red)."""
        s = thrash_score_from_deltas(
            dt_s=1.0,
            d_swapins=0,
            d_swapouts=2,
            d_pageouts=10,
            d_decompressions=0,
        )
        # Without floor: scale=60 → swap_r=120 → score=240 hard.
        # With MIN_THRASH_RATE_DT_S=15: scale=4 → score well below hard.
        self.assertLess(s, 25.0)
        self.assertGreater(s, 0.0)

    def test_short_window_still_scores_but_capped(self):
        s15 = thrash_score_from_deltas(
            dt_s=15.0,
            d_swapins=0,
            d_swapouts=10,
            d_pageouts=0,
            d_decompressions=0,
        )
        s60 = thrash_score_from_deltas(
            dt_s=60.0,
            d_swapins=0,
            d_swapouts=10,
            d_pageouts=0,
            d_decompressions=0,
        )
        # 10 swapouts in 15s rates higher than same count over 60s — but both finite.
        self.assertGreaterEqual(s15, s60)
        self.assertLess(s15, 500.0)

    def test_memsize_falls_back_when_sysctl_blocked(self):
        vm = "Pages free: 1000.\n"
        with mock.patch("local_ai_monitor.resource.physics._sysctl_int", return_value=None):
            phys = sample_physics(vm_stat_text=vm, free_pages_override=1000)
        self.assertIsNotNone(phys.memsize_bytes)
        self.assertGreater(phys.memsize_bytes or 0, 0)


if __name__ == "__main__":
    unittest.main()
