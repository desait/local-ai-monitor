"""Friendly Terminal list for non-technical users — no tmux, minimal keys."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
from typing import Any, Dict, Optional

from local_ai_monitor.humanize import summarize_live
from local_ai_monitor.store import LiveStore, parse_local_iso


def _is_stale(live: Dict[str, Any]) -> bool:
    ts = parse_local_iso(str(live.get("ts") or ""))
    if not ts:
        return True
    interval = float(live.get("sample_interval_s") or 10)
    return (time.time() - ts) > 3 * interval


def _load_summary() -> Dict[str, Any]:
    live = LiveStore().read()
    if not live:
        # Fall back to one-shot collect path
        try:
            from local_ai_monitor.collect_basic import collect_sessions
            from local_ai_monitor.store import build_live_payload

            sessions = collect_sessions(include_threads=False)
            live = build_live_payload(
                sessions,
                collector_pid=os.getpid(),
                sample_interval_s=10.0,
            )
            return summarize_live(live, stale=False)
        except Exception:
            return summarize_live({}, stale=True)
    return summarize_live(live, stale=_is_stale(live))


def render_simple(summary: Dict[str, Any]) -> str:
    lines = []
    lines.append("")
    lines.append("  AI tools on this Mac")
    lines.append("  ────────────────────")
    lines.append("")
    lines.append(f"  {summary.get('sentence', '')}")
    mem = summary.get("memory_line") or ""
    if mem:
        lines.append(f"  Using {mem}.")
    lines.append("")

    tools = summary.get("tools") or []
    if not tools and summary.get("worry") != "stale":
        lines.append("  No AI tools are using noticeable resources right now.")
    elif summary.get("worry") == "stale":
        lines.append("  Background monitor is not updating.")
        lines.append("  Try: local-ai-monitor install")
    else:
        lines.append("  By tool")
        for t in tools:
            lines.append(
                f"    · {t['name']:<14}  {t['load']:<10}  {t['mem']}"
            )

    buzz = summary.get("buzz_projects") or []
    if buzz:
        lines.append("")
        lines.append("  Buzz projects")
        for g in buzz:
            lines.append(f"    {g['project']}")
            for w in g.get("workers") or []:
                lines.append(f"      └ {w['name']:<20}  {w['load']:<10}  {w['mem']}")

    lines.append("")
    lines.append("  Press Q to close")
    lines.append("  The menu bar icon (AI) keeps watching in the background.")
    lines.append("")
    return "\n".join(lines)


def run_simple(*, once: bool = False, interval: float = 2.0) -> int:
    stop = False

    def _sig(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    use_keys = sys.stdin.isatty() and not once
    old = None
    if use_keys:
        try:
            import termios
            import tty

            old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            use_keys = False
            old = None

    try:
        while not stop:
            text = render_simple(_load_summary())
            if once:
                print(text)
                return 0
            sys.stdout.write("\033[H\033[2J")
            print(text)
            sys.stdout.flush()
            end = time.time() + max(interval, 0.5)
            while time.time() < end and not stop:
                if use_keys:
                    r, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch in ("q", "Q", "\x03"):
                            stop = True
                            break
                        if ch in ("r", "R"):
                            break
                else:
                    time.sleep(0.2)
    finally:
        if old is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        if not once:
            print()
    return 0


def print_home_message() -> int:
    """Bare `local-ai-monitor`: guide non-tech users to the menu bar."""
    summary = _load_summary()
    print()
    print("  Local AI Monitor is watching in your menu bar.")
    print(f"  Look for:  {summary.get('chip', 'AI · …')}")
    print()
    print(f"  {summary.get('sentence', '')}")
    if summary.get("memory_line"):
        print(f"  Using {summary['memory_line']}.")
    print()
    print("  Click that menu bar item for a simple list and actions.")
    print("  Friendly full list:  local-ai-monitor simple")
    print("  Expert Terminal:     local-ai-monitor tui   or   local-ai-monitor dash")
    print()
    return 0
