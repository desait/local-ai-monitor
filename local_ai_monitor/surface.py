"""Session surface metadata: openability + human activity (why this uses RAM).

Deterministic: a session is openable or not — never “check Terminal or the app.”
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_PORT_RE = re.compile(r":(\d{2,5})\b")


def _port_from_label(label: str) -> Optional[str]:
    m = _PORT_RE.search(label or "")
    return m.group(1) if m else None


def session_surface(
    app: str,
    session_id: str = "",
    label: str = "",
    detail: str = "",
    *,
    cpu_pct: float = 0.0,
    rss_kb: int = 0,
) -> Dict[str, Any]:
    """Return kind / openable / activity for menu bar + live.json."""
    app = app or ""
    sid = session_id or ""
    lab = label or ""
    det = detail or ""
    mb = max(1, int(round(rss_kb / 1024.0))) if rss_kb > 0 else 0
    idle = cpu_pct < 0.5

    # --- Background services (LaunchAgent / daemon) — no UI to open ---
    if app == "OpenClaw" or sid.startswith("svc:"):
        port = _port_from_label(lab) or _port_from_label(det)
        port_bit = f"port {port}" if port else "local network port"
        load = "nearly idle" if idle else f"using CPU (~{cpu_pct:.0f}%)"
        activity = (
            f"Background gateway on {port_bit}. "
            f"Kept alive by LaunchAgent even when you are not using OpenClaw. "
            f"Using about {mb} MB · {load}. "
            f"Quarantine stops it until you allow it again."
        )
        return {
            "kind": "service",
            "openable": False,
            "open_denied": "Background gateway — no app window or Terminal session",
            "activity": activity,
            "title_hint": "Gateway",
            "subtitle_hint": f"Background service · {port_bit}" if port else "Background service",
        }

    if app == "Claude Desktop" or sid.startswith("app:claude"):
        return {
            "kind": "desktop",
            "openable": True,
            "open_target": "app",
            "open_denied": None,
            "activity": f"Co-Work desktop app · about {mb} MB",
            "title_hint": None,
            "subtitle_hint": "Co-Work app",
        }

    if app == "ChatGPT" or sid.startswith("app:chatgpt"):
        return {
            "kind": "desktop",
            "openable": True,
            "open_target": "app",
            "open_denied": None,
            "activity": f"ChatGPT desktop app · about {mb} MB",
            "title_hint": None,
            "subtitle_hint": "ChatGPT app",
        }

    if app == "Cursor":
        return {
            "kind": "desktop",
            "openable": True,
            "open_target": "app",
            "open_denied": None,
            "activity": f"Cursor editor · about {mb} MB",
            "title_hint": None,
            "subtitle_hint": "Cursor app",
        }

    if app == "Buzz":
        # Desktop + agents — openable only when we have a path or can activate app
        has_path = det.startswith("/") or "path=" in det
        return {
            "kind": "agent",
            "openable": True,
            "open_target": "app" if not has_path else "folder",
            "open_denied": None,
            "activity": f"Buzz worker · about {mb} MB" + (" · quiet" if idle else ""),
            "title_hint": None,
            "subtitle_hint": None,
        }

    # CLI tools: openable only if we can match a Terminal window or have a cwd
    if app in ("Grok", "Claude CLI", "Codex", "OpenAI CLI"):
        has_cwd = det.startswith("/")
        if has_cwd:
            return {
                "kind": "cli",
                "openable": True,
                "open_target": "terminal",
                "open_denied": None,
                "activity": (
                    f"Terminal session in {(det.rstrip('/').split('/')[-1])} · about {mb} MB"
                ),
                "title_hint": None,
                "subtitle_hint": None,
            }
        # pid-only session without cwd — cannot deterministically open
        return {
            "kind": "cli",
            "openable": False,
            "open_target": None,
            "open_denied": "No folder or Terminal window linked — cannot open from here",
            "activity": f"CLI process · about {mb} MB" + (" · quiet" if idle else ""),
            "title_hint": None,
            "subtitle_hint": "Running · no open target",
        }

    # Unknown: fail closed on open
    return {
        "kind": "unknown",
        "openable": False,
        "open_target": None,
        "open_denied": "No known window or app for this session",
        "activity": f"About {mb} MB" if mb else "Running",
        "title_hint": None,
        "subtitle_hint": None,
    }


def enrich_live_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate/copy a live session row with surface fields."""
    out = dict(row)
    surf = session_surface(
        str(out.get("app") or ""),
        str(out.get("session_id") or ""),
        str(out.get("label") or ""),
        str(out.get("detail") or ""),
        cpu_pct=float(out.get("cpu_pct") or 0),
        rss_kb=int(out.get("rss_kb") or 0),
    )
    out["kind"] = surf["kind"]
    out["openable"] = bool(surf["openable"])
    out["open_denied"] = surf.get("open_denied")
    out["open_target"] = surf.get("open_target")
    out["activity"] = surf.get("activity")
    if surf.get("title_hint"):
        out["title_hint"] = surf["title_hint"]
    if surf.get("subtitle_hint"):
        out["subtitle_hint"] = surf["subtitle_hint"]
    return out
