"""Everyday Runway CLI — no tmux, no expert jargon.

  runway
  runway status | watch | suggest | pause | json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from local_ai_monitor.runway.actions import pause_idle
from local_ai_monitor.runway.compose import compose_card
from local_ai_monitor.runway.copy import home_lines
from local_ai_monitor.runway.model import brand


def _print_card(card, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(card.to_dict(), indent=2))
        return
    print(home_lines(card), end="")


def cmd_runway(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(
        prog=brand().lower(),
        description=(
            f"{brand()} — see whether this Mac has room for more work. "
            "No Terminal knowledge required. Active work is never closed for you."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Everyday use: look at the menu bar icon.\n"
            f"  {brand().lower()}              short reminder (never opens tmux)\n"
            f"  {brand().lower()} watch        friendly live list (press Q)\n"
            f"  {brand().lower()} suggest      one next step\n"
            f"  {brand().lower()} pause --yes  pause unused background work only\n"
        ),
    )
    p.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["status", "watch", "suggest", "pause", "json"],
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--yes", action="store_true", help="Required for pause")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--interval", "-i", type=float, default=2.0)
    args = p.parse_args(argv)

    if args.command == "watch":
        from local_ai_monitor.simple import run_simple

        return run_simple(once=False, interval=args.interval)

    if args.command == "json":
        card = compose_card()
        print(json.dumps(card.to_dict(), indent=2))
        return 0

    card = compose_card()

    if args.command == "status":
        if args.json:
            print(json.dumps(card.to_dict(), indent=2))
        else:
            print(home_lines(card), end="")
        return 0

    if args.command == "suggest":
        if args.json:
            print(
                json.dumps(
                    {
                        "state": card.state,
                        "action": card.action,
                        "can_start": card.can_start,
                        "next_step": card.next_step,
                        "candidate_label": card.candidate_label,
                    },
                    indent=2,
                )
            )
        else:
            print(card.next_step)
            print(card.promise)
        if card.action == "pause_idle":
            return 0
        if card.state in ("hold", "protect") and card.action != "pause_idle":
            return 2
        return 0

    if args.command == "pause":
        result = pause_idle(yes=args.yes, dry_run=args.dry_run, card=card)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if result.get("error"):
                print(result["error"], file=sys.stderr)
                if result.get("candidate_label"):
                    print(f"would pause: {result['candidate_label']}", file=sys.stderr)
            else:
                print(result.get("message") or result.get("skipped") or "ok")
        if result.get("error"):
            return 2
        return 0 if result.get("ok", True) else 1

    p.error(f"unknown command {args.command}")
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    return cmd_runway(argv)


if __name__ == "__main__":
    raise SystemExit(main())
