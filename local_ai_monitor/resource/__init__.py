"""local-ai-rm — physics-first AI resource policy (companion to local-ai-monitor observe plane).

Reads live.json + host free pages. Recommends / ends sessions via end_session.
Does not own a second collector. Default: never auto-kill.
"""

from __future__ import annotations

from local_ai_monitor.resource.cli import cmd_resource
from local_ai_monitor.resource.physics import PhysicsSample, sample_physics
from local_ai_monitor.resource.policy import PolicyDecision, evaluate

__all__ = [
    "PhysicsSample",
    "PolicyDecision",
    "cmd_resource",
    "evaluate",
    "sample_physics",
]
