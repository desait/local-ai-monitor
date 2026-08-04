"""Append-only audit log for resource policy actions. Never logs cmd/env."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from local_ai_monitor.config import ensure_state_dir, state_dir


def audit_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "resource-audit.jsonl")


def append_audit(
    event: str,
    payload: Dict[str, Any],
    *,
    state: Optional[str] = None,
) -> None:
    """Best-effort append. Never raises to callers."""
    try:
        ensure_state_dir(state)
        path = audit_path(state)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            **{k: v for k, v in payload.items() if k not in ("cmd", "env", "command")},
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
