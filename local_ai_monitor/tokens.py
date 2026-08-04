"""Per-session token usage from local session stores (not live API).

Sources (read-only, mtime-cached):
  Grok:   ~/.grok/sessions/<enc>/<uuid>/signals.json  (context)
          .../updates.jsonl  turn_completed.usage     (API in/out/total)
  Claude: ~/.claude/projects/<path-encoded>/*.jsonl   usage blocks

Buzz / OpenClaw / ChatGPT / Desktop: no local token ledger → empty.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from local_ai_monitor.sessionize import SessionStats

GROK_SESSIONS = os.path.expanduser("~/.grok/sessions")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


@dataclass
class TokenUsage:
    """Token metrics for one session. All counts optional."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None  # session API volume (sum of turn totals)
    cached_tokens: Optional[int] = None
    context_used: Optional[int] = None
    context_window: Optional[int] = None
    source: str = ""  # grok|claude|…

    @property
    def has_any(self) -> bool:
        return any(
            v is not None
            for v in (
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
                self.context_used,
            )
        )

    def apply_to(self, s: SessionStats) -> None:
        s.tokens_in = self.input_tokens
        s.tokens_out = self.output_tokens
        s.tokens_total = self.total_tokens
        s.tokens_cached = self.cached_tokens
        s.context_used = self.context_used
        s.context_window = self.context_window
        s.tokens_source = self.source or None


# path/mtime/size → result
_cache: Dict[str, Tuple[float, int, Any]] = {}
_grok_uuid_index: Dict[str, str] = {}  # uuid → session dir
_grok_index_built_at: float = 0.0


def _cached_load_json(path: str) -> Optional[Any]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = "json:" + path
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    _cache[key] = (st.st_mtime, st.st_size, data)
    return data


def _cached_grok_turn_usage(updates_path: str) -> Tuple[int, int, int, int]:
    """Sum turn_completed.usage over updates.jsonl. Returns in, out, total, cached."""
    try:
        st = os.stat(updates_path)
    except OSError:
        return (0, 0, 0, 0)
    key = "grok_turns:" + updates_path
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]  # type: ignore[return-value]

    tin = tout = ttot = tcache = 0
    n = 0
    try:
        with open(updates_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "turn_completed" not in line or "inputTokens" not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                upd = (o.get("params") or {}).get("update") or {}
                if upd.get("sessionUpdate") != "turn_completed":
                    continue
                u = upd.get("usage") or {}
                if not isinstance(u, dict):
                    continue
                tin += int(u.get("inputTokens") or 0)
                tout += int(u.get("outputTokens") or 0)
                ttot += int(u.get("totalTokens") or 0)
                tcache += int(u.get("cachedReadTokens") or 0)
                n += 1
    except OSError:
        return (0, 0, 0, 0)

    result = (tin, tout, ttot, tcache) if n else (0, 0, 0, 0)
    _cache[key] = (st.st_mtime, st.st_size, result)
    return result


def _cached_claude_usage(jsonl_path: str) -> Tuple[int, int, int, int]:
    """Sum usage blocks in a Claude transcript jsonl."""
    try:
        st = os.stat(jsonl_path)
    except OSError:
        return (0, 0, 0, 0)
    key = "claude:" + jsonl_path
    hit = _cache.get(key)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]  # type: ignore[return-value]

    tin = tout = tcache = 0
    n = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "input_tokens" not in line and "output_tokens" not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for u in _iter_usage_dicts(o):
                    # Prefer message-level usage; skip nested iteration duplicates
                    # by only counting dicts that have input_tokens and are not type=message only nested
                    tin += int(u.get("input_tokens") or 0)
                    tout += int(u.get("output_tokens") or 0)
                    tcache += int(
                        u.get("cache_read_input_tokens")
                        or u.get("cache_read_tokens")
                        or 0
                    )
                    n += 1
    except OSError:
        return (0, 0, 0, 0)

    # Claude transcripts often nest usage under message AND top-level — can double.
    # Prefer: if double-counting suspected (n large), use half? Better: only count
    # usage at message.usage path.
    tin2 = tout2 = tcache2 = 0
    n2 = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "input_tokens" not in line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = o.get("message") if isinstance(o, dict) else None
                if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                    u = msg["usage"]
                    tin2 += int(u.get("input_tokens") or 0)
                    tout2 += int(u.get("output_tokens") or 0)
                    tcache2 += int(u.get("cache_read_input_tokens") or 0)
                    n2 += 1
                elif isinstance(o, dict) and isinstance(o.get("usage"), dict):
                    u = o["usage"]
                    # only if no message.usage already counted style
                    if "input_tokens" in u:
                        tin2 += int(u.get("input_tokens") or 0)
                        tout2 += int(u.get("output_tokens") or 0)
                        tcache2 += int(u.get("cache_read_input_tokens") or 0)
                        n2 += 1
    except OSError:
        pass

    if n2 > 0:
        tin, tout, tcache, n = tin2, tout2, tcache2, n2

    total = tin + tout
    result = (tin, tout, total, tcache) if n else (0, 0, 0, 0)
    _cache[key] = (st.st_mtime, st.st_size, result)
    return result


def _iter_usage_dicts(obj: Any):
    if isinstance(obj, dict):
        if "input_tokens" in obj or "output_tokens" in obj:
            yield obj
        for v in obj.values():
            yield from _iter_usage_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_usage_dicts(v)


def _rebuild_grok_index(force: bool = False) -> None:
    global _grok_index_built_at, _grok_uuid_index
    now = os.path.getmtime(GROK_SESSIONS) if os.path.isdir(GROK_SESSIONS) else 0.0
    # rebuild at most every 30s unless forced
    if not force and _grok_uuid_index and (now - _grok_index_built_at) < 30:
        return
    idx: Dict[str, str] = {}
    if not os.path.isdir(GROK_SESSIONS):
        _grok_uuid_index = idx
        _grok_index_built_at = now
        return
    try:
        for enc in os.listdir(GROK_SESSIONS):
            base = os.path.join(GROK_SESSIONS, enc)
            if not os.path.isdir(base):
                continue
            try:
                for uuid in os.listdir(base):
                    p = os.path.join(base, uuid)
                    if os.path.isdir(p) and len(uuid) >= 20:
                        idx[uuid] = p
            except OSError:
                continue
    except OSError:
        pass
    _grok_uuid_index = idx
    _grok_index_built_at = now


def find_grok_session_dir(session_id: str, detail: str = "") -> Optional[str]:
    """Resolve ~/.grok/sessions/.../<uuid> from session_id (uuid) and optional cwd detail."""
    if session_id and not session_id.startswith("pid:"):
        uuid = session_id
        # Fast path: detail is cwd
        if detail and detail.startswith("/"):
            enc = urllib.parse.quote(detail, safe="")
            p = os.path.join(GROK_SESSIONS, enc, uuid)
            if os.path.isdir(p):
                return p
        _rebuild_grok_index()
        hit = _grok_uuid_index.get(uuid)
        if hit:
            return hit
    # Fallback: newest session under this cwd (pid:start keys still get tokens)
    return find_grok_session_dir_by_cwd(detail)


def find_grok_session_dir_by_cwd(cwd: str) -> Optional[str]:
    if not cwd or not cwd.startswith("/"):
        return None
    enc = urllib.parse.quote(cwd, safe="")
    base = os.path.join(GROK_SESSIONS, enc)
    if not os.path.isdir(base):
        return None
    best = None
    best_m = -1.0
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if not os.path.isdir(p) or len(name) < 20:
                continue
            # prefer signals / events mtime
            for fn in ("signals.json", "updates.jsonl", "events.jsonl"):
                fp = os.path.join(p, fn)
                try:
                    m = os.path.getmtime(fp)
                except OSError:
                    continue
                if m > best_m:
                    best_m = m
                    best = p
    except OSError:
        return None
    return best


def grok_tokens(session_id: str, detail: str = "") -> TokenUsage:
    d = find_grok_session_dir(session_id, detail)
    if not d:
        return TokenUsage()
    usage = TokenUsage(source="grok")
    sig = _cached_load_json(os.path.join(d, "signals.json"))
    if isinstance(sig, dict):
        cu = sig.get("contextTokensUsed")
        cw = sig.get("contextWindowTokens")
        if isinstance(cu, (int, float)):
            usage.context_used = int(cu)
        if isinstance(cw, (int, float)):
            usage.context_window = int(cw)
    updates = os.path.join(d, "updates.jsonl")
    tin, tout, ttot, tcache = _cached_grok_turn_usage(updates)
    if ttot or tin or tout:
        usage.input_tokens = tin
        usage.output_tokens = tout
        usage.total_tokens = ttot if ttot else (tin + tout)
        usage.cached_tokens = tcache or None
    return usage


def claude_project_dir(cwd: str) -> Optional[str]:
    if not cwd or not cwd.startswith("/"):
        return None
    if not os.path.isdir(CLAUDE_PROJECTS):
        return None
    # Claude encodes absolute paths as -Users-foo-bar (slash AND spaces → -)
    enc = cwd.replace("/", "-").replace(" ", "-")
    p = os.path.join(CLAUDE_PROJECTS, enc)
    if os.path.isdir(p):
        return p
    enc2 = cwd.replace("/", "-")
    p2 = os.path.join(CLAUDE_PROJECTS, enc2)
    if os.path.isdir(p2):
        return p2

    # Fuzzy: `_` vs `-` (cwd sample_project vs dir sample-project)
    def norm(s: str) -> str:
        return s.replace("_", "-").casefold()

    target = norm(enc)
    try:
        names = os.listdir(CLAUDE_PROJECTS)
    except OSError:
        return None
    for name in names:
        if norm(name) == target:
            return os.path.join(CLAUDE_PROJECTS, name)
    # basename suffix match
    base = norm(os.path.basename(cwd.rstrip("/")))
    if base:
        hits = [
            name
            for name in names
            if norm(name).endswith(base) or base in norm(name)
        ]
        if len(hits) == 1:
            return os.path.join(CLAUDE_PROJECTS, hits[0])
        # prefer hyphenated exact-ish longest match
        if hits:
            hits.sort(key=lambda n: (norm(n) == target, len(n)), reverse=True)
            return os.path.join(CLAUDE_PROJECTS, hits[0])
    return None


def _newest_claude_jsonl(project_dir: str) -> Optional[str]:
    newest = None
    newest_m = -1.0
    try:
        for name in os.listdir(project_dir):
            if not name.endswith(".jsonl"):
                continue
            if name.startswith("."):
                continue
            p = os.path.join(project_dir, name)
            if not os.path.isfile(p):
                continue
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > newest_m:
                newest_m = m
                newest = p
    except OSError:
        return None
    return newest


def claude_tokens(detail: str = "", label: str = "") -> TokenUsage:
    cwd = detail if detail.startswith("/") else ""
    if not cwd and label and not label.startswith("pid"):
        # cannot resolve without path
        return TokenUsage()
    pdir = claude_project_dir(cwd) if cwd else None
    if not pdir:
        return TokenUsage()
    jsonl = _newest_claude_jsonl(pdir)
    if not jsonl:
        return TokenUsage()
    tin, tout, ttot, tcache = _cached_claude_usage(jsonl)
    if not (tin or tout or ttot):
        return TokenUsage()
    return TokenUsage(
        input_tokens=tin,
        output_tokens=tout,
        total_tokens=ttot,
        cached_tokens=tcache or None,
        source="claude",
    )


def enrich_sessions_with_tokens(sessions: List[SessionStats]) -> List[SessionStats]:
    """Mutate sessions in place with token fields. Cheap when caches warm."""
    from local_ai_monitor.sessionize import lsof_cwd_for_pid

    for s in sessions:
        usage = TokenUsage()
        if s.app == "Grok":
            detail = s.detail or ""
            if not detail and s.pids:
                # Recover cwd when sessionize fell back to pid: without path
                try:
                    root_pid = min(s.pids)
                    detail = lsof_cwd_for_pid(root_pid) or ""
                    if detail and not s.detail:
                        s.detail = detail
                except Exception:
                    detail = ""
            usage = grok_tokens(s.session_id, detail)
        elif s.app == "Claude CLI":
            usage = claude_tokens(s.detail or "", s.label or "")
        # else: no source
        if usage.has_any:
            usage.apply_to(s)
    return sessions


def fmt_token_count(n: Optional[int]) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n / 1000:.0f}k"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000:.0f}M"


def fmt_tokens_cell(s: SessionStats, width: int = 12) -> str:
    """Compact cell: prefer Σ total; append ctx%% if known."""
    parts: List[str] = []
    if s.tokens_total is not None and s.tokens_total > 0:
        parts.append(fmt_token_count(s.tokens_total))
    elif s.tokens_in is not None or s.tokens_out is not None:
        inn = fmt_token_count(s.tokens_in or 0)
        out = fmt_token_count(s.tokens_out or 0)
        parts.append(f"{inn}↑{out}↓")
    if s.context_used is not None and s.context_window:
        pct = int(round(100.0 * s.context_used / max(s.context_window, 1)))
        if parts:
            parts.append(f"{pct}%")
        else:
            parts.append(
                f"{fmt_token_count(s.context_used)}/{fmt_token_count(s.context_window)}"
            )
    elif s.context_used is not None and not parts:
        parts.append(f"{fmt_token_count(s.context_used)} ctx")
    if not parts:
        return "—".rjust(width)[:width]
    text = " ".join(parts)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.rjust(width)
