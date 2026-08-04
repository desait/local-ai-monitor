"""Live / once TUI — night-instrument panel for local AI load.

Visual direction (frontend-design, 2026-07-29):
  Subject: multi-agent workstation telemetry
  Job: glance — who is hot, heavy, token-hungry
  Identity: "night instrument" — cool chrome, heat-mapped load, product dots
  Signature: CPU heat color (cool→hot); product hue only on a leading mark
  Geometry: one fixed-column table for app and session grain
"""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from local_ai_monitor.classify import APPS, AppStats
from local_ai_monitor.collect_basic import collect, collect_sessions
from local_ai_monitor.sessionize import SessionStats
from local_ai_monitor.tokens import fmt_token_count

# --- Night instrument palette (256-color; not acid-green-on-black default) ---

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

PALETTE = {
    # chrome
    "brand": 147,       # soft violet — mark only
    "header": 245,      # cool gray labels
    "rule": 238,        # deep hairline
    "body": 252,        # primary read
    "muted": 244,       # secondary numbers (mem, #)
    "dead": 240,        # idle / empty
    "total": 255,       # total row
    "tokens": 116,      # teal data
    "footer": 242,
    # product marks (dot only — not the whole name)
    "Grok": 215,            # warm apricot
    "Claude CLI": 180,      # soft rose-clay
    "Claude Desktop": 180,
    "Buzz": 222,            # gold
    "OpenClaw": 75,         # steel blue
    "ChatGPT": 114,         # sage
    # load heat (signature)
    "heat_idle": 245,
    "heat_low": 151,        # mint
    "heat_mid": 185,        # amber
    "heat_high": 214,       # orange
    "heat_hot": 203,        # rose-red
}


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("LOCAL_AI_MONITOR_NO_COLOR") in ("1", "true", "yes"):
        return False
    return sys.stdout.isatty()


def c(
    text: str,
    *,
    fg: Optional[int] = None,
    bold: bool = False,
    dim: bool = False,
    use: bool = True,
) -> str:
    if not use:
        return text
    parts = []
    if bold:
        parts.append(_BOLD)
    if dim:
        parts.append(_DIM)
    if fg is not None:
        parts.append(f"\033[38;5;{fg}m")
    if not parts:
        return text
    return "".join(parts) + text + _RESET


def app_fg(app: str) -> int:
    return PALETTE.get(app, PALETTE["body"])


def heat_fg(cpu: float) -> int:
    """Signature: load temperature on CPU values."""
    if cpu < 0.5:
        return PALETTE["heat_idle"]
    if cpu < 5:
        return PALETTE["heat_low"]
    if cpu < 20:
        return PALETTE["heat_mid"]
    if cpu < 50:
        return PALETTE["heat_high"]
    return PALETTE["heat_hot"]


def _term_width() -> int:
    try:
        return max(48, os.get_terminal_size().columns)
    except OSError:
        return 80


# --- fixed table geometry ---
# Line = " " + NAME[name_w] + " " + CPU[6] + " " + MEM[5] + " " + #[4] + " " + TOK[9]
# metrics block (with its leading spaces) = 4 + 6+5+4+9 = 28
# full line width = 1 + name_w + 28 = name_w + 29

_CPU_W = 6
_MEM_W = 5
_N_W = 4
_TOK_W = 9
_METRICS = 4 + _CPU_W + _MEM_W + _N_W + _TOK_W  # 28
_LINE_OVERHEAD = 1 + _METRICS  # leading space + metrics = 29


def _clip(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def _pad(s: str, width: int, *, right: bool = False) -> str:
    s = _clip(s, width)
    if right:
        return s.rjust(width)
    return s.ljust(width)


def fmt_mem(rss_kb: int) -> str:
    """Always ≤5 chars: 12K 1.2M 265M 1.1G"""
    if rss_kb <= 0:
        return "—"
    mb = rss_kb / 1024.0
    if mb < 1:
        return f"{rss_kb:.0f}K"
    if mb < 10:
        return f"{mb:.1f}M"
    if mb < 1000:
        return f"{mb:.0f}M"
    gb = mb / 1024.0
    if gb < 10:
        return f"{gb:.1f}G"
    return f"{gb:.0f}G"


def fmt_cpu(pct: float) -> str:
    if pct <= 0:
        return "  0.0%"
    if pct >= 1000:
        return f"{pct:5.0f}%"
    return f"{pct:5.1f}%"


def fmt_tokens(s: SessionStats) -> str:
    """≤9 chars: 12M·33% | 364k | —"""
    tot = None
    if s.tokens_total is not None and s.tokens_total > 0:
        tot = fmt_token_count(s.tokens_total)
    elif s.tokens_in or s.tokens_out:
        tot = fmt_token_count((s.tokens_in or 0) + (s.tokens_out or 0))
    pct = None
    if s.context_used is not None and s.context_window:
        pct = int(round(100.0 * s.context_used / max(s.context_window, 1)))
    if tot and pct is not None:
        text = f"{tot}·{pct}%"
    elif tot:
        text = tot
    elif pct is not None:
        text = f"{pct}%ctx"
    else:
        text = "—"
    return _clip(text, _TOK_W)


def fmt_tokens_app(sessions: Sequence[SessionStats]) -> str:
    """Roll tokens for app grain."""
    total = 0
    any_t = False
    ctx_u = 0
    ctx_w = 0
    for s in sessions:
        if s.tokens_total:
            total += s.tokens_total
            any_t = True
        if s.context_used and s.context_window:
            # show max context % among sessions (not sum)
            if s.context_window > ctx_w:
                ctx_w = s.context_window
                ctx_u = s.context_used
    fake = SessionStats(app="x", session_id="x", label="x")
    if any_t:
        fake.tokens_total = total
    if ctx_w:
        fake.context_used = ctx_u
        fake.context_window = ctx_w
    return fmt_tokens(fake)


@dataclass
class Row:
    name: str
    app: str  # for color
    cpu: float
    rss_kb: int
    nproc: int
    tokens: str
    tokens_total: int = 0  # raw for TOTAL
    dim: bool = False  # idle / empty


def _name_width(term_w: int) -> int:
    return max(14, min(40, term_w - _LINE_OVERHEAD))


def _rule(width: int, use_color: bool) -> str:
    """Hairline rule — mid-dot rhythm, not a solid wall of dashes."""
    # " ·" pattern feels like instrument tick marks
    n = max(8, width)
    # keep exact width with thin line (cleaner scan than ··· noise at large width)
    return c(" " + "─" * n, fg=PALETTE["rule"], use=use_color)


def _title_line(title: str, w: int, use_color: bool) -> str:
    """Brand mark + dim chrome title."""
    # Signature brand glyph (not the whole row)
    mark = c(" ◈", fg=PALETTE["brand"], bold=True, use=use_color)
    # title like "local-ai-monitor  12:04  ·  9 sessions"
    rest = c(_clip("  " + title, max(0, w - 2)), fg=PALETTE["header"], use=use_color)
    return mark + rest


def _col_header(name_w: int, w: int, use_color: bool) -> str:
    hdr = (
        f" {_pad('NAME', name_w)}"
        f" {_pad('CPU', _CPU_W, right=True)}"
        f" {_pad('MEM', _MEM_W, right=True)}"
        f" {_pad('#', _N_W, right=True)}"
        f" {_pad('TOKENS', _TOK_W, right=True)}"
    )
    return c(_clip(hdr, w), fg=PALETTE["header"], dim=True, use=use_color)


def _format_name_cell(r: Row, name_w: int, use_color: bool) -> str:
    """Product color on a mark only; name body is readable body ink.

    Geometry: leading space + name_w chars = same as plain ` {name:<name_w}`.
    Within name_w: '● ' (2) + text (name_w-2).
    """
    text_w = max(1, name_w - 2)
    body = _pad(r.name, text_w)
    if r.dim:
        return (
            " "
            + c("●", fg=PALETTE["dead"], use=use_color)
            + " "
            + c(body, fg=PALETTE["dead"], use=use_color)
        )
    return (
        " "
        + c("●", fg=app_fg(r.app), bold=True, use=use_color)
        + " "
        + c(body, fg=PALETTE["body"], use=use_color)
    )


def _format_metrics(r: Row, use_color: bool) -> str:
    cpu_s = _pad(fmt_cpu(r.cpu), _CPU_W, right=True)
    mem_s = _pad(fmt_mem(r.rss_kb) if r.rss_kb or r.nproc else "—", _MEM_W, right=True)
    n_s = _pad(str(r.nproc) if r.nproc else "—", _N_W, right=True)
    tok_s = _pad(r.tokens, _TOK_W, right=True)
    if r.dim:
        return c(f" {cpu_s} {mem_s} {n_s} {tok_s}", fg=PALETTE["dead"], use=use_color)
    # Heat on CPU (signature); mem/# muted; tokens teal
    return (
        " "
        + c(cpu_s, fg=heat_fg(r.cpu), bold=True, use=use_color)
        + " "
        + c(mem_s, fg=PALETTE["muted"], use=use_color)
        + " "
        + c(n_s, fg=PALETTE["muted"], use=use_color)
        + " "
        + c(
            tok_s,
            fg=PALETTE["tokens"] if r.tokens not in ("—", "") else PALETTE["dead"],
            use=use_color,
        )
    )


def render_table(
    rows: List[Row],
    *,
    title: str,
    use_color: bool,
    term_w: Optional[int] = None,
) -> str:
    w = term_w if term_w is not None else _term_width()
    name_w = _name_width(w)
    rule_w = min(w - 1, name_w + _LINE_OVERHEAD)
    lines: List[str] = []

    lines.append(_title_line(title, w, use_color))
    lines.append(_col_header(name_w, w, use_color))
    lines.append(_rule(rule_w, use_color))

    if not rows:
        lines.append(c("  nothing running", fg=PALETTE["dead"], dim=True, use=use_color))
    else:
        for r in rows:
            lines.append(_format_name_cell(r, name_w, use_color) + _format_metrics(r, use_color))

    lines.append(_rule(rule_w, use_color))
    return "\n".join(lines)


def _total_row(rows: List[Row], *, use_color: bool, term_w: int) -> str:
    name_w = _name_width(term_w)
    cpu = sum(r.cpu for r in rows if not r.dim)
    rss = sum(r.rss_kb for r in rows if not r.dim)
    n = sum(r.nproc for r in rows if not r.dim)
    tok_sum = sum(r.tokens_total for r in rows if not r.dim)
    tok = fmt_token_count(tok_sum) if tok_sum else "—"
    tok = _clip(tok, _TOK_W)
    # TOTAL: same geometry as name cell (●-width slot → brand mark)
    label = _pad("TOTAL", max(1, name_w - 2))
    left = (
        " "
        + c("▣", fg=PALETTE["brand"], bold=True, use=use_color)
        + " "
        + c(label, fg=PALETTE["total"], bold=True, use=use_color)
    )
    metrics = (
        " "
        + c(_pad(fmt_cpu(cpu), _CPU_W, right=True), fg=heat_fg(cpu), bold=True, use=use_color)
        + " "
        + c(_pad(fmt_mem(rss) if rss else "—", _MEM_W, right=True), fg=PALETTE["body"], bold=True, use=use_color)
        + " "
        + c(_pad(str(n) if n else "—", _N_W, right=True), fg=PALETTE["body"], bold=True, use=use_color)
        + " "
        + c(_pad(tok, _TOK_W, right=True), fg=PALETTE["tokens"], bold=True, use=use_color)
    )
    return left + metrics


def sessions_to_rows(sessions: Sequence[SessionStats]) -> List[Row]:
    """One row per session.

    Sort: app family (APPS order) so Buzz channel/agent rows stay together,
    then CPU desc within family.
    """
    app_rank = {name: i for i, name in enumerate(APPS)}
    ordered = sorted(
        sessions,
        key=lambda s: (
            app_rank.get(s.app, 99),
            -s.pcpu,
            s.label or s.session_id,
        ),
    )
    rows: List[Row] = []
    for s in ordered:
        if s.nproc == 0:
            continue
        label = (s.label or s.session_id or "").strip()
        # Buzz labels are already "channel · agent"
        app_short = {
            "Claude CLI": "Claude",
            "Claude Desktop": "Claude.app",
            "OpenClaw": "OpenClaw",
            "ChatGPT": "ChatGPT",
            "Buzz": "Buzz",
        }.get(s.app, s.app)
        if s.app == "Buzz" and label:
            # e.g. "Buzz · sample-app · Operator/grok"
            name = f"Buzz · {label}"
        elif label and label.casefold() != s.app.casefold():
            name = f"{app_short} · {label}"
        else:
            name = app_short
        rows.append(
            Row(
                name=name,
                app=s.app,
                cpu=s.pcpu,
                rss_kb=s.rss_kb,
                nproc=s.nproc,
                tokens=fmt_tokens(s),
                tokens_total=int(s.tokens_total or 0),
            )
        )
    return rows


def apps_to_rows(stats: Dict[str, AppStats], sessions: Optional[Sequence[SessionStats]] = None) -> List[Row]:
    """One row per active app only (no empty placeholders)."""
    by_app_sessions: Dict[str, List[SessionStats]] = {}
    if sessions:
        for s in sessions:
            by_app_sessions.setdefault(s.app, []).append(s)

    rows: List[Row] = []
    # sort by cpu desc among active
    active = []
    for name in APPS:
        s = stats.get(name) or AppStats(name=name)
        if s.nproc == 0:
            continue
        active.append((name, s))
    active.sort(key=lambda t: -t[1].pcpu)

    for name, s in active:
        toks = "—"
        tok_sum = 0
        if name in by_app_sessions:
            toks = fmt_tokens_app(by_app_sessions[name])
            tok_sum = sum(int(x.tokens_total or 0) for x in by_app_sessions[name])
        rows.append(
            Row(
                name=name,
                app=name,
                cpu=s.pcpu,
                rss_kb=s.rss_kb,
                nproc=s.nproc,
                tokens=toks,
                tokens_total=tok_sum,
            )
        )
    return rows


def filter_apps_cycle(current: Optional[str]) -> Optional[str]:
    order: List[Optional[str]] = [None] + list(APPS)
    try:
        i = order.index(current)
    except ValueError:
        return None
    return order[(i + 1) % len(order)]


def apply_app_filter_sessions(
    sessions: Sequence[SessionStats], app_filter: Optional[str]
) -> List[SessionStats]:
    if not app_filter:
        return list(sessions)
    return [s for s in sessions if s.app == app_filter]


def apply_app_filter_apps(
    stats: Dict[str, AppStats], app_filter: Optional[str]
) -> Dict[str, AppStats]:
    if not app_filter:
        return stats
    return {k: v for k, v in stats.items() if k == app_filter}


def render(
    stats: Dict[str, AppStats],
    *,
    show_threads: bool = False,  # kept for API compat; unused in simplified table
    app_filter: Optional[str] = None,
    use_color: Optional[bool] = None,
    sessions: Optional[Sequence[SessionStats]] = None,
) -> str:
    if use_color is None:
        use_color = color_enabled()
    stats = apply_app_filter_apps(stats, app_filter)
    rows = apps_to_rows(stats, sessions=sessions)
    w = _term_width()
    now = time.strftime("%H:%M:%S")
    filt = f" · {app_filter}" if app_filter else ""
    title = f"local-ai-monitor  {now}  ·  by app{filt}"
    body = render_table(rows, title=title, use_color=use_color, term_w=w)
    lines = [body, _total_row(rows, use_color=use_color, term_w=w), ""]
    lines.append(
        c(
            _clip("  q  r  s sessions  a apps  f filter  h history  ?", w),
            fg=PALETTE["footer"],
            dim=True,
            use=use_color,
        )
    )
    return "\n".join(lines)


def render_sessions(
    sessions: List[SessionStats],
    *,
    show_threads: bool = False,
    app_filter: Optional[str] = None,
    use_color: Optional[bool] = None,
) -> str:
    if use_color is None:
        use_color = color_enabled()
    sessions = apply_app_filter_sessions(sessions, app_filter)
    rows = sessions_to_rows(sessions)
    w = _term_width()
    now = time.strftime("%H:%M:%S")
    filt = f" · {app_filter}" if app_filter else ""
    n = len(rows)
    title = f"local-ai-monitor  {now}  ·  {n} session{'s' if n != 1 else ''}{filt}"
    body = render_table(rows, title=title, use_color=use_color, term_w=w)
    lines = [body, _total_row(rows, use_color=use_color, term_w=w), ""]
    lines.append(
        c(
            _clip("  q  r  s sessions  a apps  f filter  h history  ?", w),
            fg=PALETTE["footer"],
            dim=True,
            use=use_color,
        )
    )
    return "\n".join(lines)


def render_help(*, use_color: Optional[bool] = None) -> str:
    if use_color is None:
        use_color = color_enabled()
    lines = [
        c(" ◈", fg=PALETTE["brand"], bold=True, use=use_color)
        + c("  local-ai-monitor", fg=PALETTE["body"], bold=True, use=use_color),
        c(" " + "─" * 36, fg=PALETTE["rule"], use=use_color),
        c("  q", fg=PALETTE["brand"], use=use_color) + "  quit",
        c("  r", fg=PALETTE["brand"], use=use_color) + "  refresh",
        c("  s", fg=PALETTE["brand"], use=use_color) + "  sessions (default)",
        c("  a", fg=PALETTE["brand"], use=use_color) + "  apps (same columns)",
        c("  f", fg=PALETTE["brand"], use=use_color) + "  filter product",
        c("  h", fg=PALETTE["brand"], use=use_color) + "  history 24h",
        c("  ?", fg=PALETTE["brand"], use=use_color) + "  help",
        "",
        c("  CPU color = load heat", fg=PALETTE["header"], dim=True, use=use_color),
        c("  TOKENS = Σ local usage · N% context", fg=PALETTE["header"], dim=True, use=use_color),
        "",
        c("  any key returns", fg=PALETTE["footer"], dim=True, use=use_color),
    ]
    return "\n".join(lines)


def render_history(
    rows: List[dict],
    *,
    summary: Optional[List[dict]] = None,
    hours: float = 24.0,
    use_color: Optional[bool] = None,
    offset: int = 0,
    page_size: int = 24,
) -> str:
    if use_color is None:
        use_color = color_enabled()
    summary = summary or []
    w = _term_width()
    name_w = max(12, min(28, w - 40))
    lines: List[str] = []
    lines.append(
        c(
            _clip(f" history · {hours:.0f}h · page {offset // page_size + 1}", w),
            fg=PALETTE["header"],
            use=use_color,
        )
    )
    lines.append(c(" " + "─" * min(w - 1, 60), fg=PALETTE["rule"], use=use_color))

    if summary:
        for s in summary:
            app = str(s.get("app") or "")
            cpu_s = float(s.get("cpu_seconds") or 0)
            peak_mb = (s.get("peak_rss_kb") or 0) / 1024.0
            line = (
                f" {_pad(app, name_w)}"
                f" {cpu_s:8.1f}s"
                f"  peak {_pad(f'{peak_mb:.0f}M', 5, right=True)}"
            )
            lines.append(
                c(_clip(line, w), fg=app_fg(app), bold=True, use=use_color)
            )
    else:
        lines.append(c(" (no rollups yet)", fg=PALETTE["dead"], use=use_color))

    lines.append(c(" " + "─" * min(w - 1, 60), fg=PALETTE["rule"], use=use_color))
    if not rows:
        lines.append(c(" (collector not writing history)", fg=PALETTE["dead"], use=use_color))
    else:
        page = rows[offset : offset + page_size]
        for r in page:
            peak_mb = (r.get("peak_rss_kb") or 0) / 1024.0
            label = (r.get("label") or r.get("session_id") or "")[: name_w]
            app = str(r.get("app") or "")[:10]
            bucket = str(r.get("bucket_start", ""))[11:16]  # HH:MM
            cpu_s = float(r.get("cpu_seconds") or 0)
            line = f" {bucket} {_pad(app, 10)} {_pad(str(label), name_w)} {cpu_s:7.1f}s"
            lines.append(_clip(line, w))
        if len(rows) > offset + page_size:
            lines.append(
                c(
                    f"  +{len(rows) - offset - page_size} more  (n/p)",
                    fg=PALETTE["dead"],
                    use=use_color,
                )
            )

    lines.append("")
    lines.append(
        c(" h back  n/p page  q quit", fg=PALETTE["header"], use=use_color)
    )
    return "\n".join(lines)


def footer_live_status(use_color: Optional[bool] = None) -> str:
    if use_color is None:
        use_color = color_enabled()
    try:
        from local_ai_monitor.store import LiveStore, parse_local_iso

        live = LiveStore().read()
        if not live:
            return c("  collector off", fg=PALETTE["dead"], dim=True, use=use_color)
        ts = parse_local_iso(str(live.get("ts") or ""))
        age = time.time() - ts if ts else None
        interval = float(live.get("sample_interval_s") or 10)
        totals = live.get("totals") or {}
        stale = age is not None and age > 3 * interval
        age_s = f"{age:.0f}s" if age is not None else "?"
        flag = "stale" if stale else "live"
        cpu = float(totals.get("cpu_pct") or 0)
        ns = int(totals.get("nsessions") or 0)
        dot = c(" ●", fg=PALETTE["heat_hot"] if stale else PALETTE["heat_low"], use=use_color)
        rest = c(
            f"  collector {flag}  ·  {age_s}  ·  {cpu:.0f}%  ·  {ns} sess",
            fg=PALETTE["footer"],
            dim=True,
            use=use_color,
        )
        return dot + rest
    except Exception:
        return ""


def sessions_json_payload(sessions: List[SessionStats]) -> dict:
    return {
        "version": 2,
        "view": "session",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sessions": [
            {
                "app": s.app,
                "session_id": s.session_id,
                "label": s.label,
                "detail": s.detail,
                "cpu_pct": round(s.pcpu, 2),
                "rss_mb": round(s.rss_kb / 1024.0, 1),
                "processes": s.nproc,
                "threads": s.threads,
                "pids": sorted(s.pids),
                "tokens_in": s.tokens_in,
                "tokens_out": s.tokens_out,
                "tokens_total": s.tokens_total,
                "tokens_cached": s.tokens_cached,
                "context_used": s.context_used,
                "context_window": s.context_window,
                "tokens_source": s.tokens_source,
            }
            for s in sessions
        ],
    }


def apps_json_payload(stats: Dict[str, AppStats]) -> dict:
    return {
        name: {
            "cpu_pct": round(s.pcpu, 2),
            "mem_pct": round(s.pmem, 2),
            "rss_mb": round(s.rss_kb / 1024.0, 1),
            "processes": s.nproc,
            "threads": s.threads,
            "pids": sorted(s.pids),
            "samples": s.samples,
        }
        for name, s in stats.items()
    }


def _clear() -> None:
    sys.stdout.write("\033[H\033[2J")
    sys.stdout.flush()


def _load_history(hours: float = 24.0) -> Tuple[List[dict], List[dict]]:
    try:
        from local_ai_monitor.store import HistoryStore

        hist = HistoryStore()
        if not os.path.isfile(hist.db_path):
            return [], []
        rows = hist.query(hours=hours, limit=120)
        summary = hist.summarize_by_app(hours=hours)
        hist.close()
        return rows, summary
    except Exception:
        return [], []


def live(
    interval: float,
    once: bool,
    as_json: bool,
    no_threads: bool = False,
    view: str = "session",
    threads: bool = False,
) -> int:
    stop = False
    view = view if view in ("app", "session") else "session"
    include_threads = bool(threads)
    show_threads_col = bool(threads)
    app_filter: Optional[str] = None
    overlay: Optional[str] = None
    history_offset = 0
    history_page = 24

    def _sig(_s, _f):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    use_keys = sys.stdin.isatty() and not once and not as_json
    old_settings = None
    if use_keys:
        try:
            import termios
            import tty

            old_settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            use_keys = False
            old_settings = None

    try:
        while not stop:
            if overlay == "help":
                text = render_help()
            elif overlay == "history":
                rows, summary = _load_history(24.0)
                if history_offset >= len(rows) and rows:
                    history_offset = max(0, len(rows) - history_page)
                text = render_history(
                    rows,
                    summary=summary,
                    hours=24.0,
                    offset=history_offset,
                    page_size=history_page,
                )
            else:
                # Always sessionize once; app view rolls up (same sample, tokens)
                sessions = collect_sessions(include_threads=include_threads)
                if as_json and view == "session":
                    print(json.dumps(sessions_json_payload(sessions), indent=2))
                    return 0
                if view == "session":
                    text = render_sessions(
                        sessions,
                        show_threads=show_threads_col,
                        app_filter=app_filter,
                    )
                else:
                    # roll sessions → AppStats for display + keep sessions for tokens
                    from local_ai_monitor.sessionize import sessions_to_app_stats

                    stats = sessions_to_app_stats(sessions)
                    if as_json:
                        payload = apps_json_payload(stats)
                        if not include_threads:
                            for v in payload.values():
                                v["threads"] = None
                        print(json.dumps(payload, indent=2))
                        return 0
                    text = render(
                        stats,
                        show_threads=show_threads_col,
                        app_filter=app_filter,
                        sessions=sessions,
                    )
                foot = footer_live_status()
                if foot:
                    text = text + "\n" + foot

            if once:
                print(text)
                return 0

            _clear()
            print(text)
            sys.stdout.flush()

            end = time.time() + max(interval, 0.3)
            while time.time() < end and not stop:
                if use_keys:
                    r, _, _ = select.select([sys.stdin], [], [], 0.15)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch in ("q", "Q", "\x03"):
                            stop = True
                            break
                        if ch in ("r", "R"):
                            if overlay == "help":
                                overlay = None
                            break
                        if ch in ("a", "A"):
                            view = "app"
                            overlay = None
                            break
                        if ch in ("s", "S"):
                            view = "session"
                            overlay = None
                            break
                        if ch in ("f", "F"):
                            app_filter = filter_apps_cycle(app_filter)
                            overlay = None
                            break
                        if ch in ("t", "T"):
                            include_threads = not include_threads
                            show_threads_col = include_threads
                            break
                        if ch in ("h", "H"):
                            if overlay == "history":
                                overlay = None
                            else:
                                overlay = "history"
                                history_offset = 0
                            break
                        if ch in ("n", "N") and overlay == "history":
                            history_offset += history_page
                            break
                        if ch in ("p", "P") and overlay == "history":
                            history_offset = max(0, history_offset - history_page)
                            break
                        if ch in ("?", "/"):
                            overlay = "help" if overlay != "help" else None
                            break
                else:
                    time.sleep(min(0.2, max(0.0, end - time.time())))
    finally:
        if old_settings is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        if not once and not as_json:
            print()
    return 0
