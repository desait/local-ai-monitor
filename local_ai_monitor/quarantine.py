"""Quarantine AI tools so they stay off until you allow them again.

Use case: OpenClaw LaunchAgent KeepAlive hogs RAM for days while unused.
Quarantine = bootout LaunchAgent + end processes + remember preference.
Collector enforces quarantine each sample if the tool comes back.
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional

from local_ai_monitor.config import CONFIG_DIR

QUARANTINE_PATH = os.path.join(CONFIG_DIR, "quarantine.json")

DEFAULT: Dict[str, Any] = {
    "tools": [],  # tool ids e.g. "OpenClaw"
    "since": {},  # tool_id -> iso timestamp
    "user_unquarantined": [],  # tools user explicitly allowed (never re-seed)
}

# Seeded before first glance when the tool is installed / known.
# User can unquarantine; we remember and do not re-add.
DEFAULT_SEED_TOOLS = ("OpenClaw",)


def quarantine_path(override: Optional[str] = None) -> str:
    return os.path.expanduser(
        override or os.environ.get("LOCAL_AI_MONITOR_QUARANTINE") or QUARANTINE_PATH
    )


def load_quarantine(path: Optional[str] = None) -> Dict[str, Any]:
    p = quarantine_path(path)
    if not os.path.isfile(p):
        return deepcopy(DEFAULT)
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return deepcopy(DEFAULT)
        out = deepcopy(DEFAULT)
        tools = raw.get("tools") or []
        if isinstance(tools, list):
            out["tools"] = [str(t) for t in tools]
        since = raw.get("since") or {}
        if isinstance(since, dict):
            out["since"] = {str(k): str(v) for k, v in since.items()}
        uu = raw.get("user_unquarantined") or []
        if isinstance(uu, list):
            out["user_unquarantined"] = [str(t) for t in uu]
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return deepcopy(DEFAULT)


def ensure_default_quarantine(path: Optional[str] = None) -> Dict[str, Any]:
    """Pre-seed quarantine (OpenClaw etc.) before the user opens the menu.

    Does not re-add tools the user explicitly allowed again.
    """
    p = quarantine_path(path)
    data = load_quarantine(path)
    blocked = set(data.get("user_unquarantined") or [])
    tools = set(data.get("tools") or [])
    since = dict(data.get("since") or {})
    changed = False
    for t in DEFAULT_SEED_TOOLS:
        if t in blocked:
            continue
        if t not in tools:
            tools.add(t)
            since[t] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            changed = True
    if changed or not os.path.isfile(p):
        data["tools"] = sorted(tools)
        data["since"] = since
        save_quarantine(data, path)
    return data


def save_quarantine(data: Dict[str, Any], path: Optional[str] = None) -> str:
    p = quarantine_path(path)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    payload = {
        "tools": sorted(set(str(t) for t in (data.get("tools") or []))),
        "since": data.get("since") or {},
        "user_unquarantined": sorted(
            set(str(t) for t in (data.get("user_unquarantined") or []))
        ),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def is_quarantined(tool_id: str, path: Optional[str] = None) -> bool:
    return tool_id in set(load_quarantine(path).get("tools") or [])


def list_quarantined(path: Optional[str] = None) -> List[str]:
    return list(load_quarantine(path).get("tools") or [])


def quarantine_tool(tool_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Mark tool quarantined and stop it now (LaunchAgent bootout + kill)."""
    if not tool_id:
        return {"ok": False, "error": "tool id required"}
    data = load_quarantine(path)
    tools = set(data.get("tools") or [])
    tools.add(tool_id)
    data["tools"] = sorted(tools)
    since = dict(data.get("since") or {})
    since[tool_id] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data["since"] = since
    save_quarantine(data, path)

    from local_ai_monitor.end_session import end_tool, stop_launchd_for_app

    launchd = stop_launchd_for_app(tool_id, None, dry_run=False)
    ended = end_tool(tool_id, dry_run=False, refresh_live=True)
    return {
        "ok": True,
        "tool": tool_id,
        "quarantined": True,
        "launchd": launchd,
        "end": ended,
        "message": (
            f"{tool_id} quarantined — stopped and will stay off until you allow it again"
        ),
    }


def unquarantine_tool(tool_id: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Allow tool again. Does not auto-start (user/LaunchAgent policy)."""
    if not tool_id:
        return {"ok": False, "error": "tool id required"}
    data = load_quarantine(path)
    tools = [t for t in (data.get("tools") or []) if t != tool_id]
    data["tools"] = tools
    since = dict(data.get("since") or {})
    since.pop(tool_id, None)
    data["since"] = since
    # Remember override so ensure_default_quarantine never re-seeds this tool
    uu = set(data.get("user_unquarantined") or [])
    uu.add(tool_id)
    data["user_unquarantined"] = sorted(uu)
    save_quarantine(data, path)

    # Re-enable launchd label if we know it (user can start later)
    reenabled = []
    from local_ai_monitor.end_session import launchd_labels_for
    import subprocess

    for label in launchd_labels_for(tool_id, None):
        domain = f"gui/{os.getuid()}"
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        try:
            subprocess.run(
                ["launchctl", "enable", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if os.path.isfile(plist):
                subprocess.run(
                    ["launchctl", "bootstrap", domain, plist],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                reenabled.append(label)
        except (OSError, subprocess.TimeoutExpired):
            pass

    return {
        "ok": True,
        "tool": tool_id,
        "quarantined": False,
        "reenabled": reenabled,
        "message": (
            f"{tool_id} allowed again"
            + (f" · restarted {', '.join(reenabled)}" if reenabled else "")
        ),
    }


def enforce_quarantine(*, state: Optional[str] = None) -> Dict[str, Any]:
    """If a quarantined tool is running, stop it again (KeepAlive fight)."""
    tools = list_quarantined()
    if not tools:
        return {"ok": True, "stopped": []}
    stopped: List[str] = []
    from local_ai_monitor.end_session import end_tool, stop_launchd_for_app
    from local_ai_monitor.store import live_path, read_json

    live = read_json(live_path(state)) or {}
    running = {
        str(s.get("app"))
        for s in (live.get("sessions") or [])
        if isinstance(s, dict) and s.get("app")
    }
    for tool in tools:
        if tool not in running:
            # Still bootout LaunchAgent in case process list lag
            stop_launchd_for_app(tool, None, dry_run=False)
            continue
        stop_launchd_for_app(tool, None, dry_run=False)
        end_tool(tool, dry_run=False, refresh_live=False)
        stopped.append(tool)
    return {"ok": True, "stopped": stopped, "quarantined": tools}


def quarantine_meta() -> Dict[str, Any]:
    data = load_quarantine()
    return {
        "tools": list(data.get("tools") or []),
        "since": data.get("since") or {},
    }


def cmd_quarantine(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="local-ai-monitor quarantine",
        description="Keep unused AI background tools off (e.g. OpenClaw gateway).",
    )
    p.add_argument(
        "action",
        choices=["list", "add", "remove", "enforce"],
        help="list | add <tool> | remove <tool> | enforce",
    )
    p.add_argument("tool", nargs="?", help='Tool id e.g. "OpenClaw"')
    p.add_argument("--json", action="store_true")
    args = p.parse_args(list(argv or []))

    if args.action == "list":
        data = load_quarantine()
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            tools = data.get("tools") or []
            if not tools:
                print("Nothing quarantined.")
            else:
                for t in tools:
                    since = (data.get("since") or {}).get(t, "")
                    print(f"{t}" + (f"  since {since}" if since else ""))
        return 0

    if args.action == "enforce":
        r = enforce_quarantine()
        if args.json:
            print(json.dumps(r, indent=2))
        else:
            print("enforced:", ", ".join(r.get("stopped") or []) or "(none running)")
        return 0

    if not args.tool:
        print("tool id required", file=sys.stderr)
        return 2

    if args.action == "add":
        r = quarantine_tool(args.tool)
    else:
        r = unquarantine_tool(args.tool)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print(r.get("message") or r)
    return 0 if r.get("ok") else 1
