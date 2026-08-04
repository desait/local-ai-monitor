"""Optional background session observer (learning logs only).

Writes dry-run packs under ~/.local/state/local-ai-monitor/ccm/.
Does not inject capsules into live agents. Does not claim improved efficiency.
Optional external packer path via LOCAL_AI_MONITOR_OBSERVER_ROOT (never required).
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from local_ai_monitor.config import STATE_DIR, ensure_state_dir

CCM_STATE = os.path.join(STATE_DIR, "ccm")
CCM_STATUS = os.path.join(CCM_STATE, "status.json")
CCM_PACKS = os.path.join(CCM_STATE, "packs")
CCM_LABEL = "com.user.local-ai-monitor.ccm-observer"
LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ccm_status_meta() -> Dict[str, Any]:
    """For live.json / status CLI — not shown in the menu bar."""
    if not os.path.isfile(CCM_STATUS):
        return {
            "enabled": False,
            "last_run_ts": None,
            "last_pack": None,
            "note": "not started",
        }
    try:
        with open(CCM_STATUS, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {"enabled": False, "note": "unavailable"}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "last_run_ts": raw.get("last_run_ts"),
            "last_pack": raw.get("last_pack"),
            "note": raw.get("note") or "background session log",
        }
    except (OSError, json.JSONDecodeError):
        return {"enabled": False, "note": "unavailable"}


def format_status_line(meta: Optional[Dict[str, Any]] = None) -> str:
    """Plain-English one-liner for `local-ai-monitor status`."""
    m = meta if meta is not None else ccm_status_meta()
    if not m.get("enabled"):
        return "off"
    last = m.get("last_run_ts")
    if not last:
        return "on"
    try:
        s = str(last).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        when = local.strftime("%I:%M %p").lstrip("0")
    except Exception:
        when = str(last)[:16]
    return f"on · last update {when}"


def _write_status(**kwargs: Any) -> None:
    ensure_state_dir()
    os.makedirs(CCM_STATE, mode=0o700, exist_ok=True)
    cur: Dict[str, Any] = {}
    if os.path.isfile(CCM_STATUS):
        try:
            with open(CCM_STATUS, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cur = raw
        except (OSError, json.JSONDecodeError):
            cur = {}
    cur.update(kwargs)
    cur["enabled"] = True
    cur["last_run_ts"] = _now_iso()
    with open(CCM_STATUS, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2)
        f.write("\n")
    try:
        os.chmod(CCM_STATUS, 0o600)
    except OSError:
        pass


def _resolve_python3() -> str:
    for candidate in (
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        shutil.which("python3") or "",
        "/usr/bin/python3",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            try:
                return os.path.realpath(candidate)
            except OSError:
                return candidate
    return "/usr/bin/python3"


def install_ccm_launchagent(
    *, python: Optional[str] = None, interval_s: int = 300
) -> str:
    """Install the optional background session-log LaunchAgent plist."""
    ensure_state_dir()
    os.makedirs(LAUNCH_AGENTS, mode=0o755, exist_ok=True)
    py = python or _resolve_python3()
    plist_path = os.path.join(LAUNCH_AGENTS, f"{CCM_LABEL}.plist")
    data = {
        "Label": CCM_LABEL,
        "ProgramArguments": [py, "-m", "local_ai_monitor.ccm_observer"],
        "WorkingDirectory": STATE_DIR,
        "EnvironmentVariables": {
            "HOME": os.path.expanduser("~"),
            "LC_ALL": "C",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": os.path.expanduser("~/.local/lib/local-ai-monitor"),
        },
        "RunAtLoad": True,
        "StartInterval": int(max(60, interval_s)),
        "StandardOutPath": os.path.join(STATE_DIR, "ccm-observer.log"),
        "StandardErrorPath": os.path.join(STATE_DIR, "ccm-observer.log"),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    try:
        os.chmod(plist_path, 0o644)
    except OSError:
        pass
    return plist_path


def run_observer_once(*, observer_root: Optional[str] = None) -> Dict[str, Any]:
    """One observer cycle: write a dry-run pack under state.

    If LOCAL_AI_MONITOR_OBSERVER_ROOT (or observer_root) points at an optional external
    tool tree with a pack script, invoke it. Otherwise write a local stub pack.
    """
    ensure_state_dir()
    os.makedirs(CCM_PACKS, mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pack_path = os.path.join(CCM_PACKS, f"dryrun_{stamp}.jsonl")

    row = {
        "ts": _now_iso(),
        "event": "observer_tick",
        "note": "local dry-run pack (no external claims)",
    }
    with open(pack_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")

    root = observer_root or os.environ.get("LOCAL_AI_MONITOR_OBSERVER_ROOT") or ""
    external: Dict[str, Any] = {"ok": False, "skip": "no_observer_root"}
    if root and os.path.isdir(root):
        # Optional: run external packer if present (site-specific; not required)
        packer = os.path.join(root, "scripts", "shadow_pack.py")
        if os.path.isfile(packer):
            try:
                r = subprocess.run(
                    ["python3", packer, "--dry-run", "--out", pack_path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=root,
                )
                external = {"ok": r.returncode == 0, "rc": r.returncode}
            except (OSError, subprocess.TimeoutExpired) as exc:
                external = {"ok": False, "error": str(exc)}
        else:
            external = {"ok": False, "skip": "no_packer_script"}

    _write_status(
        last_pack=pack_path,
        note="background session log",
        external=external,
    )
    return {"ok": True, "pack": pack_path, "external": external}


def main(argv: Optional[list] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="local-ai-monitor ccm-observer")
    p.add_argument(
        "--observer-root",
        default=None,
        help="Optional external observer tool tree (LOCAL_AI_MONITOR_OBSERVER_ROOT)",
    )
    args = p.parse_args(list(argv or []))
    r = run_observer_once(observer_root=args.observer_root)
    print(json.dumps(r, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
