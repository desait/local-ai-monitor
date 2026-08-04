"""Config loader — ~/.config/local-ai-monitor/config.json.

Stdlib JSON only. Missing file means defaults. Unknown keys are ignored.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Dict, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    "sample_interval_s": 10,
    "tui_interval_s": 1.5,
    "rollup_interval_s": 1800,
    "retention_days": 14,
    "threads_default": False,
    "mux": {
        "glances_left": True,
        "tmux_session": "local-ai-monitor",
        "glances_args": ["-t", "2", "--disable-plugin", "docker"],
    },
    "menubar": {
        "refresh_s": 3,
        "stale_factor": 3,
    },
}

# State / config paths (design install layout)
CONFIG_DIR = os.path.expanduser("~/.config/local-ai-monitor")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = os.path.expanduser("~/.local/state/local-ai-monitor")


def state_dir(override: Optional[str] = None) -> str:
    d = override or os.environ.get("LOCAL_AI_MONITOR_STATE") or STATE_DIR
    return os.path.expanduser(d)


def ensure_state_dir(override: Optional[str] = None) -> str:
    d = state_dir(override)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load config with defaults. Never raises on missing/corrupt file."""
    cfg_path = path or os.environ.get("LOCAL_AI_MONITOR_CONFIG") or CONFIG_PATH
    cfg_path = os.path.expanduser(cfg_path)
    if not os.path.isfile(cfg_path):
        return deepcopy(DEFAULT_CONFIG)
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return deepcopy(DEFAULT_CONFIG)
        return _deep_merge(DEFAULT_CONFIG, raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return deepcopy(DEFAULT_CONFIG)


def write_default_config(path: Optional[str] = None) -> str:
    """Write defaults if missing. Returns path written (or existing)."""
    cfg_path = path or CONFIG_PATH
    cfg_path = os.path.expanduser(cfg_path)
    parent = os.path.dirname(cfg_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if not os.path.isfile(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
            f.write("\n")
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
    return cfg_path
