"""Thrash action ladder — pause → mute → soft-stop unused bg → force (last).

Product law (2026-07-30; restated):
  - Act only on freeze-risk / thrash, not low free pages.
  - Do **not** suddenly shut down apps the user is using (all user work).
  - Exception: **unused background** (idle services, OS cache helpers).
  - Never touch WindowServer / Finder / loginwindow / local-ai-monitor stack.

One step per manage tick (escalates only if thrash still elevated after cooldown).
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Dict, List, Optional, Sequence

from local_ai_monitor.activity import pick_reclaim_candidate
from local_ai_monitor.soft_stop import soft_stop_session

# Escalation steps (index = step)
STEP_PAUSE = 0  # renice +15 (helpers only)
STEP_MUTE = 1  # renice +19 + background policy
STEP_SOFT_STOP = 2  # idle service soft-stop only
STEP_FORCE = 3  # SIGTERM unused OS cache helpers only
STEP_APPLE = 4  # graceful quit large browsers (opt-in; user may still be using them)

# May renice under thrash (helpers — not "quit my browser")
PAUSE_HOG_SUBSTRINGS = (
    "ThumbnailsAgent",
    "WebThumbnailExtension",
    "quicklookd",
    "photoanalysisd",
    "cloudphotod",
    "mediaanalysisd",
    "Google Chrome Helper",
    "Chrome Helper",
    "Chromium Helper",
    "plugin-container",  # Firefox helper
)

# May SIGTERM only when thrash continues (never whole Safari/Chrome apps here)
FORCE_HOG_SUBSTRINGS = (
    "ThumbnailsAgent",
    "WebThumbnailExtension",
    "quicklookd",
    "photoanalysisd",
    "cloudphotod",
    "mediaanalysisd",
)

# Never signal / renice these (system + our stack)
SACRED_SUBSTRINGS = (
    "WindowServer",
    "loginwindow",
    "Finder",
    "Dock",
    "SystemUIServer",
    "kernel_task",
    "launchd",
    "local-ai-monitor",
    "local_ai_monitor",
    "local-ai-monitord",
    "local-ai-monitor-litebar",
    "local-ai-monitor-menubar",
    "local-ai-monitor-sensor",
)


def _is_sacred(cmd: str) -> bool:
    c = cmd or ""
    low = c.lower()
    for s in SACRED_SUBSTRINGS:
        if s.lower() in low:
            return True
    # User-facing AI binaries / paths — never thrash-kill
    if any(
        x in low
        for x in (
            "/bin/grok",
            "/.grok/",
            "claude",
            "codex",
            "cursor",
            "windsurf",
            "chatgpt",
            "openclaw",
        )
    ):
        # openclaw gateway is soft-stopped via session path, not SIGTERM-by-name here
        if "openclaw" in low:
            return True
        if any(x in low for x in ("grok", "claude", "codex", "cursor", "windsurf", "chatgpt")):
            return True
    return False


def _matches(cmd: str, needles: Sequence[str]) -> bool:
    if _is_sacred(cmd):
        return False
    for s in needles:
        if s in cmd:
            return True
    return False


def list_hog_pids(
    *,
    min_rss_kb: int = 80 * 1024,
    limit: int = 6,
    needles: Sequence[str] = PAUSE_HOG_SUBSTRINGS,
) -> List[Dict[str, Any]]:
    """Heaviest matching hogs from ps (rss KB). Allowlist only — no random apps."""
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,rss=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: List[Dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2].strip()
        if rss < min_rss_kb:
            continue
        if not _matches(cmd, needles):
            continue
        rows.append({"pid": pid, "rss_kb": rss, "cmd": cmd[:200]})
    rows.sort(key=lambda x: int(x["rss_kb"]), reverse=True)
    return rows[:limit]


def _renice(pid: int, nice: int) -> bool:
    try:
        r = subprocess.run(
            ["renice", f"+{int(nice)}", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _taskpolicy_background(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["taskpolicy", "-b", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            return True
        r2 = subprocess.run(
            ["taskpolicy", "-c", "utility", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return r2.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _sigterm(pid: int) -> bool:
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def pause_hogs(*, min_rss_kb: int = 80 * 1024, dry_run: bool = False) -> Dict[str, Any]:
    hogs = list_hog_pids(min_rss_kb=min_rss_kb, limit=5, needles=PAUSE_HOG_SUBSTRINGS)
    touched = []
    for h in hogs:
        pid = int(h["pid"])
        if dry_run:
            touched.append({**h, "action": "would_renice+15"})
            continue
        ok = _renice(pid, 15)
        touched.append({**h, "action": "renice+15", "ok": ok})
    return {
        "ok": True,
        "step": "pause",
        "acted": bool(touched) and not dry_run,
        "dry_run": dry_run,
        "targets": touched,
        "message": (
            f"Paused unused background helpers ({len(touched)})"
            if touched
            else "No unused background helpers to pause"
        ),
    }


def mute_hogs(*, min_rss_kb: int = 80 * 1024, dry_run: bool = False) -> Dict[str, Any]:
    hogs = list_hog_pids(min_rss_kb=min_rss_kb, limit=5, needles=PAUSE_HOG_SUBSTRINGS)
    touched = []
    for h in hogs:
        pid = int(h["pid"])
        if dry_run:
            touched.append({**h, "action": "would_mute"})
            continue
        ok_n = _renice(pid, 19)
        ok_t = _taskpolicy_background(pid)
        touched.append({**h, "action": "mute", "renice": ok_n, "taskpolicy": ok_t})
    return {
        "ok": True,
        "step": "mute",
        "acted": bool(touched) and not dry_run,
        "dry_run": dry_run,
        "targets": touched,
        "message": (
            f"Muted unused background helpers ({len(touched)})"
            if touched
            else "No unused background helpers to mute"
        ),
    }


def soft_stop_idle_service(
    sessions: Sequence[Dict[str, Any]],
    *,
    protect: Sequence[str],
    min_rss_kb: int,
    state: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    cand = pick_reclaim_candidate(
        list(sessions),
        protect=protect,
        min_rss_kb=min_rss_kb,
        prefer_services=True,
    )
    if cand is None:
        return {
            "ok": False,
            "step": "soft_stop",
            "acted": False,
            "message": "No unused background service to soft-stop",
        }
    is_svc = cand.get("activity_state") == "idle_service" or cand.get("kind") == "service"
    app = str(cand.get("app") or "")
    if app in set(protect) or not cand.get("auto_reclaim"):
        return {
            "ok": False,
            "step": "soft_stop",
            "acted": False,
            "refused": True,
            "app": app,
            "message": f"Refused soft-stop of {app} (user work or protected)",
        }
    if not is_svc:
        return {
            "ok": False,
            "step": "soft_stop",
            "acted": False,
            "message": "Candidate is not an unused background service",
        }
    sid = str(cand.get("session_id") or "")
    r = soft_stop_session(
        app, sid, state=state, session_row=cand, dry_run=dry_run, force=False
    )
    r["step"] = "soft_stop"
    r["acted"] = bool(r.get("ok")) and not r.get("refused") and not dry_run
    return r


def force_term_safe_hogs(
    *,
    min_rss_kb: int = 100 * 1024,
    dry_run: bool = False,
    max_pids: int = 3,
) -> Dict[str, Any]:
    """SIGTERM only unused OS cache helpers — never whole user apps."""
    hogs = list_hog_pids(min_rss_kb=min_rss_kb, limit=10, needles=FORCE_HOG_SUBSTRINGS)
    hogs = hogs[:max_pids]
    touched = []
    for h in hogs:
        pid = int(h["pid"])
        if dry_run:
            touched.append({**h, "action": "would_sigterm"})
            continue
        ok = _sigterm(pid)
        touched.append({**h, "action": "sigterm", "ok": ok})
    return {
        "ok": True,
        "step": "force",
        "acted": bool(touched) and not dry_run,
        "dry_run": dry_run,
        "targets": touched,
        "message": (
            f"Stopped unused cache helpers ({len(touched)})"
            if touched
            else "No unused cache helpers to force-stop"
        ),
    }


def run_ladder_step(
    step: int,
    *,
    sessions: Sequence[Dict[str, Any]],
    protect: Sequence[str],
    min_rss_kb: int,
    state: Optional[str] = None,
    apple_relief: bool = False,
    apple_min_rss_kb: int = 1024 * 1024,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if step <= STEP_PAUSE:
        return pause_hogs(dry_run=dry_run)
    if step == STEP_MUTE:
        return mute_hogs(dry_run=dry_run)
    if step == STEP_SOFT_STOP:
        return soft_stop_idle_service(
            sessions,
            protect=protect,
            min_rss_kb=min_rss_kb,
            state=state,
            dry_run=dry_run,
        )
    if step == STEP_FORCE:
        return force_term_safe_hogs(dry_run=dry_run)
    if step >= STEP_APPLE:
        if not apple_relief:
            return {
                "ok": False,
                "step": "apple",
                "acted": False,
                "message": "apple_relief disabled (would quit user browsers — opt-in only)",
            }
        try:
            from local_ai_monitor.apple_relief import relieve_apple_apps

            ar = relieve_apple_apps(
                min_rss_kb=apple_min_rss_kb, max_apps=1, dry_run=dry_run
            )
            ar["step"] = "apple"
            ar["acted"] = bool(ar.get("acted"))
            return ar
        except Exception as exc:
            return {
                "ok": False,
                "step": "apple",
                "acted": False,
                "error": str(exc),
            }
    return {"ok": False, "step": "unknown", "acted": False, "message": f"bad step {step}"}


def next_step_after(prev_step: Optional[int], thrash_still_high: bool) -> int:
    if not thrash_still_high:
        return STEP_PAUSE
    if prev_step is None:
        return STEP_PAUSE
    return min(int(prev_step) + 1, STEP_APPLE)
