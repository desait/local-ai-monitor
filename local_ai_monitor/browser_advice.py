"""Browser pressure advice — recommend manual tab/window cleanup only.

No automatic close. No AppleScript mutation. Frontmost browser is treated as
active work and skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from typing import Any, Dict, List, Optional

from local_ai_monitor.apple_relief import top_quitable_apps


PROTECTED_APPS = {
    "Terminal",
    "iTerm2",
    "Code",
    "Visual Studio Code",
    "Cursor",
    "Claude",
    "ChatGPT",
    "Monitor",
    "Local AI Monitor Menu",
    "Finder",
    "Dock",
    "System Settings",
    "Activity Monitor",
}

BROWSER_ALIASES: Dict[str, str] = {
    "Safari": "Safari",
    "com.apple.WebKit.WebContent": "Safari",
    "com.apple.WebKit.Networking": "Safari",
    "com.apple.WebKit.GPU": "Safari",
    "com.apple.Safari.SandboxBroker": "Safari",
    "com.apple.Safari.SearchHelper": "Safari",
    "com.apple.SafariPlatformSupport.Helper": "Safari",
    "com.apple.Safari.SafeBrowsing.Service": "Safari",
    "SafariBookmarksSyncAgent": "Safari",
    "com.apple.Safari.History": "Safari",
    "SafariNotificationAgent": "Safari",
    "Google Chrome": "Google Chrome",
    "Google Chrome Helper": "Google Chrome",
    "Google Chrome Helper (Renderer)": "Google Chrome",
    "Brave Browser": "Brave",
    "Brave Browser Helper": "Brave",
    "Brave Browser Helper (Renderer)": "Brave",
    "Chromium": "Chromium",
    "Chromium Helper": "Chromium",
    "Chromium Helper (Renderer)": "Chromium",
    "Microsoft Edge": "Microsoft Edge",
    "Microsoft Edge Helper": "Microsoft Edge",
    "Microsoft Edge Helper (Renderer)": "Microsoft Edge",
    "Arc": "Arc",
    "Arc Helper": "Arc",
    "Arc Helper (Renderer)": "Arc",
    "Firefox": "Firefox",
    "firefox": "Firefox",
    "plugin-container": "Firefox",
}

CHROMIUM_APPS = {"Google Chrome", "Brave", "Chromium", "Microsoft Edge", "Arc"}


def _osascript(script: str, timeout: int = 6) -> Optional[str]:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _frontmost_app_name() -> Optional[str]:
    return _osascript(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )


def _safari_counts() -> Optional[Dict[str, int]]:
    out = _osascript(
        'tell application "Safari" to if it is running then return (count windows) & "," & (count tabs of windows)'
    )
    if not out or "," not in out:
        return None
    a, b = out.split(",", 1)
    try:
        return {"windows": int(a.strip()), "tabs": int(b.strip())}
    except ValueError:
        return None


def _chrome_counts(app: str = "Google Chrome") -> Optional[Dict[str, int]]:
    out = _osascript(
        f'tell application "{app}" to if it is running then return (count windows) & "," & (count tabs of windows)'
    )
    if not out or "," not in out:
        return None
    a, b = out.split(",", 1)
    try:
        return {"windows": int(a.strip()), "tabs": int(b.strip())}
    except ValueError:
        return None


def _ps_browser_rows() -> list[dict[str, Any]]:
    return [r for r in _ps_app_process_rows() if r.get("category") == "browser"]


def _ps_app_process_rows() -> list[dict[str, Any]]:
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pcpu=,rss=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[dict[str, Any]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            cpu = float(parts[2])
            rss = int(parts[3])
        except ValueError:
            continue
        full = parts[4]
        comm = _process_name(full)
        app = _browser_for(comm, full)
        category = "browser"
        if not app:
            app = _app_name_from_full(full)
            category = "app"
        if not app or app in PROTECTED_APPS:
            continue
        kind = _helper_kind(comm, full) if category == "browser" else "app process"
        rows.append(
            {
                "pid": pid,
                "ppid": ppid,
                "cpu_pct": cpu,
                "rss_kb": rss,
                "rss_mb": max(0, int(round(rss / 1024))),
                "comm": comm,
                "args": full[:240],
                "app": app,
                "kind": kind,
                "category": category,
            }
        )
    return rows


def _app_name_from_full(full: str) -> Optional[str]:
    marker = ".app/Contents/MacOS/"
    if marker not in full:
        return None
    left = full.split(marker, 1)[0]
    app_path = left.rsplit(" ", 1)[-1]
    name = os.path.basename(app_path)
    if not name.endswith(".app"):
        return None
    display = name[:-4]
    if not display or display in PROTECTED_APPS:
        return None
    return display


def _process_name(full: str) -> str:
    for name in sorted(BROWSER_ALIASES, key=len, reverse=True):
        if name in full:
            return name
    first = (full or "").split(" ", 1)[0]
    return os.path.basename(first)


def _browser_for(comm: str, args: str) -> Optional[str]:
    for name, app in BROWSER_ALIASES.items():
        if comm == name or name in args:
            return app
    return None


def _helper_kind(comm: str, args: str) -> str:
    low = f"{comm} {args}".lower()
    if "renderer" in low or "webcontent" in low:
        return "tab/renderer"
    if "gpu" in low:
        return "gpu helper"
    if "network" in low:
        return "network helper"
    if "audio" in low:
        return "audio helper"
    if "video" in low:
        return "video helper"
    if "crashpad" in low:
        return "crash reporter"
    if "safebrowsing" in low or "safe browsing" in low:
        return "safe browsing"
    return "browser process"


def _counts_for(app: str) -> Optional[Dict[str, int]]:
    if app == "Safari":
        return _safari_counts()
    if app == "Google Chrome":
        return _chrome_counts("Google Chrome")
    if app == "Brave":
        return _chrome_counts("Brave Browser")
    if app == "Chromium":
        return _chrome_counts("Chromium")
    if app == "Microsoft Edge":
        return _chrome_counts("Microsoft Edge")
    if app == "Arc":
        return _chrome_counts("Arc")
    return None


def browser_advice(*, min_rss_kb: int = 80 * 1024) -> Dict[str, Any]:
    frontmost = _frontmost_app_name()
    ps_rows = _ps_app_process_rows()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ps_rows:
        grouped[str(row["app"])].append(row)

    candidates: List[Dict[str, Any]] = []
    protected: List[Dict[str, Any]] = []
    for app, procs in grouped.items():
        rss_kb = sum(int(p.get("rss_kb") or 0) for p in procs)
        if rss_kb < min_rss_kb:
            continue
        category = "browser" if any(p.get("category") == "browser" for p in procs) else "app"
        counts = _counts_for(app) if category == "browser" else None
        helpers = sorted(procs, key=lambda p: int(p.get("rss_kb") or 0), reverse=True)
        renderers = [p for p in procs if p.get("kind") == "tab/renderer"]
        frontmost_known = frontmost is not None
        is_frontmost = frontmost_known and app == frontmost
        row = {
            "id": app.lower().replace(" ", "-"),
            "app": app,
            "category": category,
            "rss_kb": rss_kb,
            "rss_mb": rss_kb // 1024,
            "processes": len(procs),
            "renderer_processes": len(renderers),
            "windows": (counts or {}).get("windows"),
            "tabs": (counts or {}).get("tabs"),
            "frontmost": is_frontmost,
            "closeable": frontmost_known and not is_frontmost,
            "top_helpers": [
                {
                    "pid": h.get("pid"),
                    "rss_mb": h.get("rss_mb"),
                    "kind": h.get("kind"),
                    "name": h.get("comm"),
                }
                for h in helpers[:4]
            ],
            "recommendation": (
                "Currently active. Do not close unless you are done with it."
                if is_frontmost
                else "Active app unknown. Monitor will not close apps until it can verify focus."
                if not frontmost_known
                else (
                    "Review inactive tabs/windows; close the ones you are done with."
                    if category == "browser"
                    else "Close if you are done with this background app."
                )
            ),
        }
        if not frontmost_known or is_frontmost:
            protected.append(row)
        else:
            candidates.append(row)

    # Preserve older app-rollup behavior for browsers not visible in process aliases.
    seen = {c["app"] for c in candidates + protected}
    for app in top_quitable_apps(min_rss_kb=min_rss_kb):
        if app.name in seen:
            continue
        counts = _counts_for(app.name)
        frontmost_known = frontmost is not None
        is_frontmost = frontmost_known and app.name == frontmost
        row = {
            "id": app.name.lower().replace(" ", "-"),
            "app": app.name,
            "category": "browser",
            "rss_mb": app.rss_kb // 1024,
            "processes": len(app.pids),
            "renderer_processes": None,
            "windows": (counts or {}).get("windows"),
            "tabs": (counts or {}).get("tabs"),
            "frontmost": is_frontmost,
            "closeable": frontmost_known and not is_frontmost,
            "top_helpers": [],
            "recommendation": (
                "Active app unknown. Monitor will not close apps until it can verify focus."
                if not frontmost_known
                else "Review inactive tabs/windows; close the ones you are done with."
            ),
        }
        if not frontmost_known or is_frontmost:
            protected.append(row)
        else:
            candidates.append(row)

    candidates.sort(key=lambda x: int(x.get("rss_mb") or 0), reverse=True)
    protected.sort(key=lambda x: int(x.get("rss_mb") or 0), reverse=True)
    total_mb = sum(int(c.get("rss_mb") or 0) for c in candidates)
    return {
        "ok": True,
        "frontmost": frontmost,
        "candidates": candidates,
        "protected": protected,
        "summary": {
            "candidate_count": len(candidates),
            "candidate_mb": total_mb,
            "protected_count": len(protected),
        },
        "message": (
            "No inactive browser load found."
            if not candidates
            else (
                f"Review {candidates[0]['app']} · {candidates[0].get('rss_mb') or 0} MB"
                if len(candidates) == 1
                else f"Review {len(candidates)} apps · {total_mb} MB"
            )
        ),
        "detail": "Manual cleanup only. Monitor asks before closing apps.",
    }


def quit_inactive_app(name: str) -> Dict[str, Any]:
    frontmost = _frontmost_app_name()
    if frontmost is None:
        return {
            "ok": False,
            "acted": False,
            "app": name,
            "message": "Active app unknown; refusing to close apps.",
        }
    if not name or name in PROTECTED_APPS:
        return {"ok": False, "acted": False, "app": name, "message": f"{name} is protected."}
    if frontmost == name:
        return {"ok": False, "acted": False, "app": name, "message": f"{name} is active now."}
    script = f'tell application "{name}" to quit'
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
        return {
            "ok": ok,
            "acted": ok,
            "app": name,
            "message": f"Asked {name} to quit." if ok else f"Could not quit {name}.",
            "error": None if ok else (r.stderr or r.stdout or "")[:200],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "acted": False, "app": name, "message": f"Could not quit {name}.", "error": str(exc)}


def quit_inactive_apps(names: list[str]) -> Dict[str, Any]:
    results = [quit_inactive_app(n) for n in names]
    acted = [r for r in results if r.get("acted")]
    return {
        "ok": True,
        "acted": bool(acted),
        "results": results,
        "message": f"Asked {len(acted)} app(s) to quit." if acted else "No apps were closed.",
    }


def _legacy_browser_advice(*, min_rss_kb: int = 300 * 1024) -> Dict[str, Any]:
    frontmost = _frontmost_app_name()
    apps = top_quitable_apps(min_rss_kb=min_rss_kb)
    rows: List[Dict[str, Any]] = []
    for app in apps:
        if app.name == frontmost:
            continue
        counts = _counts_for(app.name)
        rows.append(
            {
                "app": app.name,
                "rss_mb": app.rss_kb // 1024,
                "windows": (counts or {}).get("windows"),
                "tabs": (counts or {}).get("tabs"),
                "recommendation": "Manually close inactive tabs/windows if you are not using them.",
            }
        )
    return {
        "ok": True,
        "frontmost": frontmost,
        "candidates": rows,
        "message": (
            "No inactive browser hogs found"
            if not rows
            else "Close inactive browser tabs/windows before starting more AI work"
        ),
    }


def cmd_browser_advice(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-monitor browser-advice",
        description="Recommend inactive browser tabs/windows to close manually.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--min-rss-mb", type=int, default=300)
    p.add_argument("--quit-app", action="append", default=[], help="Gracefully quit an inactive app by display name")
    p.add_argument("--quit-all", action="store_true", help="Gracefully quit all current inactive candidates")
    args = p.parse_args(list(argv or []))
    if args.quit_app:
        out = quit_inactive_apps(list(args.quit_app))
    elif args.quit_all:
        status = browser_advice(min_rss_kb=max(1, args.min_rss_mb) * 1024)
        names = [str(c.get("app")) for c in status.get("candidates") or [] if c.get("closeable", True)]
        out = quit_inactive_apps(names)
    else:
        out = browser_advice(min_rss_kb=max(1, args.min_rss_mb) * 1024)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(out["message"])
        for c in out.get("candidates") or []:
            tabs = c.get("tabs")
            tabs_s = f", tabs={tabs}" if tabs is not None else ""
            print(f"  {c.get('app')}: {c.get('rss_mb')} MB{tabs_s}")
    return 0 if out.get("ok") else 1
