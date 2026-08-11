"""Policy: map host physics + live sessions → band + kill candidate.

Does not signal processes. Apply path calls end_session only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence

from local_ai_monitor.resource.config import load_resource_config, protect_tools
from local_ai_monitor.resource.forecast import build_forecast
from local_ai_monitor.resource.physics import PhysicsSample, sample_physics
from local_ai_monitor.store import live_path, read_json


@dataclass(frozen=True)
class SessionCandidate:
    app: str
    session_id: str
    rss_kb: int
    protected: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    ok: bool
    band: str  # unknown | ok | warn | hard
    action: str  # none | suggest_end | preflight_fail
    reason: str
    free_pages: Optional[int]
    free_mb: Optional[float]
    waterline_warn: int
    waterline_hard: int
    ai_session_count: int
    ai_rss_kb: int
    candidate: Optional[SessionCandidate]
    protected_skipped: List[str]
    physics: Dict[str, Any]
    live_ts: Optional[str]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def _sessions_from_live(state: Optional[str] = None) -> tuple[List[Dict[str, Any]], Optional[str]]:
    live = read_json(live_path(state)) or {}
    sessions = live.get("sessions") or []
    if not isinstance(sessions, list):
        sessions = []
    out = [s for s in sessions if isinstance(s, dict)]
    ts = live.get("ts")
    return out, str(ts) if ts else None


def rank_candidates(
    sessions: Sequence[Dict[str, Any]],
    *,
    protect: Sequence[str],
    min_rss_kb: int = 0,
) -> tuple[Optional[SessionCandidate], List[str]]:
    """Heaviest unprotected session; report protected that would have won."""
    protect_set = {str(p) for p in protect}
    protected_skipped: List[str] = []
    best: Optional[SessionCandidate] = None
    best_any: Optional[SessionCandidate] = None

    for s in sessions:
        app = str(s.get("app") or "")
        sid = str(s.get("session_id") or "")
        if not app or not sid:
            continue
        try:
            rss = int(s.get("rss_kb") or 0)
        except (TypeError, ValueError):
            rss = 0
        cand = SessionCandidate(
            app=app,
            session_id=sid,
            rss_kb=rss,
            protected=app in protect_set,
        )
        if best_any is None or rss > best_any.rss_kb:
            best_any = cand
        if cand.protected:
            continue
        if rss < min_rss_kb and min_rss_kb > 0:
            # still allow as fallback later
            pass
        if best is None or rss > best.rss_kb:
            best = cand

    if best is None and best_any is not None and not best_any.protected:
        best = best_any
    if best is None and best_any is not None and best_any.protected:
        protected_skipped.append(f"{best_any.app}:{best_any.session_id}")

    # If absolute heaviest was protected, note it when we picked another
    if best is not None and best_any is not None and best_any.protected:
        if best.app != best_any.app or best.session_id != best_any.session_id:
            protected_skipped.append(f"{best_any.app}:{best_any.session_id}")

    # Fallback: any unprotected even below min_rss
    if best is None:
        for s in sessions:
            app = str(s.get("app") or "")
            sid = str(s.get("session_id") or "")
            if not app or not sid or app in protect_set:
                continue
            try:
                rss = int(s.get("rss_kb") or 0)
            except (TypeError, ValueError):
                rss = 0
            if best is None or rss > best.rss_kb:
                best = SessionCandidate(app=app, session_id=sid, rss_kb=rss, protected=False)

    return best, protected_skipped


def band_for_free_pages(
    free_pages: Optional[int],
    *,
    warn: int,
    hard: int,
    physics_ok: bool,
) -> str:
    if not physics_ok or free_pages is None:
        return "unknown"
    if free_pages <= hard:
        return "hard"
    if free_pages <= warn:
        return "warn"
    return "ok"


def evaluate(
    *,
    state: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    physics: Optional[PhysicsSample] = None,
    free_pages_override: Optional[int] = None,
) -> PolicyDecision:
    cfg = config if config is not None else load_resource_config()
    warn = int(cfg.get("waterline_free_pages_warn") or 8000)
    hard = int(cfg.get("waterline_free_pages_hard") or 4000)
    min_rss = int(cfg.get("min_rss_kb") or 0)
    prot = protect_tools(cfg)

    phys = physics
    if phys is None:
        phys = sample_physics(free_pages_override=free_pages_override)
    elif free_pages_override is not None:
        phys = sample_physics(
            free_pages_override=free_pages_override,
            page_size=phys.page_size,
        )

    sessions, live_ts = _sessions_from_live(state)
    # Prefer idle reclaim targets for suggestions (never advertise killing mid-stream first)
    try:
        from local_ai_monitor.activity import enrich_sessions_activity, pick_reclaim_candidate

        live_att = []
        try:
            from local_ai_monitor.store import read_json, live_path as _lp

            live_att = (read_json(_lp(state)) or {}).get("attention") or []
        except Exception:
            live_att = []
        sessions = enrich_sessions_activity(sessions, live_att if isinstance(live_att, list) else [])
        idle_pick = pick_reclaim_candidate(
            sessions, protect=prot, min_rss_kb=min_rss, prefer_services=True
        )
    except Exception:
        idle_pick = None

    ai_rss = 0
    for s in sessions:
        try:
            ai_rss += int(s.get("rss_kb") or 0)
        except (TypeError, ValueError):
            pass

    # Primary band: headroom + thrash (2026-07-30 product law).
    # Free-page waterlines are diagnostic only — NEVER the product urgency band,
    # including exception paths. Unknown beats lying with free-page false hard.
    headroom_meta: Dict[str, Any] = {}
    try:
        from local_ai_monitor.resource.headroom import sample_headroom

        hr = sample_headroom(state=state, physics=phys, persist_prev=True)
        if hr.ok:
            band = hr.band
        else:
            band = "unknown"
        headroom_meta = {
            "headroom_mb": hr.headroom_mb,
            "thrash_score": hr.thrash_score,
            "cheap_mb": hr.cheap_mb,
            "file_backed_mb": hr.file_backed_mb,
            "headroom_ok_mb": hr.headroom_ok_mb,
            "headroom_warn_mb": hr.headroom_warn_mb,
            "profile_status": hr.profile_status,
            "profile_confidence": hr.profile_confidence,
            "host_id": hr.host_id,
            "headroom_reason": hr.reason,
            "headroom_band": hr.band,
        }
        if not hr.ok:
            headroom_meta["headroom_error"] = hr.error or "headroom sample failed"
    except Exception as exc:
        band = "unknown"
        headroom_meta = {"headroom_error": str(exc)}
    if idle_pick is not None:
        candidate = SessionCandidate(
            app=str(idle_pick.get("app") or ""),
            session_id=str(idle_pick.get("session_id") or ""),
            rss_kb=int(idle_pick.get("rss_kb") or 0),
            protected=False,
        )
        skipped = []
    else:
        # Never fall back to heaviest-active — empty candidate is correct when only
        # mid-stream work remains.
        reclaimable = [
            s
            for s in sessions
            if isinstance(s, dict) and s.get("auto_reclaim")
        ]
        if reclaimable:
            candidate, skipped = rank_candidates(
                reclaimable, protect=prot, min_rss_kb=min_rss
            )
        else:
            candidate, skipped = None, []

    phys_dict = phys.to_dict()
    phys_dict["headroom"] = headroom_meta
    active_work_only = candidate is None and any(
        str(s.get("activity_state") or s.get("activity") or "") in ("active", "interactive", "open")
        for s in sessions
        if isinstance(s, dict)
    )
    forecast = build_forecast(
        band=band,
        headroom_mb=headroom_meta.get("headroom_mb"),
        thrash_score=headroom_meta.get("thrash_score"),
        swap_used_mb=phys_dict.get("swap_used_mb"),
        swap_total_mb=phys_dict.get("swap_total_mb"),
        has_safe_candidate=candidate is not None,
        active_work_only=active_work_only,
    )
    try:
        from local_ai_monitor.resource.host_profile import append_usage_sample

        append_usage_sample(
            state=state,
            host_id=headroom_meta.get("host_id"),
            headroom_mb=headroom_meta.get("headroom_mb"),
            thrash_score=float(headroom_meta.get("thrash_score") or 0.0),
            ai_rss_mb=ai_rss / 1024.0 if ai_rss else None,
        )
    except Exception:
        pass
    phys_dict["forecast"] = forecast.to_dict()

    if band == "unknown":
        return PolicyDecision(
            ok=False,
            band=band,
            action="none",
            reason="host headroom unknown — refuse policy (unknown beats lying)",
            free_pages=phys.free_pages,
            free_mb=phys.free_mb,
            waterline_warn=warn,
            waterline_hard=hard,
            ai_session_count=len(sessions),
            ai_rss_kb=ai_rss,
            candidate=candidate,
            protected_skipped=skipped,
            physics=phys_dict,
            live_ts=live_ts,
            error=phys.error or "physics unavailable",
        )

    if band == "ok":
        return PolicyDecision(
            ok=True,
            band=band,
            action="none",
            reason=str(
                headroom_meta.get("headroom_reason")
                or "headroom OK — multitask safe"
            ),
            free_pages=phys.free_pages,
            free_mb=phys.free_mb,
            waterline_warn=warn,
            waterline_hard=hard,
            ai_session_count=len(sessions),
            ai_rss_kb=ai_rss,
            candidate=candidate,
            protected_skipped=skipped,
            physics=phys_dict,
            live_ts=live_ts,
        )

    # warn or hard — freeze-risk / thin headroom
    if candidate is None:
        return PolicyDecision(
            ok=True,
            band=band,
            action="none",
            reason=(
                str(headroom_meta.get("headroom_reason") or f"band={band}")
                + "; no safe idle reclaim target"
                + (f" (skipped protected: {', '.join(skipped)})" if skipped else "")
            ),
            free_pages=phys.free_pages,
            free_mb=phys.free_mb,
            waterline_warn=warn,
            waterline_hard=hard,
            ai_session_count=len(sessions),
            ai_rss_kb=ai_rss,
            candidate=None,
            protected_skipped=skipped,
            physics=phys_dict,
            live_ts=live_ts,
        )

    action = "suggest_end"
    hr_mb = headroom_meta.get("headroom_mb")
    thr = headroom_meta.get("thrash_score")
    reason = (
        f"freeze-risk band={band} headroom≈{hr_mb} MB thrash={thr}; "
        f"suggest reclaim {candidate.app} "
        f"({candidate.rss_kb // 1024} MB RSS) if idle"
    )
    return PolicyDecision(
        ok=True,
        band=band,
        action=action,
        reason=reason,
        free_pages=phys.free_pages,
        free_mb=phys.free_mb,
        waterline_warn=warn,
        waterline_hard=hard,
        ai_session_count=len(sessions),
        ai_rss_kb=ai_rss,
        candidate=candidate,
        protected_skipped=skipped,
        physics=phys_dict,
        live_ts=live_ts,
    )


def preflight_ok(
    *,
    state: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    require_band: str = "ok",
    free_pages_override: Optional[int] = None,
) -> PolicyDecision:
    """For scripts: pass only when headroom/freeze-risk band allows work.

    require_band (product band from sample_headroom — not free-page waterlines):
      - "ok": need green headroom (multitask safe)
      - "warn": green or yellow allowed (hard/freeze-risk fails)
    """
    d = evaluate(
        state=state, config=config, free_pages_override=free_pages_override
    )
    hr = {}
    if isinstance(d.physics, dict) and isinstance(d.physics.get("headroom"), dict):
        hr = d.physics["headroom"]
    hr_mb = hr.get("headroom_mb")
    thr = hr.get("thrash_score")
    if d.band == "unknown":
        return PolicyDecision(
            ok=False,
            band=d.band,
            action="preflight_fail",
            reason=d.reason,
            free_pages=d.free_pages,
            free_mb=d.free_mb,
            waterline_warn=d.waterline_warn,
            waterline_hard=d.waterline_hard,
            ai_session_count=d.ai_session_count,
            ai_rss_kb=d.ai_rss_kb,
            candidate=d.candidate,
            protected_skipped=d.protected_skipped,
            physics=d.physics,
            live_ts=d.live_ts,
            error=d.error,
        )
    if require_band == "warn":
        passed = d.band in ("ok", "warn")
    else:
        passed = d.band == "ok"
    if passed:
        return PolicyDecision(
            ok=True,
            band=d.band,
            action="none",
            reason=(
                f"preflight pass (headroom band={d.band}, "
                f"headroom≈{hr_mb} MB thrash={thr})"
            ),
            free_pages=d.free_pages,
            free_mb=d.free_mb,
            waterline_warn=d.waterline_warn,
            waterline_hard=d.waterline_hard,
            ai_session_count=d.ai_session_count,
            ai_rss_kb=d.ai_rss_kb,
            candidate=d.candidate,
            protected_skipped=d.protected_skipped,
            physics=d.physics,
            live_ts=d.live_ts,
        )
    need = "ok (green headroom)" if require_band != "warn" else "ok or warn (not hard)"
    return PolicyDecision(
        ok=False,
        band=d.band,
        action="preflight_fail",
        reason=(
            f"preflight fail: headroom band={d.band} "
            f"(headroom≈{hr_mb} MB thrash={thr}; need {need}). "
            f"{d.reason}"
        ),
        free_pages=d.free_pages,
        free_mb=d.free_mb,
        waterline_warn=d.waterline_warn,
        waterline_hard=d.waterline_hard,
        ai_session_count=d.ai_session_count,
        ai_rss_kb=d.ai_rss_kb,
        candidate=d.candidate,
        protected_skipped=d.protected_skipped,
        physics=d.physics,
        live_ts=d.live_ts,
    )
