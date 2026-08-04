"""Active vs idle classification — reclaim only unused background.

Physics: free pages / thrash decide *whether* to act.
Activity decides *what* is legal.

Product law (2026-07-30; restated after false Grok kill):
- **Default: do not suddenly shut down apps the user is using.**
  That is all interactive / mid-stream / open work — not just Grok.
- **Exception:** background load that is **not serving the user**
  (idle gateway/service, unused cache helpers under thrash).
- Low CPU on a live CLI/desktop/agent is often "waiting for you" — not reclaim.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Set

from local_ai_monitor.config import CONFIG_DIR

TOOL_ACTIVITY_PATH = os.path.join(CONFIG_DIR, "tool_activity.json")

# CPU% above this → actively working (mid-stream).
ACTIVE_CPU_PCT = 2.5
# Below this and stale activity → quiet (still not auto-reclaim unless service).
IDLE_CPU_PCT = 0.8
# No tool activity stamp within this many seconds → stale (idle clock).
ACTIVITY_STALE_S = 15 * 60  # 15 minutes
# Services: slightly stricter idle CPU (gateways chatter).
SERVICE_IDLE_CPU_PCT = 1.5

# Named AI CLIs (documentation + manage protect_tools seed). Not the only
# protected class — see is_user_work() / auto_reclaim rules below.
INTERACTIVE_APPS: Set[str] = {
    "Grok",
    "Claude CLI",
    "Claude",
    "Codex",
    "Cursor",
    "Gemini",
    "Aider",
    "Continue",
    "Windsurf",
    "ChatGPT",
    "Claude Desktop",
}

# Session kinds that are *user work* even when quiet (never auto soft-stop).
USER_WORK_KINDS: Set[str] = {
    "cli",
    "desktop",
    "agent",
    "unknown",
}


def is_interactive_app(app: str, *, kind: str = "") -> bool:
    """Named interactive AI tools (seed list). Prefer is_user_work for policy."""
    a = (app or "").strip()
    if not a:
        return False
    if a in INTERACTIVE_APPS:
        return True
    low = a.lower()
    if low in {x.lower() for x in INTERACTIVE_APPS}:
        return True
    if kind == "cli" and any(
        x in low for x in ("grok", "claude", "codex", "cursor", "gemini", "aider")
    ):
        return True
    return False


def is_user_work(session: Dict[str, Any]) -> bool:
    """True if this row is (or may be) work the user still owns.

    Law: sudden shutdown is forbidden for user work. Only proven unused
    background services are auto-reclaimable.
    """
    kind = str(session.get("kind") or "").lower()
    app = str(session.get("app") or "")
    sid = str(session.get("session_id") or "")
    if kind == "service" or sid.startswith("svc:") or app == "OpenClaw":
        return False
    if kind in USER_WORK_KINDS or not kind:
        return True
    if is_interactive_app(app, kind=kind):
        return True
    return True  # fail closed: unknown → treat as user work


def load_tool_activity(path: Optional[str] = None) -> Dict[str, float]:
    p = path or TOOL_ACTIVITY_PATH
    p = os.path.expanduser(p)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, float] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def attention_active_apps(attention: Sequence[Dict[str, Any]]) -> Set[str]:
    """Apps that need the user or are limited — treat as active work."""
    out: Set[str] = set()
    for it in attention:
        if not isinstance(it, dict):
            continue
        kind = it.get("kind") or ""
        if kind in ("needs_you", "limited", "ready_to_resume"):
            app = it.get("app")
            if app:
                out.add(str(app))
    return out


def classify_session(
    session: Dict[str, Any],
    *,
    tool_activity: Optional[Dict[str, float]] = None,
    attention_apps: Optional[Set[str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Return activity classification for one live session row."""
    now = now if now is not None else time.time()
    app = str(session.get("app") or "")
    sid = str(session.get("session_id") or "")
    kind = str(session.get("kind") or "")
    try:
        cpu = float(session.get("cpu_pct") or 0)
    except (TypeError, ValueError):
        cpu = 0.0
    try:
        rss_kb = int(session.get("rss_kb") or 0)
    except (TypeError, ValueError):
        rss_kb = 0

    ta = tool_activity if tool_activity is not None else load_tool_activity()
    last = float(ta.get(app) or 0)
    age = (now - last) if last > 0 else None
    stale = age is None or age >= ACTIVITY_STALE_S
    recent = age is not None and age < ACTIVITY_STALE_S

    att = attention_apps if attention_apps is not None else set()
    in_attention = app in att

    is_service = kind == "service" or sid.startswith("svc:") or app == "OpenClaw"
    user_work = (not is_service) and is_user_work(session)

    def _row(
        activity: str,
        reason: str,
        *,
        auto_reclaim: bool,
        is_svc: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return {
            "activity": activity,
            "reason": reason,
            "auto_reclaim": bool(auto_reclaim),
            "is_service": is_service if is_svc is None else is_svc,
            "cpu_pct": cpu,
            "rss_kb": rss_kb,
            "activity_age_s": age,
        }

    # --- Active (never auto-kill) ---
    if in_attention:
        return _row("active", "needs attention / limit state", auto_reclaim=False)
    if cpu >= ACTIVE_CPU_PCT:
        return _row(
            "active",
            f"cpu {cpu:.1f}% ≥ {ACTIVE_CPU_PCT}% (mid-stream)",
            auto_reclaim=False,
        )
    if recent and cpu >= IDLE_CPU_PCT and not is_service:
        return _row(
            "active",
            f"recent tool activity ({int(age)}s ago) with cpu {cpu:.1f}%",
            auto_reclaim=False,
        )

    # --- User work (any app the user may still be using) — never auto-reclaim ---
    if user_work:
        return _row(
            "user_work",
            "user-owned session — sudden auto-stop forbidden (menu confirm only)",
            auto_reclaim=False,
            is_svc=False,
        )

    # --- Service idle (background gateway with no task) — legal reclaim ---
    if is_service and cpu < SERVICE_IDLE_CPU_PCT:
        return _row(
            "idle_service",
            "unused background service — legal reclaim under thrash/pressure",
            auto_reclaim=True,
            is_svc=True,
        )

    # Service but not idle enough
    if is_service:
        return _row(
            "service_busy",
            "background service still using CPU — not reclaimed",
            auto_reclaim=False,
            is_svc=True,
        )

    # Fail closed: never auto-reclaim unknown rows
    return _row(
        "protected",
        "not classified as unused background — auto-stop forbidden",
        auto_reclaim=False,
    )


def enrich_sessions_activity(
    sessions: Sequence[Dict[str, Any]],
    attention: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    ta = load_tool_activity()
    att = attention_active_apps(attention or [])
    now = time.time()
    out: List[Dict[str, Any]] = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        row = dict(s)
        cls = classify_session(row, tool_activity=ta, attention_apps=att, now=now)
        row["activity_state"] = cls["activity"]
        row["activity_reason"] = cls["reason"]
        row["auto_reclaim"] = bool(cls["auto_reclaim"])
        out.append(row)
    return out


def pick_reclaim_candidate(
    sessions: Sequence[Dict[str, Any]],
    *,
    protect: Sequence[str] = (),
    min_rss_kb: int = 0,
    prefer_services: bool = True,
) -> Optional[Dict[str, Any]]:
    """Highest-RSS auto_reclaim=True session; prefer idle services first."""
    protect_set = {str(p) for p in protect}
    services: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        if not s.get("auto_reclaim"):
            continue
        app = str(s.get("app") or "")
        if not app or app in protect_set:
            continue
        if not s.get("session_id"):
            continue
        try:
            rss = int(s.get("rss_kb") or 0)
        except (TypeError, ValueError):
            rss = 0
        if rss < min_rss_kb:
            continue
        if s.get("activity_state") == "idle_service" or s.get("kind") == "service":
            services.append(s)
        else:
            others.append(s)

    def best(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not rows:
            return None
        return max(rows, key=lambda r: int(r.get("rss_kb") or 0))

    if prefer_services:
        b = best(services)
        if b is not None:
            return b
    return best(others) or best(services)
