"""Resource policy config — ~/.config/local-ai-monitor/resource.json

Orchestration on 8 GB (physics-first):
- Free pages decide *when* to reclaim.
- Activity decides *what* is legal (never mid-stream active work).
- auto_end defaults ON for **idle only** (idle_service / idle sessions).
"""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from local_ai_monitor.config import CONFIG_DIR

RESOURCE_CONFIG_PATH = os.path.join(CONFIG_DIR, "resource.json")

_BASE_MEM_GB = 8.0
_BASE_HARD = 4000
_BASE_WARN = 8000


def _sysctl_int(name: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode != 0:
            return None
        return int((r.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def waterlines_for_host(
    memsize_bytes: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Tuple[int, int]:
    """Return (warn, hard) free-page waterlines scaled to host RAM."""
    mem = memsize_bytes if memsize_bytes is not None else _sysctl_int("hw.memsize")
    if not mem or mem <= 0:
        return _BASE_WARN, _BASE_HARD
    gb = float(mem) / (1024.0**3)
    scale = max(0.5, min(4.0, gb / _BASE_MEM_GB))
    hard = int(round(_BASE_HARD * scale))
    warn = int(round(_BASE_WARN * scale))
    ps = page_size or 16384
    try:
        from local_ai_monitor.resource.physics import page_size_bytes

        ps = page_size or page_size_bytes()
    except Exception:
        pass
    total_pages = max(1, int(mem // ps))
    hard = max(1500, min(hard, int(total_pages * 0.02)))
    warn = max(hard + 500, min(warn, int(total_pages * 0.04)))
    return warn, hard


def _default_resource() -> Dict[str, Any]:
    warn, hard = waterlines_for_host()
    # GB-scale floors; interactive CLIs never auto-killed (2026-07-30 incident).
    gb_kb = 1024 * 1024
    half_gb_kb = 512 * 1024
    return {
        # Auto reclaim OFF by default after false Grok soft-stop.
        # When ON: idle services only unless auto_end_cli=true (still never
        # interactive apps — see activity.INTERACTIVE_APPS).
        "auto_end": False,
        "auto_end_band": "hard",
        "auto_end_idle_on_warn": False,
        "auto_end_cli": False,
        "auto_end_active": False,  # HARD LAW — never kill mid-stream
        "apple_relief": False,  # opt-in Safari/Chrome quit under hard
        "apple_min_rss_kb": gb_kb,
        "max_auto_ends_per_tick": 1,
        "waterline_free_pages_warn": warn,
        "waterline_free_pages_hard": hard,
        "protect_tools": [
            "Grok",
            "Claude CLI",
            "Claude",
            "Codex",
            "Cursor",
            "Buzz",
        ],
        "cooldown_s": 60,
        "prefer_end": "idle_service_then_idle",
        "min_rss_kb": half_gb_kb,
        "heavy_rss_mb": 1024,
        "notify": "both",
        "waterlines_calibrated": True,
        "waterlines_mem_gb": round(
            (_sysctl_int("hw.memsize") or 8 * 1024**3) / (1024.0**3), 1
        ),
        "units": "gb_scale",
        "min_rss_gb": 0.5,
        "apple_min_rss_gb": 1.0,
        "heavy_rss_gb": 1.0,
    }


DEFAULT_RESOURCE: Dict[str, Any] = _default_resource()


def resource_config_path(override: Optional[str] = None) -> str:
    return os.path.expanduser(
        override or os.environ.get("LOCAL_AI_MONITOR_RESOURCE_CONFIG") or RESOURCE_CONFIG_PATH
    )


def load_resource_config(path: Optional[str] = None) -> Dict[str, Any]:
    cfg_path = resource_config_path(path)
    base = _default_resource()
    if not os.path.isfile(cfg_path):
        return deepcopy(base)
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return deepcopy(base)
        out = deepcopy(base)
        out.update(raw)
        pt = out.get("protect_tools")
        if not isinstance(pt, list):
            out["protect_tools"] = list(base["protect_tools"])
        else:
            out["protect_tools"] = [str(x) for x in pt]
        # Law: never allow auto_end_active via config (user kill is separate)
        out["auto_end_active"] = False
        out["auto_end"] = bool(out.get("auto_end"))
        if "waterline_free_pages_hard" not in raw:
            out["waterline_free_pages_hard"] = base["waterline_free_pages_hard"]
        if "waterline_free_pages_warn" not in raw:
            out["waterline_free_pages_warn"] = base["waterline_free_pages_warn"]
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return deepcopy(base)


def write_default_resource_config(
    path: Optional[str] = None,
    *,
    force: bool = False,
) -> str:
    cfg_path = resource_config_path(path)
    parent = os.path.dirname(cfg_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if force or not os.path.isfile(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(_default_resource(), f, indent=2)
            f.write("\n")
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
    return cfg_path


def protect_tools(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    c = cfg if cfg is not None else load_resource_config()
    pt = c.get("protect_tools") or []
    return [str(x) for x in pt] if isinstance(pt, list) else []
