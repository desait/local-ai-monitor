"""Pressure forecast and safe recommendation layer.

This is the user-facing control model for an 8 GB Mac:

- swap/pageout churn predicts hangs better than free pages alone;
- static swap used is context, not an emergency by itself;
- action recommendations must preserve active work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


STATE_OK = "ok"
STATE_CAUTION = "caution"
STATE_STOP_START_GATE = "stop_start_gate"
STATE_FREEZE_RISK = "freeze_risk"
STATE_UNKNOWN = "unknown"

REC_DO_NOTHING = "do_nothing"
REC_AVOID_NEW_HEAVY = "avoid_new_heavy_work"
REC_RECLAIM_IDLE = "reclaim_idle"
REC_PROTECT_ACTIVE = "protect_active_work"
REC_REFUSE = "refuse"


@dataclass(frozen=True)
class PressureForecast:
    state: str
    recommendation: str
    can_start_heavy: bool
    reason: str
    headroom_mb: Optional[float]
    thrash_score: float
    swap_used_mb: Optional[float]
    swap_total_mb: Optional[float]
    swap_ratio: Optional[float]
    has_safe_candidate: bool
    active_work_only: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_forecast(
    *,
    band: str,
    headroom_mb: Any,
    thrash_score: Any,
    swap_used_mb: Any = None,
    swap_total_mb: Any = None,
    has_safe_candidate: bool = False,
    active_work_only: bool = False,
) -> PressureForecast:
    headroom = _float_or_none(headroom_mb)
    thrash = _float_or_none(thrash_score) or 0.0
    swap_used = _float_or_none(swap_used_mb)
    swap_total = _float_or_none(swap_total_mb)
    swap_ratio = None
    if swap_used is not None and swap_total and swap_total > 0:
        swap_ratio = swap_used / swap_total

    if band == "unknown" or headroom is None:
        return PressureForecast(
            state=STATE_UNKNOWN,
            recommendation=REC_REFUSE,
            can_start_heavy=False,
            reason="headroom unknown — refuse heavy starts and destructive relief",
            headroom_mb=headroom,
            thrash_score=thrash,
            swap_used_mb=swap_used,
            swap_total_mb=swap_total,
            swap_ratio=swap_ratio,
            has_safe_candidate=has_safe_candidate,
            active_work_only=active_work_only,
        )

    if band == "hard" or thrash >= 25.0:
        if has_safe_candidate:
            rec = REC_RECLAIM_IDLE
            reason = "freeze risk; safe idle reclaim exists"
        elif active_work_only:
            rec = REC_PROTECT_ACTIVE
            reason = "freeze risk, but only active work remains"
        else:
            rec = REC_AVOID_NEW_HEAVY
            reason = "freeze risk; no safe reclaim target"
        return PressureForecast(
            state=STATE_FREEZE_RISK,
            recommendation=rec,
            can_start_heavy=False,
            reason=reason,
            headroom_mb=headroom,
            thrash_score=thrash,
            swap_used_mb=swap_used,
            swap_total_mb=swap_total,
            swap_ratio=swap_ratio,
            has_safe_candidate=has_safe_candidate,
            active_work_only=active_work_only,
        )

    high_static_swap = bool(swap_ratio is not None and swap_ratio >= 0.50)
    if thrash >= 8.0:
        rec = REC_RECLAIM_IDLE if has_safe_candidate else REC_AVOID_NEW_HEAVY
        return PressureForecast(
            state=STATE_STOP_START_GATE,
            recommendation=rec,
            can_start_heavy=False,
            reason="swap/page churn says avoid starting new heavy work",
            headroom_mb=headroom,
            thrash_score=thrash,
            swap_used_mb=swap_used,
            swap_total_mb=swap_total,
            swap_ratio=swap_ratio,
            has_safe_candidate=has_safe_candidate,
            active_work_only=active_work_only,
        )

    if band == "warn" or high_static_swap:
        return PressureForecast(
            state=STATE_CAUTION,
            recommendation=REC_AVOID_NEW_HEAVY,
            can_start_heavy=True,
            reason="capacity is watch-level; starts remain allowed while churn is quiet",
            headroom_mb=headroom,
            thrash_score=thrash,
            swap_used_mb=swap_used,
            swap_total_mb=swap_total,
            swap_ratio=swap_ratio,
            has_safe_candidate=has_safe_candidate,
            active_work_only=active_work_only,
        )

    return PressureForecast(
        state=STATE_OK,
        recommendation=REC_DO_NOTHING,
        can_start_heavy=True,
        reason="headroom is usable and swap churn is quiet",
        headroom_mb=headroom,
        thrash_score=thrash,
        swap_used_mb=swap_used,
        swap_total_mb=swap_total,
        swap_ratio=swap_ratio,
        has_safe_candidate=has_safe_candidate,
        active_work_only=active_work_only,
    )
