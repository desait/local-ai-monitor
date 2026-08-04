"""CLI: list / hide / show discovered AI tools (user view prefs).

Prefs live in ~/.config/local-ai-monitor/tools.json. Discovery is catalog-driven —
new products are registered once in catalog.KNOWN_TOOLS.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from local_ai_monitor.catalog import (
    KNOWN_TOOLS,
    PREFS_PATH,
    discover_installed,
    display_name,
    load_tool_prefs,
    save_tool_prefs,
    set_hidden,
    visible_tool_ids,
)


def _print_list(*, as_json: bool = False) -> int:
    installed = discover_installed()
    prefs = load_tool_prefs()
    visible = visible_tool_ids(installed=installed, running=[], prefs=prefs)
    rows = []
    for t in KNOWN_TOOLS:
        inst = t.id in installed
        hid = t.id in prefs.hidden
        vis = t.id in visible
        rows.append(
            {
                "id": t.id,
                "name": t.display,
                "installed": inst,
                "hidden": hid,
                "visible": vis,
            }
        )
    if as_json:
        print(
            json.dumps(
                {
                    "prefs_path": PREFS_PATH,
                    "installed": installed,
                    "hidden": sorted(prefs.hidden),
                    "show_idle_installed": prefs.show_idle_installed,
                    "tools": rows,
                },
                indent=2,
            )
        )
        return 0

    print()
    print("  AI tools on this Mac (catalog)")
    print(f"  Prefs: {PREFS_PATH}")
    print()
    print(f"  {'Tool':<18}  {'Installed':<10}  {'In view':<10}  Notes")
    print(f"  {'─' * 18}  {'─' * 10}  {'─' * 10}  {'─' * 20}")
    for r in rows:
        inst = "yes" if r["installed"] else "—"
        if r["hidden"]:
            view = "hidden"
            note = "local-ai-monitor tools show " + r["id"]
        elif r["visible"]:
            view = "shown"
            note = ""
        elif r["installed"]:
            view = "off"
            note = "idle display off" if not prefs.show_idle_installed else ""
        else:
            view = "—"
            note = "not installed"
        print(f"  {r['name']:<18}  {inst:<10}  {view:<10}  {note}")
    print()
    print("  Hide a tool:   local-ai-monitor tools hide <id>")
    print("  Show a tool:   local-ai-monitor tools show <id>")
    print("  Idle rows:     local-ai-monitor tools idle on|off")
    print("  Ids:", ", ".join(t.id for t in KNOWN_TOOLS))
    print()
    return 0


def _resolve_id(name: str) -> Optional[str]:
    raw = (name or "").strip()
    if not raw:
        return None
    # exact id
    for t in KNOWN_TOOLS:
        if t.id == raw or t.display.casefold() == raw.casefold():
            return t.id
    # fuzzy: codex, openai, claude app, etc.
    key = raw.casefold().replace("_", " ").replace("-", " ")
    for t in KNOWN_TOOLS:
        if key in t.id.casefold() or key in t.display.casefold():
            return t.id
    return None


def cmd_tools(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or [])
    p = argparse.ArgumentParser(
        prog="local-ai-monitor tools",
        description="List installed AI tools and edit which ones appear in the view",
    )
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "hide", "show", "idle", "path"],
        help="list | hide | show | idle | path",
    )
    p.add_argument("target", nargs="?", help="tool id (for hide/show) or on|off (idle)")
    p.add_argument("--json", action="store_true", help="JSON output for list")
    args = p.parse_args(argv)

    if args.action in ("list", None):
        return _print_list(as_json=args.json)

    if args.action == "path":
        print(PREFS_PATH)
        return 0

    if args.action == "idle":
        on = (args.target or "").casefold()
        if on not in ("on", "off", "1", "0", "true", "false", "yes", "no"):
            print("usage: local-ai-monitor tools idle on|off", file=sys.stderr)
            return 2
        prefs = load_tool_prefs()
        prefs.show_idle_installed = on in ("on", "1", "true", "yes")
        path = save_tool_prefs(prefs)
        print(
            f"show_idle_installed={prefs.show_idle_installed}  wrote {path}"
        )
        return 0

    if args.action in ("hide", "show"):
        if not args.target:
            print(f"usage: local-ai-monitor tools {args.action} <tool-id>", file=sys.stderr)
            return 2
        tid = _resolve_id(args.target)
        if not tid:
            print(f"unknown tool: {args.target!r}", file=sys.stderr)
            print("known:", ", ".join(t.id for t in KNOWN_TOOLS), file=sys.stderr)
            return 2
        prefs = set_hidden(tid, hidden=(args.action == "hide"))
        state = "hidden" if tid in prefs.hidden else "shown"
        print(f"{display_name(tid)} ({tid}) → {state}")
        print(f"prefs: {PREFS_PATH}")
        return 0

    return 2
