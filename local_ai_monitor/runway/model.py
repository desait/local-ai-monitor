"""Runway card — the only user-facing product object."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Forecast.state → Runway state. Do not invent a sixth everyday state.
FORECAST_TO_STATE = {
    "ok": "open",
    "caution": "watch",
    "stop_start_gate": "hold",
    "freeze_risk": "protect",
    "unknown": "unknown",
    "calibrating": "watch",
}

# Forecast.recommendation → one next action a person can take.
REC_TO_ACTION = {
    "do_nothing": "nothing",
    "avoid_new_heavy_work": "avoid_start",
    "reclaim_idle": "pause_idle",
    "protect_active_work": "protect_work",
    "refuse": "wait",
}

STATES = ("open", "watch", "hold", "protect", "unknown")
ACTIONS = ("nothing", "avoid_start", "pause_idle", "protect_work", "wait")

CHIP_WORD = {
    "open": "Open",
    "watch": "Watch",
    "hold": "Hold",
    "protect": "Protect",
    "unknown": "—",
}

DEFAULT_BRAND = "Runway"


def brand() -> str:
    """Menu-bar / chip prefix. Laptop may already set LOCAL_AI_MONITOR_BRAND."""
    raw = (os.environ.get("LOCAL_AI_MONITOR_BRAND") or "").strip()
    return raw or DEFAULT_BRAND


def chip_for(state: str, *, stale: bool = False) -> str:
    if stale:
        return f"{brand()} · —"
    return f"{brand()} · {CHIP_WORD.get(state, '—')}"


def state_from_forecast(forecast_state: Optional[str], *, stale: bool = False) -> str:
    if stale:
        return "unknown"
    return FORECAST_TO_STATE.get(str(forecast_state or "unknown"), "unknown")


def action_from_recommendation(recommendation: Optional[str], *, stale: bool = False) -> str:
    if stale:
        return "wait"
    return REC_TO_ACTION.get(str(recommendation or "refuse"), "wait")


@dataclass(frozen=True)
class RunwayTool:
    """One recognizable tool. No PIDs, no session UUIDs."""

    id: str
    name: str
    load: str
    mem: str
    sessions: int
    role: str  # your_work | background | quiet

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunwayCard:
    """Plain-English snapshot a non-technical person can act on."""

    state: str
    action: str
    can_start: bool
    chip: str
    title: str
    sentence: str
    detail: str
    next_step: str
    promise: str
    tools: List[RunwayTool] = field(default_factory=list)
    candidate_label: Optional[str] = None
    candidate_app: Optional[str] = None
    candidate_session_id: Optional[str] = None
    action_label: str = "Pause unused"
    show: bool = True
    stale: bool = False
    band: str = "unknown"
    forecast_state: Optional[str] = None
    recommendation: Optional[str] = None
    headroom_mb: Optional[int] = None
    headroom_ok_mb: Optional[int] = None
    headroom_warn_mb: Optional[int] = None
    free_mb: Optional[int] = None
    thrash_score: Optional[float] = None
    ai_rss_mb: Optional[int] = None
    memsize_mb: Optional[int] = None
    ai_mem_pct: Optional[float] = None
    swap_used_mb: Optional[int] = None
    swap_total_mb: Optional[int] = None
    profile_status: Optional[str] = None
    profile_confidence: Optional[float] = None
    host_id: Optional[str] = None
    reason_tech: Optional[str] = None
    live_ts: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["tools"] = [t.to_dict() if isinstance(t, RunwayTool) else t for t in self.tools]
        return d

    def to_resource_block(self) -> Dict[str, Any]:
        """Menu-bar / live.json resource payload. Keeps existing keys."""
        urgency = "none"
        if self.state == "protect":
            urgency = "hard"
        elif self.state == "hold":
            urgency = "warn"
        elif self.state == "watch":
            urgency = "none"
        return {
            "band": self.band,
            "pressure_state": self.forecast_state,
            "recommendation": self.recommendation,
            "can_start_heavy": self.can_start,
            "show": self.show,
            "chip": self.chip,
            "title": self.title,
            "detail": self.detail,
            "candidate_label": self.candidate_label,
            "candidate_app": self.candidate_app,
            "candidate_session_id": self.candidate_session_id,
            "action_label": self.action_label,
            "headroom_mb": self.headroom_mb,
            "headroom_ok_mb": self.headroom_ok_mb,
            "headroom_warn_mb": self.headroom_warn_mb,
            "free_mb": self.free_mb,
            "swap_used_mb": self.swap_used_mb,
            "swap_total_mb": self.swap_total_mb,
            "thrash_score": self.thrash_score,
            "ai_rss_mb": self.ai_rss_mb,
            "memsize_mb": self.memsize_mb,
            "ai_mem_pct": self.ai_mem_pct,
            "profile_status": self.profile_status,
            "profile_confidence": self.profile_confidence,
            "host_id": self.host_id,
            "urgency": urgency,
            "reason_tech": self.reason_tech,
            # Runway fields (menu bar reads these when present)
            "plain_state": self.state,
            "plain_title": self.title,
            "plain_detail": self.detail,
            "next_step": self.next_step,
            "promise": self.promise,
            "runway_action": self.action,
            "stale": self.stale,
        }
