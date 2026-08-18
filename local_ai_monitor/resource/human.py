"""Plain-English resource policy for menu bar (non-technical).

Owned by Runway. This module is a thin adapter so live.json and the
menu bar keep the same keys they already read.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from local_ai_monitor.resource.policy import PolicyDecision, evaluate
from local_ai_monitor.runway.compose import card_from_decision, compose_card


def humanize_decision(d: PolicyDecision) -> Dict[str, Any]:
    """Menu-bar / notification payload. Built by the Runway card."""
    return card_from_decision(d).to_resource_block()


def resource_live_block(
    *,
    state: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if config is not None:
        d = evaluate(state=state, config=config)
        return card_from_decision(d).to_resource_block()
    return compose_card(state=state, stale=False).to_resource_block()
