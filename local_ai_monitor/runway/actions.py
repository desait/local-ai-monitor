"""Confirm-gated actions. Never closes mid-stream work."""

from __future__ import annotations

from typing import Any, Dict, Optional

from local_ai_monitor.resource.audit import append_audit
from local_ai_monitor.runway.compose import compose_card
from local_ai_monitor.runway.model import RunwayCard


def pause_idle(
    *,
    state: Optional[str] = None,
    yes: bool = False,
    dry_run: bool = False,
    card: Optional[RunwayCard] = None,
) -> Dict[str, Any]:
    """Pause the unused background candidate. Refuses without --yes.

    Uses the existing end-session path. Never falls back to heaviest-active.
    """
    card = card if card is not None else compose_card(state=state)
    if card.action != "pause_idle":
        return {
            "ok": True,
            "acted": False,
            "skipped": card.action,
            "message": card.next_step,
            "card": card.to_dict(),
        }
    if not card.candidate_app or not card.candidate_session_id:
        return {
            "ok": True,
            "acted": False,
            "skipped": "no_idle_candidate",
            "message": "No unused background work to pause.",
            "card": card.to_dict(),
        }
    if not yes and not dry_run:
        return {
            "ok": False,
            "acted": False,
            "error": "refusing pause without --yes (confirm before closing unused work)",
            "candidate_label": card.candidate_label,
            "card": card.to_dict(),
        }

    from local_ai_monitor.end_session import end_session

    result = end_session(
        card.candidate_app,
        card.candidate_session_id,
        state=state,
        dry_run=dry_run,
    )
    append_audit(
        "runway_dry_run" if dry_run else "runway_pause",
        {
            "state": card.state,
            "action": card.action,
            "app": result.get("app"),
            "session_id": result.get("session_id"),
            "pids": result.get("pids"),
            "ok": result.get("ok"),
            "error": result.get("error"),
            "dry_run": dry_run,
        },
        state=state,
    )
    out = dict(result)
    out["acted"] = bool(result.get("ok"))
    out["card"] = card.to_dict()
    return out
