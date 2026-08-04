"""AI tool catalog — discover installed tools; user visibility prefs.

New tools are registered once in KNOWN_TOOLS (markers). Discovery scans the
machine; classify uses the same markers. Users hide tools they do not want
in the menu / simple view via ~/.config/local-ai-monitor/tools.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from local_ai_monitor.config import CONFIG_DIR

# --- Registry (extend here; no one-off code paths for each new product) ---


@dataclass(frozen=True)
class ToolDef:
    """One product family the monitor can detect."""

    id: str  # stable key used in classify / live.json
    display: str  # default English name
    # Process markers (any match → this tool)
    binaries: Tuple[str, ...] = ()
    # Substrings in cmd path/env (strict — avoid folder-name false positives)
    path_markers: Tuple[str, ...] = ()
    env_markers: Tuple[str, ...] = ()
    # macOS .app bundle names under /Applications
    app_bundles: Tuple[str, ...] = ()
    # Install footprints (existence ⇒ "installed" even if idle)
    install_bins: Tuple[str, ...] = ()  # relative to PATH dirs or absolute
    install_dirs: Tuple[str, ...] = ()  # expanduser
    install_apps: Tuple[str, ...] = ()  # /Applications/Name.app
    # Classification priority (lower = earlier). Desktop apps before generic CLIs.
    priority: int = 100


# Order = default display priority when not overridden by prefs.
KNOWN_TOOLS: Tuple[ToolDef, ...] = (
    ToolDef(
        id="Claude Desktop",
        display="Co-Work",
        path_markers=("/Applications/Claude.app/", "Claude Helper"),
        app_bundles=("Claude.app",),
        install_apps=("Claude.app",),
        priority=10,
    ),
    ToolDef(
        id="ChatGPT",
        display="ChatGPT",
        path_markers=(
            "/Applications/ChatGPT.app/",
            "Application Support/com.openai.chat",
            "Application Support/OpenAI/",
        ),
        binaries=("ChatGPT",),
        app_bundles=("ChatGPT.app",),
        install_apps=("ChatGPT.app",),
        priority=20,
    ),
    ToolDef(
        id="Buzz",
        display="Buzz",
        path_markers=(
            "/Applications/Buzz.app/",
            "Application Support/Buzz/",
            "/.buzz/",
            "xyz.block.buzz",
        ),
        env_markers=(
            "XPC_SERVICE_NAME=com.buzz",
            "BUZZ_ACP_",
            "BUZZ_MANAGED_AGENT=",
            "BUZZ_RELAY_URL=",
        ),
        binaries=("buzz", "buzz-desktop", "buzz-agent", "buzz-acp", "buzz-dev-mcp"),
        app_bundles=("Buzz.app",),
        install_apps=("Buzz.app",),
        install_dirs=("~/.buzz",),
        priority=30,
    ),
    ToolDef(
        id="OpenClaw",
        display="OpenClaw",
        path_markers=(
            "/node_modules/openclaw/",
            "/.openclaw/",
            "ai.openclaw.",
        ),
        env_markers=(
            "OPENCLAW_SERVICE_MARKER=",
            "OPENCLAW_GATEWAY",
            "XPC_SERVICE_NAME=ai.openclaw",
        ),
        binaries=("openclaw",),
        install_bins=("openclaw",),
        install_dirs=("~/.openclaw",),
        priority=40,
    ),
    ToolDef(
        id="Codex",
        display="Codex",
        binaries=("codex",),
        path_markers=(
            "/.codex/packages/",
            "/.codex/bin/",
            "node_modules/@openai/codex",
            "openai-codex",
        ),
        # Prefer binary/path; bare CODEX_ env alone is too weak on multi-tool hosts
        env_markers=("OPENAI_CODEX=",),
        app_bundles=("Codex.app", "OpenAI Codex.app"),
        install_bins=("codex",),
        install_dirs=("~/.codex",),
        install_apps=("Codex.app", "OpenAI Codex.app"),
        priority=50,
    ),
    ToolDef(
        id="OpenAI CLI",
        display="OpenAI CLI",
        binaries=("openai",),
        path_markers=("/.openai/", "node_modules/openai/bin"),
        # env alone never classifies — see classify_with_catalog gate
        env_markers=(),
        install_bins=("openai",),
        install_dirs=("~/.openai",),
        priority=55,
    ),
    ToolDef(
        id="Cursor",
        display="Cursor",
        binaries=("cursor", "Cursor"),
        path_markers=(
            "/Applications/Cursor.app/",
            "Cursor Helper",
            "/.cursor/",
        ),
        app_bundles=("Cursor.app",),
        install_bins=("cursor",),
        install_apps=("Cursor.app",),
        install_dirs=("~/.cursor",),
        priority=60,
    ),
    ToolDef(
        id="Grok",
        display="Grok",
        binaries=("grok",),
        path_markers=("/.grok/sessions/", "/.grok/bin/"),
        env_markers=("GROK_AGENT=",),
        install_bins=("grok",),
        install_dirs=("~/.grok",),
        priority=70,
    ),
    ToolDef(
        id="Claude CLI",
        display="Anthropic CLI",
        binaries=("claude",),
        path_markers=("@anthropic-ai/claude",),
        env_markers=("CLAUDE_CODE",),
        install_bins=("claude",),
        # not Claude.app — that is Desktop
        priority=80,
    ),
)

_BY_ID: Dict[str, ToolDef] = {t.id: t for t in KNOWN_TOOLS}

# Built-in product order for UI (discovered-only tools append after)
DEFAULT_ORDER = tuple(t.id for t in sorted(KNOWN_TOOLS, key=lambda t: t.priority))

PREFS_PATH = os.path.join(CONFIG_DIR, "tools.json")

_PATH_DIRS = (
    os.path.expanduser("~/.local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/bin"),
)


def tool_def(tool_id: str) -> Optional[ToolDef]:
    return _BY_ID.get(tool_id)


def all_tool_ids() -> List[str]:
    return list(DEFAULT_ORDER)


def display_name(tool_id: str) -> str:
    t = _BY_ID.get(tool_id)
    return t.display if t else tool_id


# --- Discovery ---


def _bin_exists(name: str) -> bool:
    if name.startswith("/"):
        return os.path.isfile(name) and os.access(name, os.X_OK)
    for d in _PATH_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return True
    # PATH
    path = os.environ.get("PATH", "")
    for d in path.split(":"):
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return True
    return False


def _app_exists(bundle: str) -> bool:
    return os.path.isdir(f"/Applications/{bundle}")


def is_installed(tool: ToolDef) -> bool:
    for b in tool.install_bins or tool.binaries:
        if _bin_exists(b):
            return True
    for a in tool.install_apps or tool.app_bundles:
        if _app_exists(a):
            return True
    for d in tool.install_dirs:
        if os.path.isdir(os.path.expanduser(d)):
            return True
    return False


def discover_installed() -> List[str]:
    """Return tool ids that appear installed on this machine."""
    found = [t.id for t in KNOWN_TOOLS if is_installed(t)]
    return found


# --- Prefs (user-editable view) ---


@dataclass
class ToolPrefs:
    """User view preferences."""

    hidden: Set[str] = field(default_factory=set)
    # Explicit show order; empty → default catalog order
    order: List[str] = field(default_factory=list)
    # Show installed tools even when not running (as Idle)
    show_idle_installed: bool = True
    # Extra user-defined markers later
    custom_ids: List[str] = field(default_factory=list)

    def is_visible(self, tool_id: str) -> bool:
        return tool_id not in self.hidden

    def ordered_visible(self, candidates: Sequence[str]) -> List[str]:
        cand = [c for c in candidates if self.is_visible(c)]
        if self.order:
            ranked = [x for x in self.order if x in cand]
            rest = [x for x in cand if x not in ranked]
            # rest by default catalog order
            rest_sorted = [x for x in DEFAULT_ORDER if x in rest] + [
                x for x in rest if x not in DEFAULT_ORDER
            ]
            return ranked + rest_sorted
        return [x for x in DEFAULT_ORDER if x in cand] + [
            x for x in cand if x not in DEFAULT_ORDER
        ]


def load_tool_prefs(path: Optional[str] = None) -> ToolPrefs:
    p = os.path.expanduser(path or PREFS_PATH)
    prefs = ToolPrefs()
    if not os.path.isfile(p):
        return prefs
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return prefs
        hid = raw.get("hidden") or raw.get("hidden_ids") or []
        if isinstance(hid, list):
            prefs.hidden = {str(x) for x in hid}
        order = raw.get("order") or []
        if isinstance(order, list):
            prefs.order = [str(x) for x in order]
        if "show_idle_installed" in raw:
            prefs.show_idle_installed = bool(raw["show_idle_installed"])
        custom = raw.get("custom_ids") or []
        if isinstance(custom, list):
            prefs.custom_ids = [str(x) for x in custom]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return prefs


def save_tool_prefs(prefs: ToolPrefs, path: Optional[str] = None) -> str:
    p = os.path.expanduser(path or PREFS_PATH)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    payload = {
        "hidden": sorted(prefs.hidden),
        "order": list(prefs.order),
        "show_idle_installed": prefs.show_idle_installed,
        "custom_ids": list(prefs.custom_ids),
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def set_hidden(tool_id: str, hidden: bool, path: Optional[str] = None) -> ToolPrefs:
    prefs = load_tool_prefs(path)
    if hidden:
        prefs.hidden.add(tool_id)
    else:
        prefs.hidden.discard(tool_id)
    save_tool_prefs(prefs, path)
    return prefs


def visible_tool_ids(
    *,
    running: Optional[Sequence[str]] = None,
    installed: Optional[Sequence[str]] = None,
    prefs: Optional[ToolPrefs] = None,
) -> List[str]:
    """Tools to show in non-tech views."""
    prefs = prefs or load_tool_prefs()
    installed = list(installed if installed is not None else discover_installed())
    running = list(running or [])
    if prefs.show_idle_installed:
        candidates = sorted(set(installed) | set(running), key=lambda x: 0)
    else:
        candidates = list(running)
    # Always include anything currently running even if "not installed" heuristic missed
    for r in running:
        if r not in candidates:
            candidates.append(r)
    return prefs.ordered_visible(candidates)


# --- Classification against catalog ---


def classify_with_catalog(cmd: str, basename: str, exe: str) -> Optional[str]:
    """Return tool id for a process, or None.

    Rules:
    - Never match bare project folder names without binary/env markers.
    - Claude Desktop before Claude CLI.
    - Skip local-ai-monitor self (caller should self-filter first).
    """
    c = cmd
    # Claude Desktop vs CLI: desktop paths win
    if "/Applications/Claude.app/" in c or "Claude Helper" in c:
        return "Claude Desktop"

    # Score tools by marker hits; lowest priority number wins on tie-break
    best: Optional[Tuple[int, str]] = None

    for t in KNOWN_TOOLS:
        hit = False
        if t.id == "Claude Desktop":
            # already handled
            continue
        if t.id == "Claude CLI" and "/Applications/Claude.app/" in c:
            continue

        if basename in t.binaries:
            hit = True
        if not hit:
            for b in t.binaries:
                if exe.endswith("/" + b) or exe.endswith("/bin/" + b):
                    hit = True
                    break
        if not hit:
            for m in t.path_markers:
                if m in c:
                    # Grok: do not treat a generic project folder path as Grok.
                    if t.id == "Grok" and m in ("/.grok/sessions/", "/.grok/bin/"):
                        if m in c:
                            hit = True
                            break
                    elif t.id == "Grok":
                        continue
                    else:
                        hit = True
                        break
        if not hit:
            for m in t.env_markers:
                if m in c:
                    # OpenAI CLI: env alone is too weak without binary
                    if t.id == "OpenAI CLI" and basename not in t.binaries:
                        if not any(b in c for b in t.binaries):
                            continue
                    hit = True
                    break

        # Codex binary (standalone install often ~/.local/bin/codex → ~/.codex/...)
        if not hit and t.id == "Codex":
            if basename == "codex" or exe.endswith("/codex") or exe.endswith(
                "/bin/codex"
            ):
                hit = True
            elif re.search(r"(?:^|[\s/])codex(?:\s|$)", c[:120]):
                # only when argv looks like the codex CLI, not PATH noise
                if basename in ("codex", "node", "python", "python3") or "codex" in exe:
                    hit = True

        if hit:
            cand = (t.priority, t.id)
            if best is None or cand[0] < best[0]:
                best = cand

    # Grok binary explicit (after catalog loop for path safety)
    if best is None or best[1] != "Grok":
        if basename == "grok" or exe.endswith("/bin/grok") or exe.endswith("/grok"):
            return "Grok"
        if re.search(r"(?:^|\s)GROK_AGENT=", c):
            # Buzz wins if Buzz markers also present (caller order)
            if not any(
                m in c
                for m in (
                    "/.buzz/",
                    "BUZZ_ACP_",
                    "BUZZ_MANAGED_AGENT=",
                    "XPC_SERVICE_NAME=com.buzz",
                )
            ):
                return "Grok"

    return best[1] if best else None


def catalog_snapshot() -> Dict[str, object]:
    """JSON-serializable snapshot for live.json / menubar."""
    installed = discover_installed()
    prefs = load_tool_prefs()
    return {
        "installed": installed,
        "hidden": sorted(prefs.hidden),
        "order": prefs.order,
        "show_idle_installed": prefs.show_idle_installed,
        "known": [
            {"id": t.id, "display": t.display, "installed": t.id in installed}
            for t in KNOWN_TOOLS
        ],
        "visible": visible_tool_ids(installed=installed, running=[], prefs=prefs),
    }
