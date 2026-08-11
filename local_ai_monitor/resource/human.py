"""Plain-English resource policy for menu bar (non-technical).

Never expose pids, free-page counts as primary copy, or session UUIDs.
Primary story: headroom / freeze-risk (can I multitask?) + AI mass.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from local_ai_monitor.humanize import tool_display_name
from local_ai_monitor.resource.policy import PolicyDecision, evaluate


def _mb(rss_kb: int) -> int:
    return max(1, int(round(rss_kb / 1024.0))) if rss_kb > 0 else 0


def _round_mb(value: Any) -> Optional[int]:
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _headroom_from_decision(d: PolicyDecision) -> Dict[str, Any]:
    phys = d.physics if isinstance(d.physics, dict) else {}
    hr = phys.get("headroom") if isinstance(phys.get("headroom"), dict) else {}
    return hr if isinstance(hr, dict) else {}


def humanize_decision(d: PolicyDecision) -> Dict[str, Any]:
    """Menu-bar / notification payload. show=False when nothing to interrupt."""
    hr = _headroom_from_decision(d)
    phys = d.physics if isinstance(d.physics, dict) else {}
    forecast = phys.get("forecast") if isinstance(phys.get("forecast"), dict) else {}
    headroom_mb = hr.get("headroom_mb")
    thrash = hr.get("thrash_score")
    try:
        headroom_i = int(round(float(headroom_mb))) if headroom_mb is not None else None
    except (TypeError, ValueError):
        headroom_i = None
    try:
        thrash_f = float(thrash) if thrash is not None else 0.0
    except (TypeError, ValueError):
        thrash_f = 0.0

    free_mb = d.free_mb
    free_mb_i = int(round(free_mb)) if free_mb is not None else None
    ai_mb = _mb(d.ai_rss_kb)

    base: Dict[str, Any] = {
        "band": d.band,
        "pressure_state": forecast.get("state"),
        "recommendation": forecast.get("recommendation"),
        "can_start_heavy": forecast.get("can_start_heavy"),
        "show": False,
        "chip": None,
        "title": None,
        "detail": None,
        "candidate_label": None,
        "candidate_app": None,
        "candidate_session_id": None,
        "action_label": "Reclaim idle",
        "free_mb": free_mb_i,  # expert/debug only — not primary copy
        "headroom_mb": headroom_i,
        "headroom_ok_mb": _round_mb(hr.get("headroom_ok_mb")),
        "headroom_warn_mb": _round_mb(hr.get("headroom_warn_mb")),
        "profile_status": hr.get("profile_status"),
        "profile_confidence": hr.get("profile_confidence"),
        "host_id": hr.get("host_id"),
        "swap_used_mb": _round_mb(d.physics.get("swap_used_mb")) if isinstance(d.physics, dict) else None,
        "swap_total_mb": _round_mb(d.physics.get("swap_total_mb")) if isinstance(d.physics, dict) else None,
        "thrash_score": thrash_f,
        "ai_rss_mb": ai_mb,
        "urgency": d.band if d.band in ("warn", "hard") else "none",
        "reason_tech": d.reason,
    }

    if d.band not in ("warn", "hard") or forecast.get("state") == "caution":
        if forecast.get("state") == "caution":
            base["show"] = True
            base["chip"] = "AI · Watch"
            base["title"] = "Keep an eye on capacity"
            base["detail"] = (
                f"About {headroom_i} MB of reclaimable headroom. "
                "Swap/page churn is quiet, so starts remain allowed."
                if headroom_i is not None
                else "Swap/page churn is quiet, so starts remain allowed."
            )
        # Quiet: no interrupt. Optional chip stays empty.
        return base

    cand = d.candidate
    if cand is not None:
        tool = tool_display_name(cand.app)
        base["candidate_app"] = cand.app
        base["candidate_session_id"] = cand.session_id
        base["candidate_label"] = f"{tool} · {_mb(cand.rss_kb)} MB"
    else:
        base["candidate_label"] = "No single session to end safely"

    ai_phrase = f"AI tools are using about {ai_mb} MB" if ai_mb else "AI tools are running"
    head_phrase = (
        f"About {headroom_i} MB of reclaimable headroom"
        if headroom_i is not None
        else "Reclaimable headroom is low"
    )

    if d.band == "hard":
        base["show"] = True
        if thrash_f >= 8.0:
            base["chip"] = "AI · Freeze risk"
            base["title"] = "This Mac is under memory thrash"
            thrash_note = "Swap/page churn is elevated — risk of beachballs."
        else:
            base["chip"] = "AI · Headroom low"
            base["title"] = "Little room left for more heavy apps"
            thrash_note = "Not thrashing yet, but headroom is thin."
        if cand is not None:
            base["detail"] = (
                f"{head_phrase}. {thrash_note} {ai_phrase}. "
                f"Idle reclaim may stop “{tool_display_name(cand.app)}” "
                f"({_mb(cand.rss_kb)} MB) if it is not mid-stream. "
                f"Active work is never auto-killed."
            )
        else:
            base["detail"] = (
                f"{head_phrase}. {thrash_note} {ai_phrase}. "
                "No idle AI session to reclaim — active work is left alone."
            )
        base["action_label"] = "Reclaim idle"
        base["auto_end"] = False
        return base

    # warn
    base["show"] = True
    base["chip"] = "AI · Start gate"
    base["title"] = "Do not start more heavy work yet"
    if cand is not None:
        base["detail"] = (
            f"{head_phrase}. {ai_phrase}. "
            f"Prefer pausing idle background load "
            f"(e.g. “{tool_display_name(cand.app)}”) before opening more heavy work. "
            f"Active sessions are never auto-killed."
        )
    else:
        base["detail"] = f"{head_phrase}. {ai_phrase}."
    base["action_label"] = "Reclaim idle"
    base["auto_end"] = False
    return base


def resource_live_block(
    *,
    state: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    d = evaluate(state=state, config=config)
    return humanize_decision(d)
