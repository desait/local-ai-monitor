"""Compose one Runway card from the existing observe / judge engines."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from local_ai_monitor.humanize import mem_short, summarize_live, tool_display_name
from local_ai_monitor.resource.policy import PolicyDecision, evaluate
from local_ai_monitor.runway.copy import (
    PROMISE,
    action_label_for,
    assert_clean,
    detail_for,
    next_step_for,
    role_for,
    sentence_for,
    title_for,
)
from local_ai_monitor.runway.model import (
    RunwayCard,
    RunwayTool,
    action_from_recommendation,
    chip_for,
    state_from_forecast,
)
from local_ai_monitor.store import LiveStore, parse_local_iso


def _mb(rss_kb: int) -> int:
    return max(1, int(round(rss_kb / 1024.0))) if rss_kb > 0 else 0


def _round_mb(value: Any) -> Optional[int]:
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def live_is_stale(live: Optional[Dict[str, Any]]) -> bool:
    if not live:
        return True
    ts = parse_local_iso(str(live.get("ts") or ""))
    if not ts:
        return True
    interval = float(live.get("sample_interval_s") or 10)
    return (time.time() - ts) > 3 * interval


def _load_live(state: Optional[str] = None) -> Dict[str, Any]:
    # Read the running collector only. Do not spawn a one-shot `ps` fallback:
    # unknown beats lying, and that path is noisy on non-macOS hosts.
    live = LiveStore(state).read()
    return live if isinstance(live, dict) else {}


def _tools_from_live(live: Dict[str, Any], *, stale: bool) -> List[RunwayTool]:
    if stale:
        return []
    summary = summarize_live(live, stale=False)
    sessions = live.get("sessions") or []
    activity_by_app: Dict[str, str] = {}
    kind_by_app: Dict[str, str] = {}
    for s in sessions:
        if not isinstance(s, dict):
            continue
        app = str(s.get("app") or "")
        if not app:
            continue
        kind_by_app.setdefault(app, str(s.get("kind") or ""))
        act = str(s.get("activity_state") or s.get("activity") or "")
        # Prefer a background mark if any session of this tool is reclaimable.
        if s.get("auto_reclaim") or act == "idle_service":
            activity_by_app[app] = "idle_service"
        elif app not in activity_by_app:
            activity_by_app[app] = act
    tools: List[RunwayTool] = []
    for row in summary.get("tools") or []:
        app = str(row.get("app") or "")
        tools.append(
            RunwayTool(
                id=app,
                name=str(row.get("name") or tool_display_name(app)),
                load=str(row.get("load") or "Quiet"),
                mem=str(row.get("mem") or mem_short(int(row.get("rss_kb") or 0))),
                sessions=int(row.get("sessions") or 0),
                role=role_for(
                    activity_by_app.get(app),
                    kind=kind_by_app.get(app, ""),
                ),
            )
        )
    return tools


def _forecast(decision: PolicyDecision) -> Dict[str, Any]:
    phys = decision.physics if isinstance(decision.physics, dict) else {}
    fc = phys.get("forecast") if isinstance(phys.get("forecast"), dict) else {}
    return fc if isinstance(fc, dict) else {}


def _headroom(decision: PolicyDecision) -> Dict[str, Any]:
    phys = decision.physics if isinstance(decision.physics, dict) else {}
    hr = phys.get("headroom") if isinstance(phys.get("headroom"), dict) else {}
    return hr if isinstance(hr, dict) else {}


def card_from_decision(
    decision: PolicyDecision,
    *,
    live: Optional[Dict[str, Any]] = None,
    stale: bool = False,
) -> RunwayCard:
    """Pure compose. Used by tests as the oracle adapter."""
    fc = _forecast(decision)
    hr = _headroom(decision)
    forecast_state = fc.get("state")
    if not forecast_state:
        # Forecast missing (unit fixtures): derive a conservative state from band.
        forecast_state = {
            "ok": "ok",
            "warn": "caution",
            "hard": "freeze_risk",
            "unknown": "unknown",
        }.get(decision.band, "unknown")
    recommendation = fc.get("recommendation")
    if not recommendation:
        if decision.action == "suggest_end":
            recommendation = "reclaim_idle"
        elif decision.band == "unknown":
            recommendation = "refuse"
        elif decision.band == "ok":
            recommendation = "do_nothing"
        else:
            recommendation = "avoid_new_heavy_work"

    state = state_from_forecast(str(forecast_state), stale=stale)
    action = action_from_recommendation(str(recommendation), stale=stale)
    can_start = bool(fc.get("can_start_heavy")) if "can_start_heavy" in fc else (
        state in ("open", "watch")
    )
    if stale:
        can_start = False
        action = "wait"
        state = "unknown"

    headroom_i = _round_mb(hr.get("headroom_mb"))
    ai_mb = _mb(decision.ai_rss_kb)
    cand = decision.candidate
    cand_name = None
    cand_app = None
    cand_sid = None
    cand_label = None
    if cand is not None and cand.app:
        cand_app = cand.app
        cand_sid = cand.session_id
        cand_name = tool_display_name(cand.app)
        rss_mb = _mb(cand.rss_kb)
        cand_label = f"{cand_name} · {rss_mb} MB" if rss_mb else cand_name

    title = assert_clean(title_for(state))
    sentence = assert_clean(sentence_for(state, can_start=can_start, stale=stale))
    detail = assert_clean(
        detail_for(
            state,
            headroom_mb=headroom_i,
            candidate_name=cand_name if action == "pause_idle" else None,
            ai_mb=ai_mb,
            stale=stale,
        )
    )
    next_step = assert_clean(
        next_step_for(
            action,
            candidate_name=cand_name if action == "pause_idle" else None,
            stale=stale,
        )
    )
    promise = assert_clean(PROMISE)
    chip = chip_for(state, stale=stale)

    tools = _tools_from_live(live or {}, stale=stale)

    phys = decision.physics if isinstance(decision.physics, dict) else {}
    memsize_mb = None
    try:
        mb = phys.get("memsize_bytes")
        memsize_mb = int(round(float(mb) / (1024.0 * 1024.0))) if mb else None
    except (TypeError, ValueError):
        memsize_mb = None
    ai_mem_pct = round((ai_mb * 100.0 / memsize_mb), 1) if memsize_mb and ai_mb else None

    show = True
    if state == "open" and not stale:
        # Quiet chip; menu bar still paints the green card.
        show = True

    return RunwayCard(
        state=state,
        action=action,
        can_start=can_start and not stale,
        chip=chip,
        title=title,
        sentence=sentence,
        detail=detail,
        next_step=next_step,
        promise=promise,
        tools=tools,
        candidate_label=cand_label,
        candidate_app=cand_app if action == "pause_idle" else None,
        candidate_session_id=cand_sid if action == "pause_idle" else None,
        action_label=action_label_for(action),
        show=show,
        stale=stale,
        band=decision.band,
        forecast_state=str(forecast_state) if forecast_state else None,
        recommendation=str(recommendation) if recommendation else None,
        headroom_mb=headroom_i,
        headroom_ok_mb=_round_mb(hr.get("headroom_ok_mb")),
        headroom_warn_mb=_round_mb(hr.get("headroom_warn_mb")),
        free_mb=int(round(decision.free_mb)) if decision.free_mb is not None else None,
        thrash_score=float(hr["thrash_score"]) if hr.get("thrash_score") is not None else (
            float(fc["thrash_score"]) if fc.get("thrash_score") is not None else None
        ),
        ai_rss_mb=ai_mb,
        memsize_mb=memsize_mb,
        ai_mem_pct=ai_mem_pct,
        swap_used_mb=_round_mb(phys.get("swap_used_mb")),
        swap_total_mb=_round_mb(phys.get("swap_total_mb")),
        profile_status=hr.get("profile_status"),
        profile_confidence=hr.get("profile_confidence"),
        host_id=hr.get("host_id"),
        reason_tech=decision.reason,
        live_ts=decision.live_ts,
    )


def compose_card(
    *,
    state: Optional[str] = None,
    live: Optional[Dict[str, Any]] = None,
    decision: Optional[PolicyDecision] = None,
    stale: Optional[bool] = None,
) -> RunwayCard:
    """Load live + evaluate (unless injected) and return one card."""
    payload = live if live is not None else _load_live(state)
    is_stale = live_is_stale(payload) if stale is None else bool(stale)
    d = decision if decision is not None else evaluate(state=state)
    return card_from_decision(d, live=payload, stale=is_stale)


def compose_resource_block(
    *,
    state: Optional[str] = None,
    decision: Optional[PolicyDecision] = None,
) -> Dict[str, Any]:
    card = compose_card(state=state, decision=decision, stale=False)
    return card.to_resource_block()
