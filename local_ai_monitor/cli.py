"""CLI — menu-bar first for non-technical users; expert modes retained."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from local_ai_monitor.tui import live

_HEADLESS = frozenset(
    {
        "collect",
        "history",
        "status",
        "install",
        "uninstall",
        "tools",
        "launch",
        "ccm",
        "end-session",
        "kill",
        "resource",
        "rm",
        "quarantine",
        "parking-lot",
        "browser-advice",
        "checkpoint",
        "runway",
    }
)
_ALL_MODES = (
    "live",
    "once",
    "tui",
    "simple",
    "dash",
    "expert",
    "collect",
    "history",
    "status",
    "install",
    "uninstall",
    "tools",
    "launch",
    "ccm",
    "end-session",
    "kill",
    "resource",
    "rm",
    "quarantine",
    "parking-lot",
    "browser-advice",
    "checkpoint",
    "runway",
)


def _run_tui(
    interval: float,
    *,
    once: bool = False,
    as_json: bool = False,
    view: str = "session",
    threads: bool = False,
    no_threads: bool = False,
) -> int:
    return live(
        interval,
        once=once,
        as_json=as_json,
        no_threads=no_threads,
        view=view,
        threads=threads,
    )


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in _HEADLESS:
        cmd = argv[0]
        rest = argv[1:]
        if cmd == "collect":
            from local_ai_monitor.collect import cmd_collect

            return cmd_collect(rest)
        if cmd == "history":
            from local_ai_monitor.collect import cmd_history

            return cmd_history(rest)
        if cmd == "status":
            from local_ai_monitor.collect import cmd_status

            return cmd_status(rest)
        if cmd == "install":
            from local_ai_monitor.install_svc import cmd_install

            return cmd_install(rest)
        if cmd == "uninstall":
            from local_ai_monitor.install_svc import cmd_uninstall

            return cmd_uninstall(rest)
        if cmd == "tools":
            from local_ai_monitor.tools_cmd import cmd_tools

            return cmd_tools(rest)
        if cmd == "launch":
            from local_ai_monitor.launch import cmd_launch

            return cmd_launch(rest)
        if cmd == "ccm":
            from local_ai_monitor.ccm_observer import cmd_ccm

            return cmd_ccm(rest)
        if cmd in ("end-session", "kill"):
            from local_ai_monitor.end_session import cmd_end_session

            return cmd_end_session(rest)
        if cmd in ("resource", "rm"):
            from local_ai_monitor.resource.cli import cmd_resource

            return cmd_resource(rest)
        if cmd == "quarantine":
            from local_ai_monitor.quarantine import cmd_quarantine

            return cmd_quarantine(rest)
        if cmd == "parking-lot":
            from local_ai_monitor.parking_lot import cmd_parking_lot

            return cmd_parking_lot(rest)
        if cmd == "browser-advice":
            from local_ai_monitor.browser_advice import cmd_browser_advice

            return cmd_browser_advice(rest)
        if cmd == "checkpoint":
            from local_ai_monitor.checkpoint import cmd_checkpoint

            return cmd_checkpoint(rest)
        if cmd == "runway":
            from local_ai_monitor.runway.cli import cmd_runway

            return cmd_runway(rest)

    p = argparse.ArgumentParser(
        description="Runway — see whether this Mac has room for more AI work",
        epilog=(
            "Everyday use: look at Runway in your Mac menu bar "
            "(after `local-ai-monitor install`).\n"
            "  local-ai-monitor              short reminder + menu bar tip (no tmux)\n"
            "  local-ai-monitor runway       same reminder (product name)\n"
            "  local-ai-monitor simple       friendly full list in Terminal (press Q)\n"
            "  local-ai-monitor tools        list installed AI tools; hide/show in view\n"
            "  local-ai-monitor launch Grok  start a new session (Terminal/app)\n"
            "  local-ai-monitor resource     physics-first RAM policy (alias: local-ai-rm)\n"
            "  local-ai-monitor quarantine   keep unused background tools off (OpenClaw)\n"
            "  local-ai-monitor parking-lot  lower priority of unused background helpers\n"
            "  local-ai-monitor browser-advice recommend inactive browser tabs/windows\n"
            "  local-ai-monitor checkpoint    show per-tool checkpoint/continue advice\n"
            "  local-ai-monitor tui          expert table view\n"
            "  local-ai-monitor dash|expert  expert split view (tmux + Glances)\n"
            "  local-ai-monitor install      start background monitor + menu bar\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "mode",
        nargs="?",
        default=None,
        choices=list(_ALL_MODES),
        help="omit = home message; simple = friendly list; tui/dash = expert",
    )
    p.add_argument("--interval", "-i", type=float, default=2.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--threads", action="store_true")
    p.add_argument("--no-threads", action="store_true")
    p.add_argument("--view", choices=["app", "session"], default="session")
    p.add_argument("--recreate", action="store_true")
    args, unknown = p.parse_known_args(argv)

    if args.mode in _HEADLESS:
        rest = unknown
        if args.mode == "collect":
            from local_ai_monitor.collect import cmd_collect

            return cmd_collect(argv[1:] if argv and argv[0] == "collect" else rest)
        if args.mode == "history":
            from local_ai_monitor.collect import cmd_history

            return cmd_history(argv[1:] if argv and argv[0] == "history" else rest)
        if args.mode == "status":
            from local_ai_monitor.collect import cmd_status

            return cmd_status(argv[1:] if argv and argv[0] == "status" else rest)
        if args.mode == "install":
            from local_ai_monitor.install_svc import cmd_install

            return cmd_install(argv[1:] if argv and argv[0] == "install" else rest)
        if args.mode == "uninstall":
            from local_ai_monitor.install_svc import cmd_uninstall

            return cmd_uninstall(argv[1:] if argv and argv[0] == "uninstall" else rest)
        if args.mode == "tools":
            from local_ai_monitor.tools_cmd import cmd_tools

            return cmd_tools(argv[1:] if argv and argv[0] == "tools" else rest)
        if args.mode == "launch":
            from local_ai_monitor.launch import cmd_launch

            return cmd_launch(argv[1:] if argv and argv[0] == "launch" else rest)
        if args.mode == "ccm":
            from local_ai_monitor.ccm_observer import cmd_ccm

            return cmd_ccm(argv[1:] if argv and argv[0] == "ccm" else rest)
        if args.mode in ("resource", "rm"):
            from local_ai_monitor.resource.cli import cmd_resource

            return cmd_resource(argv[1:] if argv and argv[0] in ("resource", "rm") else rest)
        if args.mode == "quarantine":
            from local_ai_monitor.quarantine import cmd_quarantine

            return cmd_quarantine(argv[1:] if argv and argv[0] == "quarantine" else rest)

    if args.mode == "once" or args.json:
        return _run_tui(
            args.interval,
            once=True,
            as_json=args.json,
            view=args.view,
            threads=args.threads,
            no_threads=args.no_threads,
        )

    if args.mode == "simple":
        from local_ai_monitor.simple import run_simple

        return run_simple(once=False, interval=args.interval)

    if args.mode in ("tui", "live"):
        return _run_tui(
            args.interval,
            view=args.view,
            threads=args.threads,
            no_threads=args.no_threads,
        )

    if args.mode in ("dash", "expert"):
        from local_ai_monitor.mux import run_dash

        return run_dash(
            interval=args.interval,
            view=args.view,
            threads=args.threads,
            recreate=args.recreate,
            attach=True,
        )

    # Bare: non-technical home message — NEVER open tmux
    from local_ai_monitor.simple import print_home_message

    return print_home_message()
