"""Contract tests for per-bucket accum + SQLite rollup.

IDs: C-ROLL-01..04
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.sessionize import SessionStats  # noqa: 
from local_ai_monitor.store import (  # noqa: 
    AccumStore,
    HistoryStore,
    floor_bucket_start,
    format_local_iso,
    parse_local_iso,
    rollup_and_clear,
    split_interval,
)


def _sess(
    app: str = "Grok",
    session_id: str = "019fae27-d955-7e71-b0ca-45fe8250e2ee",
    label: str = "proj · 019fae27",
    detail: str = "/tmp/proj",
    pcpu: float = 50.0,
    rss_kb: int = 100_000,
    nproc: int = 2,
) -> SessionStats:
    s = SessionStats(
        app=app,
        session_id=session_id,
        label=label,
        detail=detail,
        pcpu=pcpu,
        rss_kb=rss_kb,
    )
    for i in range(nproc):
        s.pids.add(1000 + i)
    return s


class TestSplitInterval(unittest.TestCase):
    def test_split_crosses_30m_boundary(self):
        # Local wall: construct prev just before a :30 or :00 boundary
        # Use a known epoch: pick midday and floor
        # 2026-07-29 10:29:50 local — depends on TZ; use floor_bucket_start math
        # Build times from a known bucket start
        mid = parse_local_iso("2026-07-29T10:00:00")
        if mid <= 0:
            self.skipTest("parse failed")
        # bucket 10:00 covers [10:00, 10:30)
        prev = mid + 29 * 60 + 50  # 10:29:50
        now = mid + 30 * 60 + 10  # 10:30:10
        slices = split_interval(prev, now)
        self.assertEqual(len(slices), 2, slices)
        keys = [b for _, b in slices]
        self.assertEqual(keys[0], format_local_iso(floor_bucket_start(prev)))
        self.assertEqual(keys[1], format_local_iso(floor_bucket_start(now)))
        # sub_dt sum equals total
        self.assertAlmostEqual(sum(d for d, _ in slices), now - prev, places=5)
        self.assertAlmostEqual(slices[0][0], 10.0, places=3)  # 50→30m = 10s
        self.assertAlmostEqual(slices[1][0], 10.0, places=3)  # 30m→10 = 10s


class TestCRoll(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="local-ai-monitor-roll-")
        self.state = self.tmpdir

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_c_roll_01_cross_boundary_two_bucket_keys(self):
        """C-ROLL-01: dt crosses :30 boundary → two bucket keys in accum."""
        accum = AccumStore(self.state)
        mid = parse_local_iso("2026-07-29T10:00:00")
        prev = mid + 29 * 60 + 50  # 10:29:50
        now = mid + 30 * 60 + 10  # 10:30:10
        s = _sess(pcpu=100.0)  # 100% → cpu_seconds = sub_dt * 1.0
        accum.on_sample(prev, now, [s], sample_interval=10.0, write=True)

        sk = "Grok|019fae27-d955-7e71-b0ca-45fe8250e2ee"
        self.assertIn(sk, accum.data.sessions)
        buckets = accum.data.sessions[sk].buckets
        self.assertEqual(len(buckets), 2, list(buckets.keys()))
        b0 = format_local_iso(floor_bucket_start(prev))
        b1 = format_local_iso(floor_bucket_start(now))
        self.assertIn(b0, buckets)
        self.assertIn(b1, buckets)
        # 100% for 10s each → 10 cpu_seconds each
        self.assertAlmostEqual(buckets[b0].cpu_seconds_delta, 10.0, places=3)
        self.assertAlmostEqual(buckets[b1].cpu_seconds_delta, 10.0, places=3)
        # sample_count only on now's bucket
        self.assertEqual(buckets[b0].sample_count_delta, 0)
        self.assertEqual(buckets[b1].sample_count_delta, 1)

        # reload from disk
        accum2 = AccumStore(self.state)
        accum2.load()
        self.assertEqual(len(accum2.data.sessions[sk].buckets), 2)

    def test_c_roll_02_double_flush_no_double_count(self):
        """C-ROLL-02: second rollup after clear does not double-count."""
        accum = AccumStore(self.state)
        hist = HistoryStore(self.state)
        mid = parse_local_iso("2026-07-29T10:00:00")
        prev = mid
        now = mid + 10
        s = _sess(pcpu=50.0)
        accum.on_sample(prev, now, [s], sample_interval=10.0, write=True)
        sk = "Grok|019fae27-d955-7e71-b0ca-45fe8250e2ee"
        cpu_before = list(accum.data.sessions[sk].buckets.values())[0].cpu_seconds_delta

        n1 = rollup_and_clear(accum, hist)
        self.assertGreaterEqual(n1, 1)
        # buckets cleared
        self.assertEqual(len(accum.data.sessions), 0)

        # second rollup empty → no-op / no double count
        n2 = hist.rollup(accum.data)
        self.assertEqual(n2, 0)

        rows = hist.query(hours=24 * 365, limit=20)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cpu_seconds"], cpu_before, places=3)

        # another rollup_and_clear on empty
        n3 = rollup_and_clear(accum, hist)
        self.assertEqual(n3, 0)
        rows2 = hist.query(hours=24 * 365, limit=20)
        self.assertEqual(len(rows2), 1)
        self.assertAlmostEqual(rows2[0]["cpu_seconds"], cpu_before, places=3)
        hist.close()

    def test_c_roll_03_restart_loads_accum(self):
        """C-ROLL-03: restart mid-window loads accum.json deltas."""
        accum = AccumStore(self.state)
        mid = parse_local_iso("2026-07-29T11:00:00")
        prev = mid
        now = mid + 10
        s = _sess(pcpu=20.0, rss_kb=200_000)
        accum.on_sample(prev, now, [s], sample_interval=10.0, write=True)
        sk = "Grok|019fae27-d955-7e71-b0ca-45fe8250e2ee"
        cpu1 = list(accum.data.sessions[sk].buckets.values())[0].cpu_seconds_delta

        # simulate restart
        accum_b = AccumStore(self.state)
        accum_b.load()
        self.assertIn(sk, accum_b.data.sessions)
        self.assertAlmostEqual(
            list(accum_b.data.sessions[sk].buckets.values())[0].cpu_seconds_delta,
            cpu1,
            places=5,
        )
        # continue sampling merges
        accum_b.on_sample(now, now + 10, [s], sample_interval=10.0, write=True)
        cpu2 = list(accum_b.data.sessions[sk].buckets.values())[0].cpu_seconds_delta
        self.assertAlmostEqual(cpu2, cpu1 * 2, places=3)

    def test_c_roll_04_two_unflushed_buckets_upsert(self):
        """C-ROLL-04: crash with two unflushed buckets → both UPSERT after restart."""
        accum = AccumStore(self.state)
        mid = parse_local_iso("2026-07-29T10:00:00")
        prev = mid + 29 * 60 + 50
        now = mid + 30 * 60 + 10
        s = _sess(pcpu=100.0)
        accum.on_sample(prev, now, [s], sample_interval=10.0, write=True)
        sk = "Grok|019fae27-d955-7e71-b0ca-45fe8250e2ee"
        self.assertEqual(len(accum.data.sessions[sk].buckets), 2)

        # crash: do not rollup; restart
        accum2 = AccumStore(self.state)
        accum2.load()
        self.assertEqual(len(accum2.data.sessions[sk].buckets), 2)

        hist = HistoryStore(self.state)
        n = rollup_and_clear(accum2, hist)
        self.assertEqual(n, 2)

        rows = hist.query(hours=24 * 365, limit=20)
        # two bucket rows
        self.assertEqual(len(rows), 2)
        by_bucket = {r["bucket_start"]: r for r in rows}
        b0 = format_local_iso(floor_bucket_start(prev))
        b1 = format_local_iso(floor_bucket_start(now))
        self.assertIn(b0, by_bucket)
        self.assertIn(b1, by_bucket)
        self.assertAlmostEqual(by_bucket[b0]["cpu_seconds"], 10.0, places=3)
        self.assertAlmostEqual(by_bucket[b1]["cpu_seconds"], 10.0, places=3)

        # double rollup after clear must not increase
        n2 = rollup_and_clear(accum2, hist)
        self.assertEqual(n2, 0)
        rows2 = hist.query(hours=24 * 365, limit=20)
        self.assertEqual(len(rows2), 2)
        for r in rows2:
            self.assertAlmostEqual(r["cpu_seconds"], 10.0, places=3)
        hist.close()


class TestLiveWrite(unittest.TestCase):
    def test_live_atomic(self):
        from local_ai_monitor.store import LiveStore

        tmp = tempfile.mkdtemp(prefix="local-ai-monitor-live-")
        try:
            live = LiveStore(tmp)
            payload = live.write(
                [_sess()],
                collector_pid=12345,
                sample_interval_s=10,
            )
            self.assertEqual(payload["version"], 2)
            self.assertEqual(payload["collector_pid"], 12345)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "live.json")))
            again = live.read()
            self.assertIsNotNone(again)
            self.assertEqual(again["totals"]["nsessions"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
