"""Per-tool weekly local burn + limit state (honest, fail-closed).

A) week_tokens from local ledgers (Claude stats-cache, session tokens)
B) limit_state / resets_at only when attention/limit events provide them
Never invent remaining % of vendor plan.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

CLAUDE_STATS = os.path.expanduser("~/.claude/stats-cache.json")


def _week_start_local() -> datetime:
    now = datetime.now().astimezone()
    # Monday 00:00 local
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = monday.fromordinal(monday.toordinal() - monday.weekday())
    return monday


def claude_week_tokens() -> Optional[int]:
    if not os.path.isfile(CLAUDE_STATS):
        return None
    try:
        with open(CLAUDE_STATS, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    daily = raw.get("dailyModelTokens") or []
    if not isinstance(daily, list):
        return None
    start = _week_start_local().date()
    total = 0
    found = False
    for row in daily:
        if not isinstance(row, dict):
            continue
        ds = row.get("date") or ""
        try:
            d = datetime.strptime(ds[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < start:
            continue
        found = True
        tbm = row.get("tokensByModel") or {}
        if isinstance(tbm, dict):
            for v in tbm.values():
                try:
                    total += int(v)
                except (TypeError, ValueError):
                    pass
    return total if found else None


def session_week_tokens(sessions: Sequence[Any], app: str) -> Optional[int]:
    """Sum tokens_total for sessions of app (best-effort; not calendar-perfect)."""
    total = 0
    n = 0
    for s in sessions:
        a = getattr(s, "app", None) or (s.get("app") if isinstance(s, dict) else None)
        if a != app:
            continue
        tt = getattr(s, "tokens_total", None)
        if tt is None and isinstance(s, dict):
            tt = s.get("tokens_total")
        if tt is None:
            continue
        try:
            total += int(tt)
            n += 1
        except (TypeError, ValueError):
            pass
    return total if n else None


def build_budgets(
    sessions: Sequence[Any],
    attention: Sequence[Dict[str, Any]],
    apps: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """Map tool_id → budget dict for live.json."""
    # index attention limits
    limit_by_app: Dict[str, Dict[str, Any]] = {}
    for it in attention:
        if it.get("kind") in ("limited", "ready_to_resume"):
            app = it.get("app") or ""
            if app and app not in limit_by_app:
                limit_by_app[app] = it

    out: Dict[str, Dict[str, Any]] = {}
    claude_week = claude_week_tokens()

    for app in apps:
        week = None
        week_label = None
        if app in ("Claude CLI", "Claude Desktop"):
            if claude_week is not None:
                week = claude_week
                week_label = "This week · local (Claude)"
            else:
                week = session_week_tokens(sessions, app)
                if week is not None:
                    week_label = "Sessions · local tokens"
        else:
            week = session_week_tokens(sessions, app)
            if week is not None:
                week_label = "Sessions · local tokens"

        lim = limit_by_app.get(app)
        limit_state = "unknown"
        resets_at = None
        remaining_label = None
        limit_kind = None
        if lim:
            if lim.get("kind") == "ready_to_resume":
                limit_state = "ready"
            else:
                limit_state = "limited"
            resets_at = lim.get("resets_at")
            remaining_label = lim.get("detail") or (
                f"Resets {resets_at}" if resets_at else None
            )
            limit_kind = lim.get("limit_kind")
        elif week is not None:
            limit_state = "ok"  # we know burn only, not plan remaining

        if week is None and limit_state == "unknown":
            continue  # omit empty tools

        row = {
            "week_tokens": week,
            "week_label": week_label,
            "limit_state": limit_state,
            "resets_at": resets_at,
            "remaining_label": remaining_label,  # never fake %
        }
        if limit_kind:
            row["limit_kind"] = limit_kind
        out[app] = row

    # Overlay Claude structured limits (session registry) even if no process tokens
    try:
        from local_ai_monitor.claude_limits import claude_budget_overlay

        overlay = claude_budget_overlay(list(attention))
        if overlay:
            cur = out.get("Claude CLI") or {
                "week_tokens": claude_week,
                "week_label": "This week · local (Claude)" if claude_week else None,
                "limit_state": "unknown",
                "resets_at": None,
                "remaining_label": None,
            }
            cur.update({k: v for k, v in overlay.items() if v is not None})
            out["Claude CLI"] = cur
    except Exception:
        pass

    return out


def format_week_tokens(n: Optional[int]) -> str:
    if n is None:
        return "—"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"
