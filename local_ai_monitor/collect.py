"""Headless collector daemon — samples sessions → live/accum/SQLite.

  local-ai-monitor collect [--interval 10] [--rollup 1800] [--threads] [--once] [--self-stats]

**Status (2026-07-30): NOT always-on.** Kept for historical / on-demand use
(``--once``, expert TUI, sparks/history rebuild). Production lean stack uses
``local-ai-monitord`` → ``live.min.json`` (~KB feed, ~3–4 MB RSS). Do not re-enable
``com.user.local-ai-monitor.collect`` KeepAlive without a mass budget review.

Single-instance flock on ~/.local/state/local-ai-monitor/collect.lock.
Never logs cmd/env. Python 3.9+.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import resource
import signal
import sys
import time
from typing import Any, Dict, List, Optional, TextIO

from local_ai_monitor.collect_basic import collect_sessions
from local_ai_monitor.config import ensure_state_dir, load_config, state_dir, write_default_config
from local_ai_monitor.sessionize import SessionStats, Sessionizer
from local_ai_monitor.store import (
    AccumStore,
    HistoryStore,
    LiveStore,
    collect_lock_path,
    format_local_iso,
    rollup_and_clear,
)


class CollectLock:
    """fcntl exclusive lock — single collector instance."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh: Optional[TextIO] = None

    def acquire(self, blocking: bool = False) -> bool:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._fh.fileno(), flags)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def _self_rss_kb() -> int:
    # ru_maxrss on macOS is bytes
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = int(usage.ru_maxrss)
        # Linux is KB; macOS is bytes. Heuristic: if huge, treat as bytes.
        if rss > 10_000_000:  # > ~10MB if already KB would be absurd for us
            return rss // 1024
        # On macOS ru_maxrss is bytes
        if sys.platform == "darwin":
            return max(1, rss // 1024)
        return rss
    except Exception:
        return 0


def _nprocs_approx() -> int:
    try:
        import subprocess

        out = subprocess.check_output(
            ["ps", "-axo", "pid="],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
        )
        return sum(1 for line in out.splitlines() if line.strip())
    except Exception:
        return 0


def run_sample(
    *,
    include_threads: bool,
    sessionizer: Optional[Sessionizer] = None,
    skip_lsof: bool = False,
) -> tuple:
    """Return (sessions, sample_ms, nprocs_scanned)."""
    t0 = time.perf_counter()
    sessions = collect_sessions(
        include_threads=include_threads,
        sessionizer=sessionizer,
        skip_lsof=skip_lsof,
    )
    ms = (time.perf_counter() - t0) * 1000.0
    nprocs = sum(s.nproc for s in sessions)
    return sessions, ms, nprocs


def cmd_collect(argv: Optional[List[str]] = None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(prog="local-ai-monitor collect", description="Headless AI resource collector")
    p.add_argument(
        "--interval",
        "-i",
        type=float,
        default=float(cfg.get("sample_interval_s", 10)),
        help="sample interval seconds (default 10; design 5–15)",
    )
    p.add_argument(
        "--rollup",
        type=float,
        default=float(cfg.get("rollup_interval_s", 1800)),
        help="rollup interval seconds (default 1800)",
    )
    p.add_argument(
        "--threads",
        action="store_true",
        help="sample thread counts (ps -M; off by default in collector)",
    )
    p.add_argument("--once", action="store_true", help="single sample then exit")
    p.add_argument(
        "--self-stats",
        action="store_true",
        help="print one-line JSON self stats to stdout",
    )
    p.add_argument(
        "--state-dir",
        default=None,
        help="override state dir (default ~/.local/state/local-ai-monitor or LOCAL_AI_MONITOR_STATE)",
    )
    p.add_argument(
        "--no-lsof",
        action="store_true",
        help="skip lsof (tests / constrained env)",
    )
    args = p.parse_args(argv)

    interval = max(5.0, min(15.0, float(args.interval))) if not args.once else float(args.interval)
    if args.once and args.interval:
        interval = float(args.interval)
    # allow --once with any interval for testing; clamp only for daemon mode
    if not args.once:
        interval = max(5.0, min(60.0, float(args.interval)))
    rollup_every = max(60.0, float(args.rollup))
    retention = int(cfg.get("retention_days", 14))
    state = args.state_dir
    ensure_state_dir(state)
    write_default_config()  # ensure config exists (best-effort)

    lock = CollectLock(collect_lock_path(state))
    if not lock.acquire(blocking=False):
        print("error: another collector holds collect.lock", file=sys.stderr)
        return 2

    live = LiveStore(state)
    accum = AccumStore(state)
    history = HistoryStore(state)
    accum.load()
    history.connect()

    sessionizer = Sessionizer()
    stop = {"flag": False}

    def _handle_sig(signum: int, frame: Any) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    prev_ts: Optional[float] = None
    # resume last_sample_ts if present
    if accum.data.last_sample_ts:
        prev = None
        try:
            from local_ai_monitor.store import parse_local_iso

            prev = parse_local_iso(accum.data.last_sample_ts)
        except Exception:
            prev = None
        if prev and prev > 0:
            prev_ts = prev

    last_rollup_wall = time.time()
    include_threads = bool(args.threads)
    exit_code = 0

    paused_path = os.path.join(state_dir(), "paused")

    try:
        while True:
            # Soft pause from menu bar (file flag) — do not sample until cleared
            if os.path.isfile(paused_path):
                if stop["flag"]:
                    break
                time.sleep(min(2.0, interval))
                continue

            t_wall0 = time.time()
            sessions, sample_ms, n_sess_procs = run_sample(
                include_threads=include_threads,
                sessionizer=sessionizer,
                skip_lsof=args.no_lsof,
            )
            now = time.time()
            if prev_ts is None:
                # first sample: establish baseline without inventing a full interval of CPU
                # still write live; accumulate with sample_interval nominal only if restart
                # Design: first sample after start uses sample_interval as dt clamp base
                prev_ts = now - interval

            accum.on_sample(
                prev_ts,
                now,
                sessions,
                sample_interval=interval,
                write=True,
            )
            prev_ts = now

            self_stats = {
                "sample_ms": round(sample_ms, 2),
                "rss_kb": _self_rss_kb(),
                "nprocs_scanned": _nprocs_approx(),
                "sessions": len([s for s in sessions if s.nproc > 0]),
                "lsof_calls": getattr(sessionizer, "sample_n", 0),
            }
            debug = {
                "last_sample_ms": round(sample_ms, 2),
                "sessions_unkeyed": 0,
                "lsof_cache_size": len(sessionizer.uuid_cache._by_pid),
                "lsof_errors": 0,
                "self_rss_kb": self_stats["rss_kb"],
            }
            live.write(
                sessions,
                collector_pid=os.getpid(),
                sample_interval_s=interval,
                ts=now,
                debug=debug,
            )

            # Quarantine enforcement only if tools are listed (never invent kills).
            # Disabled from ending anything when quarantine list is empty.
            try:
                from local_ai_monitor.quarantine import list_quarantined, enforce_quarantine

                if list_quarantined():
                    enforce_quarantine()
            except Exception:
                pass

            if args.self_stats:
                print(json.dumps(self_stats, separators=(",", ":")))
                sys.stdout.flush()

            # rollup on schedule or on exit path
            if (now - last_rollup_wall) >= rollup_every:
                rollup_and_clear(accum, history, retention_days=retention)
                last_rollup_wall = now

            if args.once or stop["flag"]:
                break

            elapsed = time.time() - t_wall0
            sleep_for = max(0.1, interval - elapsed)
            # interruptible sleep
            end = time.time() + sleep_for
            while time.time() < end and not stop["flag"]:
                time.sleep(min(0.5, end - time.time()))
            if stop["flag"]:
                break
    except Exception as exc:
        print(f"error: collector failed: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        # graceful shutdown flush
        try:
            rollup_and_clear(accum, history, retention_days=retention)
        except Exception:
            pass
        try:
            history.close()
        except Exception:
            pass
        lock.release()

    return exit_code


def cmd_history(argv: Optional[List[str]] = None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(prog="local-ai-monitor history", description="Print SQLite 30m rollups")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--app", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv)

    hist = HistoryStore(args.state_dir)
    if not os.path.isfile(hist.db_path):
        print("no history.db yet (run: local-ai-monitor collect --once)", file=sys.stderr)
        return 1
    rows = hist.query(hours=args.hours, app=args.app, limit=args.limit)
    app_summary = hist.summarize_by_app(hours=args.hours)
    hist.close()

    if args.json:
        print(json.dumps({"summary": app_summary, "rows": rows}, indent=2))
        return 0
    if not rows and not app_summary:
        print("(no rollup rows in range)")
        return 0
    if app_summary:
        print(f"=== app summary (last {args.hours:g}h) ===")
        print(f"{'app':<16} {'cpu_s':>10} {'peak_mb':>10} {'buckets':>8}")
        for s in app_summary:
            peak_mb = (s.get("peak_rss_kb") or 0) / 1024.0
            print(
                f"{str(s.get('app', '')):<16} "
                f"{float(s.get('cpu_seconds') or 0):10.1f} "
                f"{peak_mb:10.1f} "
                f"{int(s.get('buckets') or 0):8d}"
            )
        print()
    print(
        f"{'bucket_start':<20} {'app':<14} {'label/session':<36} {'cpu_s':>8} {'peak_mb':>8} {'samples':>7}"
    )
    print("-" * 100)
    for r in rows:
        peak_mb = (r.get("peak_rss_kb") or 0) / 1024.0
        label = (r.get("label") or "").strip()
        sid = str(r.get("session_id") or "")
        display = label if label else sid
        if len(display) > 36:
            display = display[:33] + "..."
        print(
            f"{r.get('bucket_start', ''):<20} "
            f"{str(r.get('app', '')):<14} "
            f"{display:<36} "
            f"{float(r.get('cpu_seconds') or 0):8.1f} "
            f"{peak_mb:8.1f} "
            f"{int(r.get('sample_count') or 0):7d}"
        )
    return 0


def cmd_status(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="local-ai-monitor status", description="Collector / store health")
    p.add_argument("--json", action="store_true")
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv)

    st = state_dir(args.state_dir)
    live_p = os.path.join(st, "live.json")
    accum_p = os.path.join(st, "accum.json")
    db_p = os.path.join(st, "history.db")
    lock_p = os.path.join(st, "collect.lock")

    live_store = LiveStore(args.state_dir)
    live = live_store.read() or {}
    now = time.time()

    collector_pid = live.get("collector_pid")
    alive = False
    if collector_pid:
        try:
            os.kill(int(collector_pid), 0)
            alive = True
        except (OSError, ValueError):
            alive = False

    # lock holder
    lock_pid = None
    if os.path.isfile(lock_p):
        try:
            with open(lock_p, "r", encoding="utf-8") as f:
                line = f.readline().strip()
                if line.isdigit():
                    lock_pid = int(line)
        except OSError:
            pass

    sample_age = None
    ts_raw = live.get("ts")
    if ts_raw:
        from local_ai_monitor.store import parse_local_iso

        ts = parse_local_iso(str(ts_raw))
        if ts > 0:
            sample_age = now - ts

    tool_rss = None
    if isinstance(live.get("debug"), dict):
        tool_rss = live["debug"].get("self_rss_kb")

    db_size = 0
    if os.path.isfile(db_p):
        try:
            db_size = os.path.getsize(db_p)
        except OSError:
            pass

    # LaunchAgents
    launchd_loaded = False
    launchd_python = None
    menubar_loaded = False
    mb_pid = None
    try:
        from local_ai_monitor.install_svc import (
            agent_python_from_plist,
            launchctl_print_collect,
            launchctl_print_menubar,
            menubar_pid,
        )

        launchd_python = agent_python_from_plist()
        launchd_loaded = launchctl_print_collect() is not None
        menubar_loaded = launchctl_print_menubar() is not None
        mb_pid = menubar_pid()
    except Exception:
        pass

    info: Dict[str, Any] = {
        "state_dir": st,
        "collector_pid": collector_pid,
        "collector_alive": alive,
        "lock_pid": lock_pid,
        "last_sample_ts": ts_raw,
        "sample_age_s": round(sample_age, 1) if sample_age is not None else None,
        "sample_interval_s": live.get("sample_interval_s"),
        "totals": live.get("totals"),
        "top": live.get("top"),
        "debug": live.get("debug"),
        "tool_rss_kb": tool_rss,
        "db_size_bytes": db_size,
        "live_exists": os.path.isfile(live_p),
        "accum_exists": os.path.isfile(accum_p),
        "python": sys.executable,
        "launchd_label": "com.user.local-ai-monitor.collect",
        "launchd_loaded": launchd_loaded,
        "launchd_python": launchd_python,
        "menubar_label": "com.user.local-ai-monitor.menubar",
        "menubar_loaded": menubar_loaded,
        "menubar_pid": mb_pid,
    }

    if args.json:
        print(json.dumps(info, indent=2))
        return 0

    print("local-ai-monitor status")
    print(f"  state_dir:       {st}")
    print(f"  collector_pid:   {collector_pid}  alive={alive}")
    print(f"  lock_pid:        {lock_pid}")
    print(f"  last_sample:     {ts_raw}  age_s={info['sample_age_s']}")
    print(f"  interval_s:      {live.get('sample_interval_s')}")
    print(f"  tool_rss_kb:     {tool_rss}")
    print(f"  db_size_bytes:   {db_size}")
    print(f"  python:          {sys.executable}")
    print(f"  launchd:         com.user.local-ai-monitor.collect  loaded={launchd_loaded}")
    print(f"  launchd_python:  {launchd_python}")
    print(f"  menubar:         com.user.local-ai-monitor.menubar  loaded={menubar_loaded}  pid={mb_pid}")
    # Background session log (CCM) — expert status only, not menu bar
    try:
        from local_ai_monitor.ccm_observer import ccm_status_meta, format_status_line

        ccm = live.get("ccm") or ccm_status_meta()
        print(f"  session_log:     {format_status_line(ccm if isinstance(ccm, dict) else None)}")
    except Exception:
        pass
    if live.get("totals"):
        t = live["totals"]
        print(
            f"  totals:          cpu={t.get('cpu_pct')}%  "
            f"rss_kb={t.get('rss_kb')}  nproc={t.get('nproc')}  "
            f"nsessions={t.get('nsessions')}"
        )
    if live.get("top"):
        top = live["top"]
        print(
            f"  top:             {top.get('app')} {top.get('label')} "
            f"cpu={top.get('cpu_pct')}%"
        )
    if live.get("debug"):
        print(f"  debug:           {live['debug']}")
    return 0
