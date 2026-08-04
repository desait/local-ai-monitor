"""Process classification — catalog-driven AI tool detection.

Tools are defined in local_ai_monitor.catalog.KNOWN_TOOLS. Adding a product = one ToolDef
entry + install markers; no per-product request path required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from local_ai_monitor.catalog import (
    DEFAULT_ORDER,
    classify_with_catalog,
    display_name,
    load_tool_prefs,
    visible_tool_ids,
)

# Dynamic product list (default catalog order). Prefer catalog.DEFAULT_ORDER.
APPS: Tuple[str, ...] = tuple(DEFAULT_ORDER)


@dataclass
class Proc:
    pid: int
    ppid: int
    pcpu: float
    pmem: float
    rss_kb: int
    cmd: str  # command + env (from ps eww)
    app: Optional[str] = None
    # Set by sessionize before BFS; None means not a direct classify hit
    direct_app: Optional[str] = None

    @property
    def exe(self) -> str:
        """First path/token of the command (before env vars)."""
        parts = self.cmd.split()
        if not parts:
            return ""
        return parts[0]

    @property
    def basename(self) -> str:
        return os.path.basename(self.exe.rstrip("/"))


@dataclass
class AppStats:
    name: str
    pids: Set[int] = field(default_factory=set)
    pcpu: float = 0.0
    pmem: float = 0.0
    rss_kb: int = 0
    threads: int = 0
    samples: List[str] = field(default_factory=list)

    @property
    def nproc(self) -> int:
        return len(self.pids)


def classify_direct(p: Proc) -> Optional[str]:
    """Map a process to a tool id, or None.

    Strict markers: never match generic project folder names alone.
    """
    c = p.cmd
    base = p.basename
    exe = p.exe

    # Skip this monitor
    if "local-ai-monitor" in c and ("python" in c.lower() or base.startswith("Python")):
        return None
    if "local_ai_monitor" in c and ("python" in c.lower() or "-m" in c):
        return None

    # Buzz wins over Grok when both markers present
    from local_ai_monitor.catalog import KNOWN_TOOLS

    buzz = next((t for t in KNOWN_TOOLS if t.id == "Buzz"), None)
    if buzz:
        buzz_hit = (
            any(m in c for m in buzz.path_markers)
            or any(m in c for m in buzz.env_markers)
            or base in buzz.binaries
        )
        if buzz_hit:
            return "Buzz"

    return classify_with_catalog(c, base, exe)


def active_app_names() -> List[str]:
    """All known tool ids in display order (for expert TUI empty rows etc.)."""
    return list(APPS)


def visible_apps_for_ui(running: Optional[List[str]] = None) -> List[str]:
    """Tool ids to show in menu/simple views after prefs + discovery."""
    return visible_tool_ids(running=running or [])


def tool_label(tool_id: str) -> str:
    return display_name(tool_id)
