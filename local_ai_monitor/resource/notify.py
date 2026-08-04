"""Notify-only resource agent — never auto-kills (L4).

  local-ai-monitor resource notify [--once] [--interval 60]

Writes calibration samples; posts a macOS notification when band is warn/hard
and cooldown has elapsed. Menu bar still owns the reclaim-idle action.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from local_ai_monitor.config import ensure_state_dir, state_dir
from local_ai_monitor.resource.audit import append_audit
from local_ai_monitor.resource.config import load_resource_config
from local_ai_monitor.resource.human import resource_live_block
from local_ai_monitor.resource.policy import evaluate
from local_ai_monitor.store import live_path, read_json, atomic_write_json


def _notify_state_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "resource-notify.json")


def _calib_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "resource-calib.jsonl")


def _load_notify_state(state: Optional[str] = None) -> Dict[str, Any]:
    path = _notify_state_path(state)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_notify_state(data: Dict[str, Any], state: Optional[str] = None) -> None:
    ensure_state_dir(state)
    path = _notify_state_path(state)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def append_calib_sample(
    *,
    free_pages: Optional[int],
    band: str,
    memsize: Optional[int],
    state: Optional[str] = None,
) -> None:
    """Pressure samples for waterline calibration (no secrets)."""
    try:
        ensure_state_dir(state)
        path = _calib_path(state)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "free_pages": free_pages,
            "band": band,
            "memsize_bytes": memsize,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def merge_resource_into_live(
    resource: Dict[str, Any],
    *,
    state: Optional[str] = None,
) -> bool:
    """Patch live.json resource field without wiping sessions (best-effort)."""
    path = live_path(state)
    live = read_json(path)
    if not isinstance(live, dict):
        return False
    live["resource"] = resource
    try:
        atomic_write_json(path, live)
        return True
    except OSError:
        return False


def post_macos_notification(title: str, body: str) -> bool:
    """User-visible Notification Center banner. Never claims kill happened."""
    # Escape for AppleScript string
    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(body)}" with title "{esc(title)}" '
        f'subtitle "Local AI Monitor · Headroom"'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify_once(*, state: Optional[str] = None) -> Dict[str, Any]:
    """One evaluate → calib → optional notify. Never ends sessions."""
    cfg = load_resource_config()
    # notify config: "menubar_field" | "notification" | "both" | "none"
    mode = str(cfg.get("notify") or "notification")
    cooldown = int(cfg.get("cooldown_s") or 120)

    d = evaluate(state=state, config=cfg)
    human = resource_live_block(state=state, config=cfg)
    phys = d.physics or {}

    append_calib_sample(
        free_pages=d.free_pages,
        band=d.band,
        memsize=phys.get("memsize_bytes") if isinstance(phys, dict) else None,
        state=state,
    )

    if mode in ("menubar_field", "both", "notification"):
        merge_resource_into_live(human, state=state)

    notified = False
    skipped = "band_ok"
    if human.get("show") and d.band in ("warn", "hard"):
        st = _load_notify_state(state)
        last = float(st.get("last_notify_ts") or 0)
        last_band = st.get("last_notify_band")
        now = time.time()
        escalate = last_band != d.band and d.band == "hard"
        if mode in ("notification", "both") and (
            escalate or (now - last) >= cooldown
        ):
            title = str(human.get("title") or "Headroom is low")
            body = str(
                human.get("detail")
                or "Open Local AI Monitor in the menu bar to reclaim idle load."
            )
            # Keep notification short
            if len(body) > 180:
                body = body[:177] + "…"
            notified = post_macos_notification(title, body)
            if notified:
                st["last_notify_ts"] = now
                st["last_notify_band"] = d.band
                _save_notify_state(st, state)
                skipped = "sent"
            else:
                skipped = "osascript_failed"
        else:
            skipped = "cooldown" if mode in ("notification", "both") else "notify_off"
    else:
        skipped = "no_show"

    append_audit(
        "notify",
        {
            "band": d.band,
            "free_pages": d.free_pages,
            "notified": notified,
            "skip": skipped,
            "show": bool(human.get("show")),
        },
        state=state,
    )
    return {
        "ok": True,
        "band": d.band,
        "notified": notified,
        "skip": skipped,
        "human": human,
        "auto_end": False,  # explicit: this agent never kills
    }


def cmd_notify(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-rm notify",
        description="Notify-only memory alerts (never auto-kills).",
    )
    p.add_argument("--once", action="store_true", help="Single pass then exit (LaunchAgent)")
    p.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between checks when not --once (default 60)",
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args(list(argv or []))

    if args.once or args.interval <= 0:
        out = notify_once()
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(
                f"band={out.get('band')} notified={out.get('notified')} "
                f"skip={out.get('skip')} (never auto-kills)"
            )
            h = out.get("human") or {}
            if h.get("show"):
                print(f"  {h.get('title')}")
                print(f"  {h.get('detail')}")
        return 0

    # Long-running (dev); LaunchAgent uses --once + StartInterval
    while True:
        try:
            notify_once()
        except Exception as exc:
            print(f"notify error: {exc}", file=sys.stderr)
        time.sleep(max(15.0, float(args.interval)))
