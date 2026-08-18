"""Runway — the product control plane for this Mac.

First principles
----------------
A non-technical person does not manage pages, swap, tmux, or PIDs. They
manage *room to work*. Runway answers four questions and nothing else:

1. Can I start more work?
2. What is using this Mac, in names I recognize?
3. What is the one next step?
4. Will you close my work?  (No. Active work is never closed for you.)

The observe plane (collector, physics, headroom, activity) stays where it
is. Runway does not own a second sampler and does not change LaunchAgent
labels, state paths, or the running local install. It *composes* those
engines into one card and owns every user-facing sentence.

States (mapped from the existing forecast, never invented):

  open     — room to work; starts allowed
  watch    — getting full; starts still allowed
  hold     — do not start more heavy work
  protect  — this Mac is under strain; active work stays open
  unknown  — refuse to guess

Existing ``local-ai-monitor`` commands keep working. ``runway`` is the
everyday surface. Expert modes (tui / dash / resource) stay expert.
"""

from __future__ import annotations

from local_ai_monitor.runway.compose import compose_card
from local_ai_monitor.runway.model import RunwayCard, brand

__all__ = ["RunwayCard", "brand", "compose_card"]
