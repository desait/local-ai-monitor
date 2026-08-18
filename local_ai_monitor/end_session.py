"""End (kill) AI tool sessions safely.

Used by CLI, menu bar, and local-ai-rm self-manage.
Prefer live.json pids when present; re-scan if missing.

LaunchAgent-backed tools (e.g. OpenClaw gateway KeepAlive) must be
**bootout** before SIGTERM/SIGKILL or launchd restarts them — looks like
“kill failed” in the UI.

Never touch local-ai-monitor self, system pids, or unrelated trees.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from local_ai_monitor.store import live_path, read_json

# Hard floor: never signal these (and never expand into launchd/init).
_PROTECTED_PIDS = frozenset({0, 1})
_SELF_MARKERS = (
    "local_ai_monitor",
    "local-ai-monitor",
    "local-ai-rm",
    "Local AI Monitor Menu",
    "local-ai-monitor-menubar",
    "com.user.local-ai-monitor",
    "runway",
)

# "Heavy" absolute floor (MB). On 8 GB Macs, <1 GB is normal — not an emergency.
# Scaled upward for larger RAM in heavy_rss_mb().
HEAVY_RSS_MB = 1024


def heavy_rss_mb(memsize_bytes: Optional[int] = None) -> int:
    """Heavy badge threshold: max(1 GB, ~15% of physical RAM)."""
    import subprocess

    mem = memsize_bytes
    if mem is None:
        try:
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            mem = int((r.stdout or "").strip()) if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            mem = None
    if not mem or mem <= 0:
        return HEAVY_RSS_MB
    gb = mem / (1024.0**3)
    # 8 GB → ~1229 MB; floor 1024. 16 GB → ~2458 MB.
    scaled = int(round(gb * 1024 * 0.15))
    return max(HEAVY_RSS_MB, scaled)

# App / session → launchd labels that KeepAlive-restart the process.
# Ending the session without bootout is a no-op under memory pressure.
_LAUNCHD_BY_APP: Dict[str, List[str]] = {
    "OpenClaw": ["ai.openclaw.gateway"],
}


def _cmdline(pid: int) -> str:
    try:
        # macOS: ps -p PID -o command=
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _is_self_cmd(cmd: str) -> bool:
    c = cmd or ""
    return any(m in c for m in _SELF_MARKERS)


def _alive(pid: int) -> bool:
    if pid in _PROTECTED_PIDS:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    except OSError:
        return False


def _pids_from_live(
    app: str,
    session_id: Optional[str] = None,
    *,
    state: Optional[str] = None,
) -> List[int]:
    live = read_json(live_path(state)) or {}
    sessions = live.get("sessions") or []
    out: List[int] = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        if s.get("app") != app:
            continue
        if session_id is not None and s.get("session_id") != session_id:
            continue
        pids = s.get("pids") or []
        if isinstance(pids, list):
            for p in pids:
                try:
                    out.append(int(p))
                except (TypeError, ValueError):
                    pass
    return sorted(set(out))


def _pids_from_rescan(
    app: str,
    session_id: Optional[str] = None,
) -> List[int]:
    """Fresh sessionize when live.json lacks pids or is stale."""
    try:
        from local_ai_monitor.collect_basic import collect_sessions

        sessions = collect_sessions(include_threads=False)
    except Exception:
        return []
    out: List[int] = []
    for s in sessions:
        if s.app != app:
            continue
        if session_id is not None and s.session_id != session_id:
            continue
        out.extend(int(p) for p in s.pids)
    return sorted(set(out))


def resolve_session_pids(
    app: str,
    session_id: Optional[str] = None,
    *,
    state: Optional[str] = None,
    rescan: bool = True,
) -> List[int]:
    pids = _pids_from_live(app, session_id, state=state)
    if not pids and rescan:
        pids = _pids_from_rescan(app, session_id)
    # Filter dead / protected / self
    safe: List[int] = []
    for pid in pids:
        if pid in _PROTECTED_PIDS:
            continue
        if pid == os.getpid() or pid == os.getppid():
            continue
        if not _alive(pid):
            continue
        cmd = _cmdline(pid)
        if _is_self_cmd(cmd):
            continue
        safe.append(pid)
    return safe


def _signal_pids(
    pids: Sequence[int],
    *,
    sig: int = signal.SIGTERM,
    escalate: bool = True,
    wait_s: float = 1.5,
) -> Dict[str, Any]:
    sent: List[int] = []
    errors: List[str] = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            errors.append(f"pid {pid}: {exc}")
        except OSError as exc:
            errors.append(f"pid {pid}: {exc}")

    still: List[int] = []
    if escalate and sent:
        time.sleep(max(0.1, wait_s))
        for pid in sent:
            if _alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError) as exc:
                    errors.append(f"SIGKILL {pid}: {exc}")
                    still.append(pid)
                else:
                    if _alive(pid):
                        still.append(pid)
    return {
        "signaled": sent,
        "still_alive": still,
        "errors": errors,
    }


def launchd_labels_for(app: str, session_id: Optional[str] = None) -> List[str]:
    """Labels that must be bootout for the kill to stick."""
    labels = list(_LAUNCHD_BY_APP.get(app, []))
    # session-specific overrides later if needed
    _ = session_id
    return labels


def stop_launchd_label(label: str) -> Dict[str, Any]:
    """Stop a user LaunchAgent so KeepAlive cannot revive the process."""
    domain = f"gui/{os.getuid()}"
    path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    out: Dict[str, Any] = {"label": label, "bootout": False, "errors": []}
    # Prefer domain/label form; also try plist path
    for args in (
        ["launchctl", "bootout", f"{domain}/{label}"],
        ["launchctl", "bootout", domain, path] if os.path.isfile(path) else None,
        ["launchctl", "disable", f"{domain}/{label}"],
    ):
        if not args:
            continue
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=8,
            )
            if r.returncode == 0:
                out["bootout"] = True
            elif r.stderr:
                err = (r.stderr or "").strip()
                if err and "No such" not in err and "Could not find" not in err:
                    out["errors"].append(err[:200])
        except (OSError, subprocess.TimeoutExpired) as exc:
            out["errors"].append(str(exc))
    return out


def stop_launchd_for_app(
    app: str,
    session_id: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    labels = launchd_labels_for(app, session_id)
    if not labels:
        return []
    if dry_run:
        return [{"label": lab, "bootout": False, "dry_run": True} for lab in labels]
    return [stop_launchd_label(lab) for lab in labels]


def refresh_live_after_end(*, state: Optional[str] = None) -> bool:
    """Rescan sessions + rewrite live.json so the menu bar updates immediately."""
    try:
        from local_ai_monitor.collect_basic import collect_sessions
        from local_ai_monitor.resource.human import resource_live_block
        from local_ai_monitor.store import (
            atomic_write_json,
            format_local_iso_tz,
            session_stats_to_live_row,
        )

        sessions = collect_sessions(include_threads=False)
        rows = [session_stats_to_live_row(s) for s in sessions if s.nproc > 0]
        total_cpu = sum(float(r.get("cpu_pct") or 0) for r in rows)
        total_rss = sum(int(r.get("rss_kb") or 0) for r in rows)
        total_nproc = sum(int(r.get("nproc") or 0) for r in rows)
        top = None
        if rows:
            best = max(rows, key=lambda r: (r.get("cpu_pct") or 0, r.get("rss_kb") or 0))
            top = {
                "app": best.get("app"),
                "session_id": best.get("session_id"),
                "label": best.get("label"),
                "cpu_pct": best.get("cpu_pct"),
                "rss_kb": best.get("rss_kb"),
            }
        live = read_json(live_path(state)) or {}
        if not isinstance(live, dict):
            live = {}
        live["ts"] = format_local_iso_tz(time.time())
        live["sessions"] = rows
        live["totals"] = {
            "cpu_pct": round(total_cpu, 2),
            "rss_kb": int(total_rss),
            "nproc": int(total_nproc),
            "nsessions": len(rows),
        }
        live["top"] = top
        try:
            live["resource"] = resource_live_block(state=state)
        except Exception:
            pass
        try:
            from local_ai_monitor.quarantine import quarantine_meta

            live["quarantine"] = quarantine_meta()
        except Exception:
            live["quarantine"] = {"tools": []}
        atomic_write_json(live_path(state), live)
        return True
    except Exception:
        return False


def end_session(
    app: str,
    session_id: str,
    *,
    state: Optional[str] = None,
    dry_run: bool = False,
    refresh_live: bool = True,
) -> Dict[str, Any]:
    """Kill one session identified by app + session_id."""
    if not app or not session_id:
        return {"ok": False, "error": "app and session_id required"}
    pids = resolve_session_pids(app, session_id, state=state)
    labels = launchd_labels_for(app, session_id)
    # KeepAlive services: allow end even when pids briefly empty if we can bootout
    if not pids and not labels:
        return {
            "ok": False,
            "error": "no live pids for session",
            "app": app,
            "session_id": session_id,
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "app": app,
            "session_id": session_id,
            "pids": pids,
            "launchd": [{"label": lab, "dry_run": True} for lab in labels],
        }
    launchd_results = stop_launchd_for_app(app, session_id, dry_run=False)
    r = _signal_pids(pids) if pids else {
        "signaled": [],
        "still_alive": [],
        "errors": [],
    }
    # After bootout, re-check still_alive
    still = [p for p in (r.get("still_alive") or pids) if _alive(p)]
    r["still_alive"] = still
    if still:
        r2 = _signal_pids(still, wait_s=0.8)
        r["signaled"] = list(dict.fromkeys(list(r.get("signaled") or []) + list(r2.get("signaled") or [])))
        r["still_alive"] = r2.get("still_alive") or []
        r["errors"] = list(r.get("errors") or []) + list(r2.get("errors") or [])

    bootout_ok = any(x.get("bootout") for x in launchd_results) if launchd_results else False
    signaled = bool(r.get("signaled"))
    ok = (signaled or bootout_ok) and not r.get("still_alive")
    if refresh_live:
        refresh_live_after_end(state=state)
    msg_bits = []
    if signaled:
        msg_bits.append(f"Ended {app} ({len(r['signaled'])} process(es))")
    if bootout_ok:
        msg_bits.append("stopped background service so it stays off")
    if not msg_bits:
        msg_bits.append("Nothing signaled")
    return {
        "ok": ok,
        "app": app,
        "session_id": session_id,
        "pids": pids,
        "launchd": launchd_results,
        **r,
        "message": " · ".join(msg_bits),
    }


def end_tool(
    app: str,
    *,
    state: Optional[str] = None,
    dry_run: bool = False,
    refresh_live: bool = True,
) -> Dict[str, Any]:
    """Kill all live sessions for a tool product id."""
    if not app:
        return {"ok": False, "error": "app (tool_id) required"}
    pids = resolve_session_pids(app, None, state=state)
    labels = launchd_labels_for(app, None)
    if not pids and not labels:
        return {"ok": False, "error": f"no live pids for tool {app!r}", "app": app}
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "app": app,
            "pids": pids,
            "launchd": [{"label": lab, "dry_run": True} for lab in labels],
        }
    launchd_results = stop_launchd_for_app(app, None, dry_run=False)
    r = _signal_pids(pids) if pids else {"signaled": [], "still_alive": [], "errors": []}
    still = [p for p in (r.get("still_alive") or pids) if _alive(p)]
    if still:
        r2 = _signal_pids(still, wait_s=0.8)
        r["signaled"] = list(dict.fromkeys(list(r.get("signaled") or []) + list(r2.get("signaled") or [])))
        r["still_alive"] = r2.get("still_alive") or []
        r["errors"] = list(r.get("errors") or []) + list(r2.get("errors") or [])
    else:
        r["still_alive"] = []
    bootout_ok = any(x.get("bootout") for x in launchd_results) if launchd_results else False
    ok = (bool(r.get("signaled")) or bootout_ok) and not r.get("still_alive")
    if refresh_live:
        refresh_live_after_end(state=state)
    msg = (
        f"Ended all {app} sessions ({len(r.get('signaled') or [])} process(es))"
        if r.get("signaled")
        else ("Stopped " + app if bootout_ok else "Nothing signaled")
    )
    if bootout_ok and r.get("signaled"):
        msg += " · stopped background service so it stays off"
    return {
        "ok": ok,
        "app": app,
        "pids": pids,
        "launchd": launchd_results,
        **r,
        "message": msg,
    }


def end_heaviest(
    *,
    state: Optional[str] = None,
    min_rss_kb: int = 50 * 1024,
    dry_run: bool = False,
    refresh_live: bool = True,
) -> Dict[str, Any]:
    """End the single highest-RSS live session (RAM hog shortcut)."""
    live = read_json(live_path(state)) or {}
    sessions = live.get("sessions") or []
    best: Optional[Dict[str, Any]] = None
    for s in sessions:
        if not isinstance(s, dict):
            continue
        rss = int(s.get("rss_kb") or 0)
        if rss < min_rss_kb:
            continue
        if best is None or rss > int(best.get("rss_kb") or 0):
            best = s
    if not best:
        # fall back to absolute top even below threshold
        for s in sessions:
            if not isinstance(s, dict):
                continue
            rss = int(s.get("rss_kb") or 0)
            if best is None or rss > int(best.get("rss_kb") or 0):
                best = s
    if not best or not best.get("app") or not best.get("session_id"):
        return {"ok": False, "error": "no sessions to end"}
    return end_session(
        str(best["app"]),
        str(best["session_id"]),
        state=state,
        dry_run=dry_run,
        refresh_live=refresh_live,
    )


def cmd_end_session(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(
        prog="local-ai-monitor end-session",
        description="End (kill) an AI tool session that is burning CPU/RAM.",
    )
    p.add_argument("--tool", default=None, help='Tool id e.g. "Grok", "Claude CLI"')
    p.add_argument("--session", default=None, help="session_id; omit with --all")
    p.add_argument("--all", action="store_true", help="End every session for --tool")
    p.add_argument(
        "--heaviest",
        action="store_true",
        help="End the highest-memory session (ignores --session)",
    )
    p.add_argument("--dry-run", action="store_true", help="List pids only")
    p.add_argument("--json", action="store_true", help="JSON result on stdout")
    args = p.parse_args(list(argv or []))

    if args.heaviest:
        result = end_heaviest(dry_run=args.dry_run)
    elif args.all:
        if not args.tool:
            p.error("--all requires --tool")
            return 2
        result = end_tool(args.tool, dry_run=args.dry_run)
    elif args.session:
        if not args.tool:
            p.error("--session requires --tool")
            return 2
        result = end_session(args.tool, args.session, dry_run=args.dry_run)
    else:
        p.error("pass --session, --all, or --heaviest")
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("ok"):
            print(result.get("message") or "ok")
            if result.get("pids"):
                print("pids:", " ".join(str(x) for x in result["pids"]))
        else:
            print(result.get("error") or result.get("message") or "failed", file=sys.stderr)
            if result.get("errors"):
                for e in result["errors"]:
                    print(" ", e, file=sys.stderr)
    return 0 if result.get("ok") else 1
