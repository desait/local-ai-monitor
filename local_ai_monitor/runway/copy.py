"""Every sentence a person sees. No pages, PIDs, tmux, or waterlines."""

from __future__ import annotations

from typing import Any, Optional

from local_ai_monitor.runway.model import brand

PROMISE = "Active work is never closed for you."

# Words that must never appear in primary copy (tests + product law).
_FORBIDDEN = (
    "pid:",
    "tmux",
    "free pages",
    "waterline",
    "session_id",
    "almost out of free memory",
    "memory is getting tight",
)


def room_phrase(headroom_mb: Optional[int]) -> str:
    if headroom_mb is None:
        return "room on this Mac"
    if headroom_mb >= 1024:
        gb = headroom_mb / 1024.0
        return f"about {gb:.1f} GB of room"
    if headroom_mb >= 1:
        return f"about {headroom_mb} MB of room"
    return "very little room"


def title_for(state: str) -> str:
    return {
        "open": "You can keep working",
        "watch": "Keep an eye on this Mac",
        "hold": "Do not start more heavy work",
        "protect": "Protect the work that is open",
        "unknown": "Runway cannot read this Mac yet",
    }.get(state, "Runway cannot read this Mac yet")


def sentence_for(state: str, *, can_start: bool, stale: bool = False) -> str:
    if stale:
        return "The background watcher is not updating."
    if state == "open":
        return "You can start more work."
    if state == "watch":
        return "You can still start more work."
    if state == "hold":
        return "Do not open another heavy app yet."
    if state == "protect":
        return "This Mac is under strain. Your open work stays safe."
    if not can_start:
        return "Wait until Runway can read this Mac."
    return "Runway cannot tell if this Mac has room."


def detail_for(
    state: str,
    *,
    headroom_mb: Optional[int] = None,
    candidate_name: Optional[str] = None,
    ai_mb: Optional[int] = None,
    stale: bool = False,
) -> str:
    if stale:
        return (
            "Look for Runway in the menu bar. "
            "If it is missing, open Terminal and run: local-ai-monitor install"
        )
    room = room_phrase(headroom_mb)
    ai = ""
    if ai_mb and ai_mb > 0:
        if ai_mb >= 1024:
            ai = f" AI tools are using about {ai_mb / 1024.0:.1f} GB."
        else:
            ai = f" AI tools are using about {ai_mb} MB."

    if state == "open":
        return f"This Mac has {room}.{ai} Starts are safe."
    if state == "watch":
        return (
            f"This Mac still has {room}.{ai} "
            "Starts are allowed — just do not pile on several heavy apps at once."
        )
    if state == "hold":
        if candidate_name:
            return (
                f"This Mac has {room}.{ai} "
                f"Pause unused background work (for example “{candidate_name}”) "
                "before opening more. Mid-stream work stays open."
            )
        return (
            f"This Mac has {room}.{ai} "
            "Pause unused background work before opening more. "
            "Mid-stream work stays open."
        )
    if state == "protect":
        if candidate_name:
            return (
                f"This Mac is shuffling memory to keep up.{ai} "
                f"Unused background work such as “{candidate_name}” can be paused. "
                "Mid-stream work is never auto-killed."
            )
        return (
            f"This Mac is shuffling memory to keep up.{ai} "
            "Everything still running looks like your work, "
            "so nothing will be closed for you. Mid-stream work stays open."
        )
    return (
        "Capacity is unknown, so Runway will not guess. "
        "Avoid starting another heavy app until the watcher recovers."
    )


def next_step_for(
    action: str,
    *,
    candidate_name: Optional[str] = None,
    stale: bool = False,
) -> str:
    if stale:
        return "Wait for the menu bar icon to update, or run local-ai-monitor install."
    if action == "nothing":
        return "Nothing to do."
    if action == "avoid_start":
        return "Do not open another heavy app just yet."
    if action == "pause_idle":
        if candidate_name:
            return f"You may pause unused “{candidate_name}”. Your chats stay open."
        return "You may pause unused background work. Your chats stay open."
    if action == "protect_work":
        return "Leave everything open. Pause a session yourself only if the Mac feels stuck."
    return "Wait. Runway will not act while it cannot read this Mac."


def action_label_for(action: str) -> str:
    if action == "pause_idle":
        return "Pause unused"
    if action == "protect_work":
        return "Leave work open"
    if action == "avoid_start":
        return "Hold new work"
    return "OK"


def role_for(activity_state: Optional[str], *, kind: str = "") -> str:
    act = (activity_state or "").lower()
    if act in ("idle_service",) or kind == "service":
        return "background"
    if act in ("active", "interactive", "user_work", "open"):
        return "your_work"
    if act in ("quiet",):
        return "quiet"
    return "your_work"


def assert_clean(text: str) -> str:
    """Guardrail: primary copy must stay human. Empty strings pass."""
    low = (text or "").lower()
    for bad in _FORBIDDEN:
        if bad in low:
            raise ValueError(f"runway copy leaked {bad!r}: {text}")
    return text


def home_lines(card: Any) -> str:
    """Bare `runway` / `local-ai-monitor` — never opens tmux."""
    b = brand()
    lines = [
        "",
        f"  {b} is watching in your menu bar.",
        f"  Look for:  {card.chip}",
        "",
        f"  {card.sentence}",
        f"  {card.detail}",
        "",
        f"  Next: {card.next_step}",
        f"  {card.promise}",
        "",
        "  Click that menu bar item for the simple list and actions.",
        "  Friendly full list:  runway watch",
        "  Expert Terminal:     local-ai-monitor tui",
        "",
    ]
    return "\n".join(lines)


def watch_lines(card: Any) -> str:
    """Full Terminal list. No tmux. Q to quit (caller loop)."""
    b = brand()
    lines = [
        "",
        f"  {b}",
        "  " + "─" * max(6, len(b)),
        "",
        f"  {card.chip}",
        f"  {card.sentence}",
        f"  {card.detail}",
        "",
    ]
    if card.tools:
        lines.append("  By tool")
        for t in card.tools:
            mark = "  " if t.role != "background" else "* "
            lines.append(f"    {mark}{t.name:<16}  {t.load:<10}  {t.mem}")
        if any(t.role == "background" for t in card.tools):
            lines.append("    * unused background — safe to pause if this Mac is strained")
    elif card.stale:
        lines.append("  Background watcher is not updating.")
        lines.append("  Try: local-ai-monitor install")
    else:
        lines.append("  No AI tools are using noticeable resources right now.")
    lines.append("")
    lines.append(f"  Next: {card.next_step}")
    lines.append(f"  {card.promise}")
    lines.append("")
    lines.append("  Press Q to close")
    lines.append("  The menu bar icon keeps watching in the background.")
    lines.append("")
    return "\n".join(lines)
