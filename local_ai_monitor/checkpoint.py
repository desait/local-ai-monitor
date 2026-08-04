"""Checkpoint guidance before memory pressure.

We do not inject keystrokes into active AI terminals. This reports the safest
manual continuation/checkpoint action for each known tool.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from local_ai_monitor.config import state_dir
from local_ai_monitor.store import live_path, read_json


CHECKPOINT_HINTS = {
    "Claude CLI": "Let the current response finish, then use /compact or resume later with claude --continue / --resume.",
    "Codex": "Let the current turn finish; use /compact for long chats or codex resume later.",
    "Grok": "Let the current response finish; copy/save the key task state before closing.",
    "Cursor": "Save files and wait for background agent tasks before closing.",
    "ChatGPT": "Wait for the response to finish; keep the chat open or save the needed state.",
    "Claude Desktop": "Wait for the response to finish; keep the chat open or save the needed state.",
}


def checkpoint_advice(*, state: Optional[str] = None) -> Dict[str, Any]:
    live = read_json(live_path(state or state_dir())) or {}
    sessions = live.get("sessions") if isinstance(live, dict) else []
    if not isinstance(sessions, list):
        sessions = []
    seen: List[str] = []
    rows: List[Dict[str, Any]] = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        app = str(s.get("app") or "")
        if not app or app in seen:
            continue
        seen.append(app)
        hint = CHECKPOINT_HINTS.get(app)
        if not hint:
            continue
        rows.append(
            {
                "app": app,
                "activity": s.get("activity_state") or s.get("activity"),
                "rss_mb": int(s.get("rss_kb") or 0) // 1024,
                "hint": hint,
            }
        )
    return {
        "ok": True,
        "items": rows,
        "message": "Checkpoint active AI work before adding load" if rows else "No checkpoint advice available",
    }


def cmd_checkpoint(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-monitor checkpoint",
        description="Show per-tool checkpoint/continuation advice.",
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args(list(argv or []))
    out = checkpoint_advice()
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(out["message"])
        for item in out.get("items") or []:
            print(f"  {item['app']}: {item['hint']}")
    return 0 if out.get("ok") else 1
