"""Parking lot mode: buy responsiveness without killing work.

This is intentionally conservative:
- allowlisted OS/browser helper processes only;
- renice/taskpolicy background, not SIGKILL;
- no AI CLIs, no Terminal sessions, no Safari/Mail/Xcode app quits.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, Optional

from local_ai_monitor.resource.thrash_ladder import (
    _taskpolicy_background,
    _renice,
    list_hog_pids,
    mute_hogs,
    pause_hogs,
)


ACTIVE_STATES = {"active", "interactive", "open"}
PARKABLE_KINDS = {"agent", "service"}
PROTECTED_APPS = {"Claude CLI", "Codex", "Grok", "ChatGPT", "Claude Desktop", "Cursor", "OpenAI CLI"}


def _state_dir(state: Optional[str] = None) -> str:
    if state:
        return os.path.abspath(os.path.expanduser(state))
    env = os.environ.get("LOCAL_AI_MONITOR_STATE")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser("~/.local/state/local-ai-monitor"))


def _read_live(state: Optional[str] = None) -> Dict[str, Any]:
    st = _state_dir(state)
    for name in ("live.min.json", "live.json"):
        path = os.path.join(st, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data["_path"] = path
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _mb(rss_kb: Any) -> int:
    try:
        return max(0, int(round(float(rss_kb) / 1024.0)))
    except (TypeError, ValueError):
        return 0


def _short_cmd(cmd: str) -> str:
    base = os.path.basename((cmd or "").split(" ", 1)[0])
    if base:
        return base
    return (cmd or "background helper")[:80]


def _session_title(s: Dict[str, Any]) -> str:
    app = str(s.get("app") or "Helper")
    label = str(s.get("label") or "")
    if app == "Buzz":
        return label or "Buzz helper"
    if label and not label.startswith("pid"):
        return f"{app} · {label}"
    return app


def _parkable_sessions(live: Dict[str, Any], *, min_rss_kb: int = 5 * 1024) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for s in live.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        app = str(s.get("app") or "")
        kind = str(s.get("kind") or "")
        activity = str(s.get("activity_state") or s.get("activity") or "").lower()
        rss_kb = int(s.get("rss_kb") or 0)
        pids = [int(p) for p in (s.get("pids") or []) if str(p).isdigit()]
        if rss_kb < min_rss_kb or not pids:
            continue
        if app in PROTECTED_APPS:
            continue
        if kind not in PARKABLE_KINDS:
            continue
        if activity in ACTIVE_STATES:
            continue
        rows.append(
            {
                "id": f"session:{s.get('session_id') or app}",
                "type": "session_helper",
                "title": _session_title(s),
                "subtitle": "Idle background helper",
                "app": app,
                "session_id": s.get("session_id"),
                "rss_kb": rss_kb,
                "rss_mb": _mb(rss_kb),
                "pids": pids,
                "action": "lower_priority",
                "action_label": "Lower priority",
                "safe": True,
            }
        )
    return rows


def _protected_sessions(live: Dict[str, Any], *, limit: int = 5) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for s in live.get("sessions") or []:
        if not isinstance(s, dict):
            continue
        app = str(s.get("app") or "")
        activity = str(s.get("activity_state") or s.get("activity") or "").lower()
        kind = str(s.get("kind") or "")
        if activity not in ACTIVE_STATES and app not in PROTECTED_APPS:
            continue
        rows.append(
            {
                "id": f"protected:{s.get('session_id') or app}",
                "title": _session_title(s),
                "subtitle": "Active work" if activity in ACTIVE_STATES else "Protected app",
                "app": app,
                "kind": kind,
                "rss_kb": int(s.get("rss_kb") or 0),
                "rss_mb": _mb(s.get("rss_kb")),
                "reason": "Monitor will not park active work.",
            }
        )
    rows.sort(key=lambda r: int(r.get("rss_kb") or 0), reverse=True)
    return rows[:limit]


def _process_targets() -> list[Dict[str, Any]]:
    rows = []
    for h in list_hog_pids(min_rss_kb=20 * 1024, limit=8):
        cmd = str(h.get("cmd") or "")
        rss_kb = int(h.get("rss_kb") or 0)
        pid = int(h.get("pid") or 0)
        rows.append(
            {
                "id": f"pid:{pid}",
                "type": "process_helper",
                "title": _short_cmd(cmd),
                "subtitle": "System/browser helper",
                "pid": pid,
                "pids": [pid],
                "rss_kb": rss_kb,
                "rss_mb": _mb(rss_kb),
                "cmd": cmd,
                "action": "lower_priority",
                "action_label": "Lower priority",
                "safe": True,
            }
        )
    return rows


def _dedupe_targets(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    seen_pids: set[int] = set()
    out = []
    for row in sorted(rows, key=lambda r: int(r.get("rss_kb") or 0), reverse=True):
        pids = [int(p) for p in row.get("pids") or []]
        if pids and any(p in seen_pids for p in pids):
            continue
        seen_pids.update(pids)
        out.append(row)
    return out


def _apply_targets(targets: list[Dict[str, Any]], *, dry_run: bool) -> list[Dict[str, Any]]:
    out = []
    for t in targets:
        pids = [int(p) for p in t.get("pids") or []]
        results = []
        for pid in pids:
            if dry_run:
                results.append({"pid": pid, "ok": True, "action": "would_lower_priority"})
            else:
                ok_n = _renice(pid, 19)
                ok_t = _taskpolicy_background(pid)
                results.append({"pid": pid, "ok": bool(ok_n or ok_t), "renice": ok_n, "taskpolicy": ok_t})
        out.append({**t, "results": results, "parked": all(r.get("ok") for r in results)})
    return out


def parking_status(*, state: Optional[str] = None) -> Dict[str, Any]:
    live = _read_live(state)
    process_targets = [] if state else _process_targets()
    targets = _dedupe_targets(_parkable_sessions(live) + process_targets)
    protected = _protected_sessions(live)
    return {
        "ok": True,
        "mode": "parking_lot",
        "state_path": live.get("_path"),
        "targets": targets,
        "parkable": targets,
        "protected": protected,
        "already_parked": [],
        "summary": {
            "parkable_count": len(targets),
            "parkable_mb": sum(int(t.get("rss_mb") or 0) for t in targets),
            "protected_count": len(protected),
        },
        "title": "Parking Lot",
        "message": (
            "Only active work remains."
            if not targets
            else (
                f"Ready to park: {targets[0].get('title') or 'background helper'} · {targets[0].get('rss_mb') or 0} MB"
                if len(targets) == 1
                else f"Ready to park: {len(targets)} helpers · {sum(int(t.get('rss_mb') or 0) for t in targets)} MB"
            )
        ),
        "detail": "Lowers priority. Does not close apps or active AI work.",
    }


def parking_apply(*, dry_run: bool = True, mute: bool = True, state: Optional[str] = None) -> Dict[str, Any]:
    status = parking_status(state=state)
    touched = _apply_targets(list(status.get("parkable") or []), dry_run=dry_run)
    acted = bool(touched) and not dry_run
    return {
        **status,
        "ok": True,
        "mode": "parking_lot",
        "dry_run": dry_run,
        "mute": mute,
        "acted": acted,
        "targets": touched,
        "parked": touched,
        "safety": "lower priority/background policy only; no AI/user apps killed",
        "message": (
            f"Parked {len(touched)} helper(s)."
            if acted
            else ("Would park helper(s)." if touched else "Nothing safe to park. Only active work remains.")
        ),
    }


def cmd_parking_lot(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-monitor parking-lot",
        description="Park unused background helpers without killing active work.",
    )
    p.add_argument("command", nargs="?", choices=["status", "apply"], default="status")
    p.add_argument("--apply", action="store_true", help="Actually renice/background helpers")
    p.add_argument("--pause-only", action="store_true", help="Use renice +15 instead of mute")
    p.add_argument("--state", help="Monitor state dir containing live.min.json")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(list(argv or []))

    if args.command == "status":
        out = parking_status(state=args.state)
    else:
        out = parking_apply(dry_run=not args.apply, mute=not args.pause_only, state=args.state)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(out.get("message") or out.get("step") or "Background helpers")
        if out.get("detail"):
            print(out["detail"])
        for t in out.get("parkable") or out.get("targets") or []:
            if isinstance(t, dict):
                mb = int(t.get("rss_mb") or _mb(t.get("rss_kb")))
                print(f"  {t.get('title') or 'Background helper'} · {mb} MB · {t.get('action_label') or 'Park'}")
        protected = out.get("protected") or []
        if protected:
            print("Will not touch:")
            for t in protected[:4]:
                print(f"  {t.get('title') or t.get('app')} · {t.get('rss_mb') or 0} MB")
    return 0 if out.get("ok") else 1
