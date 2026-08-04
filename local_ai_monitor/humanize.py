"""Plain-English layer for non-technical UI (menu bar / simple list).

Collector JSON stays technical; this module only formats for humans.
No PIDs, no session UUIDs, no tmux jargon.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Internal app id → friendly tool name (catalog is source of truth when available)
TOOL_NAMES = {
    "OpenClaw": "OpenClaw",
    "Grok": "Grok",
    "Claude CLI": "Anthropic CLI",
    "Claude Desktop": "Co-Work",
    "Buzz": "Buzz",
    "ChatGPT": "ChatGPT",
    "Codex": "Codex",
    "OpenAI CLI": "OpenAI CLI",
    "Cursor": "Cursor",
}

# Buzz channel/project slugs → title case phrases
_CHANNEL_TITLES = {
    "sample-app": "Sample App",
    "sample-meshsim": "Sample MeshSim",
    "polymath": "Polymath",
    "encyclopedia": "Encyclopedia",
    "desktop": "Buzz desktop",
    "unknown": "Unknown project",
}

_AGENT_TITLES = {
    "operator/grok": "Operator (Grok)",
    "operator": "Operator",
    "fizz/claude": "Fizz (Claude)",
    "fizz": "Fizz",
    "altman": "Altman",
    "bumble": "Bumble",
    "honey": "Honey",
    "tunnel": "Network tunnel",
    "channel-trace": "Channel monitor",
    "runtime": "App runtime",
    "worker": "Worker",
    "managed": "Buzz helper",
    "acp-worker": "Agent worker",
    "access": "Access helper",
}


def tool_display_name(app: str) -> str:
    try:
        from local_ai_monitor.catalog import display_name

        return display_name(app) if app else "Unknown tool"
    except Exception:
        return TOOL_NAMES.get(app, app or "Unknown tool")


_PROJECT_SPECIAL = {
    "local-ai-monitor": "Local AI Monitor",
    "ai top": "Local AI Monitor",
    "local_ai_monitor": "Local AI Monitor",
    "localaimonitor": "Local AI Monitor",
    "sample-app": "Sample App",
    "healthcare os": "Sample App",
    "industry encyclopedia": "Sample Project",
    "sample_project": "Sample Project",
    "native 340b": "Native 340B",
    "native-340b": "Native 340B",
}


def _title_words(slug: str) -> str:
    raw = (slug or "").strip()
    if not raw:
        return "Unknown"
    key = raw.casefold().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if key in _PROJECT_SPECIAL:
        return _PROJECT_SPECIAL[key]
    # also try hyphenated original
    if raw.casefold() in _PROJECT_SPECIAL:
        return _PROJECT_SPECIAL[raw.casefold()]
    s = raw.replace("_", " ").replace("-", " ").strip()
    return " ".join(w[:1].upper() + w[1:] if w else "" for w in s.split())


def channel_display_name(channel: str) -> str:
    c = (channel or "").casefold()
    if c in _CHANNEL_TITLES:
        return _CHANNEL_TITLES[c]
    return _title_words(channel)


def agent_display_name(agent: str) -> str:
    a = (agent or "").casefold()
    if a in _AGENT_TITLES:
        return _AGENT_TITLES[a]
    # operator/grok-4.5 style
    if "/" in a:
        left, right = a.split("/", 1)
        left_h = _AGENT_TITLES.get(left, _title_words(left))
        right_h = _AGENT_TITLES.get(right, _title_words(right))
        return f"{left_h} ({right_h})"
    return _title_words(agent)


def load_phrase(cpu: float) -> str:
    if cpu < 0.5:
        return "Quiet"
    if cpu < 5:
        return "Light"
    if cpu < 20:
        return "Working"
    if cpu < 50:
        return "Busy"
    return "Very busy"


def mem_phrase(rss_kb: int) -> str:
    if rss_kb <= 0:
        return "no memory listed"
    mb = rss_kb / 1024.0
    if mb < 1:
        return f"about {int(rss_kb)} KB of memory"
    if mb < 10:
        return f"about {mb:.1f} MB of memory"
    if mb < 1024:
        return f"about {int(round(mb))} MB of memory"
    return f"about {mb / 1024.0:.1f} GB of memory"


def mem_short(rss_kb: int) -> str:
    """Shorter for menu rows: '270 MB'."""
    if rss_kb <= 0:
        return "—"
    mb = rss_kb / 1024.0
    if mb < 1:
        return f"{int(rss_kb)} KB"
    if mb < 10:
        return f"{mb:.1f} MB"
    if mb < 1024:
        return f"{int(round(mb))} MB"
    return f"{mb / 1024.0:.1f} GB"


def chip_text(cpu: float, *, stale: bool = False) -> str:
    if stale:
        return "AI · —"
    if cpu < 0.5:
        return "AI · Quiet"
    if cpu < 15:
        return f"AI · {int(round(cpu))}%"
    return f"AI · Busy {int(round(cpu))}%"


def worry_level(total_cpu: float, total_rss_kb: int, top_cpu: float = 0.0) -> str:
    """green | yellow | red"""
    mem_gb = total_rss_kb / (1024.0 * 1024.0)
    if total_cpu >= 80 or mem_gb >= 4.0:
        return "red"
    if top_cpu >= 40 or total_cpu >= 15 or mem_gb >= 2.0:
        return "yellow"
    return "green"


def status_sentence(
    *,
    total_cpu: float,
    total_rss_kb: int,
    top_tool: Optional[str] = None,
    top_cpu: float = 0.0,
    stale: bool = False,
) -> str:
    if stale:
        return "The background monitor is not updating."
    level = worry_level(total_cpu, total_rss_kb, top_cpu)
    tool = tool_display_name(top_tool) if top_tool else None
    if level == "green":
        return "All AI tools look fine."
    if level == "yellow":
        if tool and top_cpu >= 5:
            return f"{tool} is working hard."
        return "Some AI tools are using noticeable resources."
    if tool:
        return f"AI tools are loading this Mac heavily — mainly {tool}."
    return "AI tools are loading this Mac heavily."


def _parse_buzz_label(label: str, session_id: str) -> Tuple[str, str]:
    """Return (channel, agent) from session fields."""
    # session_id: buzz:channel|agent
    sid = session_id or ""
    if sid.startswith("buzz:") and "|" in sid:
        rest = sid[len("buzz:") :]
        ch, ag = rest.split("|", 1)
        return ch, ag
    # label: channel · agent
    if " · " in (label or ""):
        parts = label.split(" · ", 1)
        return parts[0], parts[1]
    return label or "unknown", "worker"


def activity_display(session: Dict[str, Any]) -> str:
    """One human line for a session/activity."""
    app = session.get("app") or ""
    label = (session.get("label") or "").strip()
    sid = session.get("session_id") or ""

    if app == "Buzz":
        ch, ag = _parse_buzz_label(label, sid)
        return f"{channel_display_name(ch)} · {agent_display_name(ag)}"

    if app == "Grok":
        # Strip pid: labels → "Grok chat"
        if not label or label.startswith("pid") or re.match(r"^pid\d+", label):
            return "Grok chat"
        # polymath · uuid8 → Polymath
        if " · " in label:
            left = label.split(" · ", 1)[0]
            if not left.startswith("pid"):
                return f"Grok chat · {_title_words(left)}"
        return f"Grok chat · {_title_words(label)}"

    if app == "Claude CLI":
        if label and not label.startswith("pid"):
            return f"Anthropic CLI · {_title_words(label)}"
        return "Anthropic CLI"

    if app == "Claude Desktop":
        if label and not label.startswith("pid") and label not in (
            "Claude Desktop",
            "Claude app",
            "Co-Work",
        ):
            return f"Co-Work · {_title_words(label)}"
        return "Co-Work"

    if app == "OpenClaw":
        return "OpenClaw gateway"

    if app == "ChatGPT":
        return "ChatGPT app"

    if app == "Codex":
        if label and not label.startswith("pid") and not re.match(r"^pid\d+", label):
            return f"Codex · {_title_words(label)}"
        return "Codex chat"

    if app == "OpenAI CLI":
        if label and not label.startswith("pid"):
            return f"OpenAI CLI · {_title_words(label)}"
        return "OpenAI CLI"

    if app == "Cursor":
        if label and not label.startswith("pid") and not re.match(r"^pid\d+", label):
            return f"Cursor · {_title_words(label)}"
        return "Cursor workspace"

    if label:
        return f"{tool_display_name(app)} · {_title_words(label)}"
    return tool_display_name(app)


def aggregate_tools(
    sessions: Sequence[Dict[str, Any]],
    *,
    visible: Optional[Sequence[str]] = None,
    include_idle: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """List of {app, name, cpu, rss_kb, load, mem} sorted by cpu desc.

    visible: if set, only these tool ids (user prefs).
    include_idle: installed-but-not-running tools to show as Quiet.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for s in sessions:
        if s.get("alive") is False:
            continue
        app = s.get("app") or "Unknown"
        b = buckets.setdefault(app, {"app": app, "cpu": 0.0, "rss_kb": 0, "n": 0})
        b["cpu"] += float(s.get("cpu_pct") or 0)
        b["rss_kb"] += int(s.get("rss_kb") or 0)
        b["n"] = int(b.get("n") or 0) + 1
    if include_idle:
        for app in include_idle:
            if app not in buckets:
                buckets[app] = {"app": app, "cpu": 0.0, "rss_kb": 0, "n": 0}
    rows = []
    for app, b in buckets.items():
        if visible is not None and app not in visible:
            continue
        if b["cpu"] <= 0 and b["rss_kb"] <= 0 and app not in (include_idle or []):
            continue
        rows.append(
            {
                "app": app,
                "name": tool_display_name(app),
                "cpu": b["cpu"],
                "rss_kb": b["rss_kb"],
                "load": load_phrase(b["cpu"]),
                "mem": mem_short(b["rss_kb"]),
                "sessions": int(b.get("n") or 0),
            }
        )
    # Match menu bar: running/heaviest first; quiet installed last; then RSS → CPU
    rows.sort(
        key=lambda r: (
            0 if (r["rss_kb"] > 0 or r["cpu"] > 0 or int(r.get("sessions") or 0) > 0) else 1,
            -int(r["rss_kb"] or 0),
            -float(r["cpu"] or 0),
            r["name"],
        )
    )
    return rows


def buzz_project_groups(sessions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{project, workers: [{name, cpu, rss_kb, load, mem}]}] for Buzz only."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in sessions:
        if s.get("app") != "Buzz":
            continue
        if s.get("alive") is False:
            continue
        ch, ag = _parse_buzz_label(s.get("label") or "", s.get("session_id") or "")
        groups.setdefault(ch, []).append(
            {
                "agent": ag,
                "name": agent_display_name(ag),
                "cpu": float(s.get("cpu_pct") or 0),
                "rss_kb": int(s.get("rss_kb") or 0),
                "load": load_phrase(float(s.get("cpu_pct") or 0)),
                "mem": mem_short(int(s.get("rss_kb") or 0)),
                "activity": activity_display(s),
            }
        )
    out = []
    for ch, workers in sorted(groups.items(), key=lambda kv: -sum(w["cpu"] for w in kv[1])):
        workers.sort(key=lambda w: -w["cpu"])
        out.append(
            {
                "channel": ch,
                "project": channel_display_name(ch),
                "workers": workers,
            }
        )
    return out


def summarize_live(live: Dict[str, Any], *, stale: bool = False) -> Dict[str, Any]:
    """Full plain-English summary from live.json-shaped dict."""
    totals = live.get("totals") or {}
    top = live.get("top") or {}
    sessions = live.get("sessions") or []
    tools_meta = live.get("tools") or {}
    total_cpu = float(totals.get("cpu_pct") or 0)
    total_rss = int(totals.get("rss_kb") or 0)
    top_cpu = float(top.get("cpu_pct") or 0)
    top_app = top.get("app")

    visible = tools_meta.get("visible")
    installed = tools_meta.get("installed") or []
    running = tools_meta.get("running") or [
        s.get("app") for s in sessions if s.get("app")
    ]
    show_idle = bool(tools_meta.get("show_idle_installed", True))
    idle = []
    if show_idle and not stale:
        idle = [t for t in installed if t not in set(running)]
        if visible is not None:
            idle = [t for t in idle if t in visible]

    return {
        "chip": chip_text(total_cpu, stale=stale),
        "sentence": status_sentence(
            total_cpu=total_cpu,
            total_rss_kb=total_rss,
            top_tool=top_app,
            top_cpu=top_cpu,
            stale=stale,
        ),
        "memory_line": mem_phrase(total_rss) if not stale else "",
        "worry": worry_level(total_cpu, total_rss, top_cpu) if not stale else "stale",
        "tools": (
            aggregate_tools(sessions, visible=visible, include_idle=idle)
            if not stale
            else []
        ),
        "buzz_projects": buzz_project_groups(sessions) if not stale else [],
        "tools_meta": tools_meta,
        "total_cpu": total_cpu,
        "total_rss_kb": total_rss,
    }
