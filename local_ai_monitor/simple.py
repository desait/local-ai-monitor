"""Friendly Terminal list for non-technical users — no tmux, minimal keys.

Renders the Runway card. Expert tables live in tui / dash.
"""

from __future__ import annotations

import select
import signal
import sys
import time

from local_ai_monitor.runway.compose import compose_card
from local_ai_monitor.runway.copy import home_lines, watch_lines


def render_simple(card=None) -> str:
    return watch_lines(card if card is not None else compose_card())


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
            text = render_simple()
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
    """Bare `local-ai-monitor` / `runway`: guide people to the menu bar."""
    print(home_lines(compose_card()), end="")
    return 0
