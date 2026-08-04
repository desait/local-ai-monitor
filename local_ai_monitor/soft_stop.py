"""Pause / soft-stop before hard kill — mid-stream work is never the target.

Order for reclaimable (idle) sessions:
1. LaunchAgent services → bootout (clean stop; no mid-stream chat).
2. Idle sessions → SIGTERM first (process can flush); SIGKILL only if still
   alive after grace *and* still classified idle.
3. Active sessions → refuse (caller must not request).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from local_ai_monitor.activity import classify_session
from local_ai_monitor.end_session import end_session, launchd_labels_for, stop_launchd_for_app


def soft_stop_session(
    app: str,
    session_id: str,
    *,
    state: Optional[str] = None,
    session_row: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Stop one session only if idle (unless force=True for explicit user kill)."""
    if not force and session_row is not None:
        cls = classify_session(session_row)
        if not cls.get("auto_reclaim"):
            return {
                "ok": False,
                "refused": True,
                "app": app,
                "session_id": session_id,
                "activity": cls.get("activity"),
                "error": f"refused: {cls.get('reason')} (never kill mid-stream)",
                "message": f"Left {app} running — still active",
            }

    labels = launchd_labels_for(app, session_id)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "app": app,
            "session_id": session_id,
            "would_bootout": labels,
            "message": f"Would soft-stop {app}" + (" via LaunchAgent bootout" if labels else ""),
        }

    # Services: bootout first (OpenClaw KeepAlive), then end_session cleans pids
    if labels:
        launchd = stop_launchd_for_app(app, session_id, dry_run=False)
        r = end_session(
            app,
            session_id,
            state=state,
            dry_run=False,
            refresh_live=True,
        )
        r["launchd_pre"] = launchd
        r["soft_stop"] = True
        r["message"] = (
            r.get("message")
            or f"Soft-stopped {app} (background service)"
        )
        return r

    # Idle CLI/desktop: SIGTERM path inside end_session (escalate only if needed)
    r = end_session(
        app,
        session_id,
        state=state,
        dry_run=False,
        refresh_live=True,
    )
    r["soft_stop"] = True
    return r
