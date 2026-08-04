"""Graceful Apple / user-app memory relief under hard free-page pressure.

Works *with* macOS: quit apps cleanly via AppleScript (not SIGKILL on
system processes). Never touches Finder, WindowServer, loginwindow, etc.

Only apps the user can relaunch in one click.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Display name → bundle id. Quit via AppleScript when hogging under hard band.
# This is opt-in only. Keep the set narrow: browser-style apps are more
# recoverable than Mail, Messages, Xcode, Photos, or other active-work apps.
QUITABLE_APPS: Dict[str, str] = {
    "Safari": "com.apple.Safari",
    "Google Chrome": "com.google.Chrome",
    "Chromium": "org.chromium.Chromium",
    "Firefox": "org.mozilla.firefox",
}

# Never signal or quit these (kernel / session critical).
_NEVER_COMM = frozenset(
    {
        "kernel_task",
        "launchd",
        "WindowServer",
        "loginwindow",
        "Finder",
        "Dock",
        "SystemUIServer",
        "cfprefsd",
        "distnoted",
        "UserEventAgent",
        "local-ai-monitor-menubar",
        "Python",  # avoid nuking collectors / agents wholesale by name alone
    }
)


@dataclass
class AppRss:
    name: str
    bundle_id: Optional[str]
    rss_kb: int
    pids: List[int]


def _ps_rss_by_comm() -> List[Tuple[str, int, int]]:
    """Return list of (comm, pid, rss_kb)."""
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,rss=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
    except (OSError, subprocess.TimeoutExpired):
        return []
    out: List[Tuple[str, int, int]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1])  # KB on macOS ps
            comm = parts[2].strip()
        except ValueError:
            continue
        # basename
        base = comm.rsplit("/", 1)[-1]
        out.append((base, pid, rss))
    return out


def _frontmost_app_name() -> Optional[str]:
    """Best-effort active app name. Unknown => fail closed at candidate time."""
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return None
        name = (r.stdout or "").strip()
        return name or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _match_quitable(comm: str) -> Optional[Tuple[str, str]]:
    """Map process comm to (display_name, bundle_id) if quitable."""
    base = comm.rsplit("/", 1)[-1]
    if base in _NEVER_COMM:
        return None
    # Direct name match
    for name, bid in QUITABLE_APPS.items():
        if base == name or base.lower() == name.lower().replace(" ", ""):
            return name, bid
        if name.replace(" ", "") in base.replace(" ", ""):
            return name, bid
    # Common binaries
    aliases = {
        "Safari": ("Safari", "com.apple.Safari"),
        "Google Chrome": ("Google Chrome", "com.google.Chrome"),
        "Google Chrome Helper": ("Google Chrome", "com.google.Chrome"),
        "firefox": ("Firefox", "org.mozilla.firefox"),
    }
    if base in aliases:
        return aliases[base]
    return None


def top_quitable_apps(min_rss_kb: int = 200 * 1024) -> List[AppRss]:
    """Aggregate RSS for quitable apps (helpers roll up to parent name)."""
    agg: Dict[str, AppRss] = {}
    frontmost = _frontmost_app_name()
    for comm, pid, rss in _ps_rss_by_comm():
        hit = _match_quitable(comm)
        if not hit:
            continue
        name, bid = hit
        if frontmost is None or name == frontmost:
            continue
        cur = agg.get(name)
        if cur is None:
            agg[name] = AppRss(name=name, bundle_id=bid, rss_kb=rss, pids=[pid])
        else:
            cur.rss_kb += rss
            cur.pids.append(pid)
    rows = [a for a in agg.values() if a.rss_kb >= min_rss_kb]
    rows.sort(key=lambda a: a.rss_kb, reverse=True)
    return rows


def quit_app(name: str, bundle_id: Optional[str] = None) -> Dict[str, Any]:
    """Graceful quit via AppleScript. Does not SIGKILL."""
    bid = bundle_id or QUITABLE_APPS.get(name)
    # Prefer quit by name (user-facing)
    script = f'tell application "{name}" to quit'
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        ok = r.returncode == 0
        return {
            "ok": ok,
            "name": name,
            "bundle_id": bid,
            "method": "applescript_quit",
            "error": None if ok else (r.stderr or r.stdout or "quit failed")[:200],
            "message": f"Asked {name} to quit" if ok else f"Could not quit {name}",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "name": name,
            "bundle_id": bid,
            "method": "applescript_quit",
            "error": str(exc),
            "message": f"Could not quit {name}",
        }


def relieve_apple_apps(
    *,
    min_rss_kb: int = 200 * 1024,
    max_apps: int = 1,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Quit top quitable memory hogs (one tick)."""
    apps = top_quitable_apps(min_rss_kb=min_rss_kb)
    if not apps:
        return {"ok": True, "acted": False, "candidates": [], "results": []}
    cands = [
        {"name": a.name, "rss_mb": a.rss_kb // 1024, "bundle_id": a.bundle_id}
        for a in apps[:5]
    ]
    results: List[Dict[str, Any]] = []
    if dry_run:
        return {
            "ok": True,
            "acted": False,
            "dry_run": True,
            "candidates": cands,
            "results": [],
        }
    for a in apps[: max(1, max_apps)]:
        results.append(quit_app(a.name, a.bundle_id))
    acted = any(r.get("ok") for r in results)
    return {
        "ok": True,
        "acted": acted,
        "candidates": cands,
        "results": results,
        "message": results[0].get("message") if results else None,
    }
