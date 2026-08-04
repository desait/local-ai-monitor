"""Claude Code rate limits — session (~5h) and weekly (seven_day).

Sources (local, fail-closed):
  1. Session jsonl rows: error=rate_limit, apiErrorStatus=429, synthetic text
     "You've hit your weekly limit · resets 3pm (America/Chicago)"
     "You've hit your session limit · resets 11:50pm (America/Chicago)"
  2. Stream/jsonl rate_limit_info:
     {status, resetsAt (unix sec), rateLimitType: seven_day|..., utilization}
  3. ~/.claude/sessions/<pid>.json → sessionId + cwd for live processes

No inventing remaining %. Only emit when a limit event or structured info exists.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

CLAUDE_HOME = os.path.expanduser("~/.claude")
CLAUDE_PROJECTS = os.path.join(CLAUDE_HOME, "projects")
CLAUDE_SESSIONS = os.path.join(CLAUDE_HOME, "sessions")

# "You've hit your weekly limit · resets 3pm (America/Chicago)"
# "You've hit your session limit · resets 11:50pm (America/Chicago)"
_LIMIT_MSG_RE = re.compile(
    r"You've hit your\s+(session|weekly)\s+limit\s*[·•\-]?\s*resets\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*"
    r"\(([^)]+)\)",
    re.I,
)

_CHI = ZoneInfo("America/Chicago")


def _encode_project_dir(cwd: str) -> str:
    """Claude projects dir encoding: /Users/foo/bar → -Users-foo-bar"""
    p = os.path.abspath(os.path.expanduser(cwd))
    if not p.startswith("/"):
        p = "/" + p
    return p.replace("/", "-")


def project_jsonl_path(cwd: str, session_id: str) -> Optional[str]:
    if not session_id:
        return None
    candidates: List[str] = []
    if cwd:
        enc = _encode_project_dir(cwd)
        candidates.append(os.path.join(CLAUDE_PROJECTS, enc, f"{session_id}.jsonl"))
        # underscore ↔ hyphen (Claude sometimes normalizes path segments)
        enc2 = enc.replace("_", "-")
        enc3 = enc.replace("-", "_")
        for e in (enc2, enc3):
            candidates.append(os.path.join(CLAUDE_PROJECTS, e, f"{session_id}.jsonl"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    # Fall back: search projects tree for this session id (one level of dirs)
    if not os.path.isdir(CLAUDE_PROJECTS):
        return None
    try:
        for name in os.listdir(CLAUDE_PROJECTS):
            p = os.path.join(CLAUDE_PROJECTS, name, f"{session_id}.jsonl")
            if os.path.isfile(p):
                return p
    except OSError:
        return None
    return None


def list_claude_session_registry() -> List[Dict[str, Any]]:
    """Read ~/.claude/sessions/*.json (pid → sessionId, cwd)."""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(CLAUDE_SESSIONS):
        return out
    try:
        names = os.listdir(CLAUDE_SESSIONS)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(CLAUDE_SESSIONS, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        pid = raw.get("pid")
        sid = raw.get("sessionId") or raw.get("session_id")
        cwd = raw.get("cwd") or ""
        if not sid:
            continue
        # alive if process exists
        alive = False
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        started = raw.get("startedAt") or raw.get("started_at")
        started_at_unix: Optional[float] = None
        if isinstance(started, (int, float)):
            started_at_unix = float(started)
            if started_at_unix > 1e12:  # ms
                started_at_unix = started_at_unix / 1000.0
        out.append(
            {
                "pid": pid,
                "session_id": str(sid),
                "cwd": cwd,
                "name": raw.get("name") or "",
                "status": raw.get("status") or "",
                "alive": alive,
                "started_at_unix": started_at_unix,
                "jsonl": project_jsonl_path(cwd, str(sid)),
            }
        )
    return out


def parse_limit_message(
    text: str, *, event_ts: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Parse Claude's 429 synthetic message into kind + resets_at ISO."""
    if not text:
        return None
    m = _LIMIT_MSG_RE.search(text)
    if not m:
        return None
    kind_raw = m.group(1).lower()  # session | weekly
    hour = int(m.group(2))
    minute = int(m.group(3) or "0")
    ampm = m.group(4).lower()
    tzname = (m.group(5) or "America/Chicago").strip()
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = _CHI

    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0

    # Anchor: event timestamp if known, else now
    if event_ts:
        try:
            et = event_ts.replace("Z", "+00:00")
            base = datetime.fromisoformat(et)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone_utc())
            base = base.astimezone(tz)
        except ValueError:
            base = datetime.now(tz)
    else:
        base = datetime.now(tz)

    reset = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # If reset time is at or before the event local time, use next day
    # (weekly hit at 11pm saying "resets 3pm" → next calendar 3pm)
    if reset <= base:
        reset = reset + timedelta(days=1)

    limit_kind = "weekly" if kind_raw == "weekly" else "session"
    return {
        "limit_kind": limit_kind,
        "resets_at": reset.isoformat(),
        "resets_at_unix": int(reset.timestamp()),
        "tz": tzname,
        "raw": text.strip(),
    }


def timezone_utc():
    from datetime import timezone

    return timezone.utc


def parse_rate_limit_info(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Structured rate_limit_info from Claude streams."""
    if not isinstance(info, dict):
        return None
    rtype = (info.get("rateLimitType") or info.get("rate_limit_type") or "").lower()
    status = (info.get("status") or "").lower()
    resets = info.get("resetsAt") or info.get("resets_at")
    util = info.get("utilization")
    if resets is None and not rtype:
        return None
    resets_unix = None
    resets_iso = None
    if isinstance(resets, (int, float)):
        ts = float(resets)
        if ts > 1e12:  # ms
            ts = ts / 1000.0
        resets_unix = int(ts)
        resets_iso = datetime.fromtimestamp(ts, tz=timezone_utc()).astimezone().isoformat()
    limit_kind = "weekly" if "seven" in rtype or "week" in rtype else "session"
    if "five" in rtype or "5" in rtype or "hour" in rtype:
        limit_kind = "session"
    # status: allowed / allowed_warning / rejected / etc.
    hard = status in ("rejected", "rate_limit", "limited") or (
        isinstance(util, (int, float)) and util >= 1.0
    )
    return {
        "limit_kind": limit_kind,
        "rate_limit_type": rtype or None,
        "status": status or None,
        "utilization": util,
        "resets_at": resets_iso,
        "resets_at_unix": resets_unix,
        "hard": hard,
        "raw_info": {k: info.get(k) for k in ("status", "resetsAt", "rateLimitType", "utilization")},
    }


def _text_from_message(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def scan_jsonl_for_limits(path: str, *, max_bytes: int = 400_000) -> List[Dict[str, Any]]:
    """Scan a Claude session jsonl for limit events (prefer latest)."""
    if not path or not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            lines = f.readlines()
    except OSError:
        return []

    found: List[Dict[str, Any]] = []
    for line in lines:
        if "rate_limit" not in line and "rateLimit" not in line and "weekly limit" not in line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue

        # Structured
        rli = o.get("rate_limit_info")
        if isinstance(rli, dict):
            parsed = parse_rate_limit_info(rli)
            if parsed:
                parsed["source"] = "rate_limit_info"
                parsed["event_ts"] = o.get("timestamp")
                found.append(parsed)

        # 429 synthetic assistant
        err = o.get("error")
        api = o.get("apiErrorStatus")
        if err == "rate_limit" or api == 429 or o.get("isApiErrorMessage"):
            text = _text_from_message(o.get("message"))
            if not text and isinstance(o.get("message"), str):
                text = o["message"]
            parsed_msg = parse_limit_message(text, event_ts=o.get("timestamp"))
            if parsed_msg:
                parsed_msg["source"] = "rate_limit_429"
                parsed_msg["event_ts"] = o.get("timestamp")
                parsed_msg["hard"] = True
                found.append(parsed_msg)

    return found


def _event_resets_unix(e: Dict[str, Any]) -> Optional[int]:
    ru = e.get("resets_at_unix")
    if isinstance(ru, (int, float)):
        return int(ru)
    if e.get("resets_at"):
        try:
            dt = datetime.fromisoformat(str(e["resets_at"]).replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def _iso_to_unix(ts: Optional[str]) -> Optional[float]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone_utc())
        return dt.timestamp()
    except ValueError:
        return None


def _pick_best_limit(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Prefer hard weekly > hard session > warning with utilization.

    Annotates ``expired`` when resets_at is in the past. Prefer still-active
    (not expired) hard limits over stale ones when scoring.
    """
    if not events:
        return None
    now = time.time()

    def score(e: Dict[str, Any]) -> Tuple[int, int, int, str]:
        hard = 1 if e.get("hard") or e.get("source") == "rate_limit_429" else 0
        weekly = 1 if e.get("limit_kind") == "weekly" else 0
        # prefer events still limiting (reset in future)
        ru = e.get("resets_at_unix") or 0
        future = 1 if ru and ru > now else 0
        # newest event_ts as tie-break (lexicographic ISO works for Zulu)
        return (hard, future, weekly, e.get("event_ts") or "")

    active: List[Dict[str, Any]] = []
    for e in events:
        e = dict(e)
        ru = _event_resets_unix(e)
        if ru is not None:
            e["resets_at_unix"] = ru
            if ru <= now:
                e["expired"] = True
        active.append(e)

    # Prefer hard events; among those, still-active (future reset) first, then newest
    hard = [e for e in active if e.get("hard") or e.get("source") == "rate_limit_429"]
    pool = hard or active
    pool.sort(key=score, reverse=True)
    return pool[0]


def transcript_resumed_after_limit(
    path: str,
    *,
    after_unix: float,
    max_bytes: int = 400_000,
) -> bool:
    """True if the session continued successfully after a hard limit event.

    Evidence (any one):
      - assistant row after ``after_unix`` without rate_limit / 429
      - structured rate_limit_info with status allowed* after that time
    User-only rows are not enough (they may still be blocked).
    """
    if not path or not os.path.isfile(path) or after_unix <= 0:
        return False
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            lines = f.readlines()
    except OSError:
        return False

    for line in lines:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        ts = _iso_to_unix(o.get("timestamp") if isinstance(o.get("timestamp"), str) else None)
        if ts is None or ts <= after_unix:
            continue

        rli = o.get("rate_limit_info")
        if isinstance(rli, dict):
            st = str(rli.get("status") or "").lower()
            # allowed / allowed_warning after the hard event ⇒ not still blocked
            if st.startswith("allowed"):
                return True

        if o.get("type") != "assistant":
            continue
        if o.get("error") == "rate_limit" or o.get("apiErrorStatus") == 429:
            continue
        if o.get("isApiErrorMessage") and o.get("error"):
            continue
        # Successful (or at least non-429) assistant turn after the limit
        return True
    return False


def live_session_already_open(reg: Dict[str, Any], *, resets_at_unix: Optional[int]) -> bool:
    """True when a live Claude process is already past the limit wall.

    Matches registry truth: status busy/running, or process started after reset.
    """
    if not reg.get("alive"):
        return False
    status = str(reg.get("status") or "").lower()
    if status in ("busy", "running", "active"):
        return True
    # New process started after weekly/session reset → user already resumed
    started = reg.get("started_at_unix")
    if started is None:
        # registry may only have raw ms in source files; optional
        pass
    if isinstance(started, (int, float)) and isinstance(resets_at_unix, int):
        # startedAt is often ms
        su = float(started)
        if su > 1e12:
            su = su / 1000.0
        if su >= float(resets_at_unix):
            return True
    return False


def collect_claude_limit_attention() -> List[Dict[str, Any]]:
    """Build attention items for Claude CLI limits across registry + recent jsonl.

    State machine (live, not historical forever):
      limited → (reset passes) ready_to_resume → (work continues) *cleared*
    Never keep ``ready_to_resume`` after successful post-limit activity or a
    busy live process for that session.
    """
    items: List[Dict[str, Any]] = []
    seen_sessions = set()

    registry = list_claude_session_registry()
    # Prefer alive sessions first
    registry.sort(key=lambda r: (not r.get("alive"), r.get("session_id") or ""))

    # Merge multiple registry rows per session_id (old idle + new busy)
    by_sid: Dict[str, List[Dict[str, Any]]] = {}
    for reg in registry:
        by_sid.setdefault(reg["session_id"], []).append(reg)

    for sid, regs in by_sid.items():
        if sid in seen_sessions:
            continue
        # Prefer a row with jsonl + alive
        reg = sorted(
            regs,
            key=lambda r: (not r.get("alive"), 0 if r.get("jsonl") else 1, r.get("pid") or 0),
        )[0]
        # Merge alive/busy flags across pids for this session
        any_alive = any(r.get("alive") for r in regs)
        any_busy = any(
            str(r.get("status") or "").lower() in ("busy", "running", "active") for r in regs
        )
        started_candidates = []
        for r in regs:
            if r.get("started_at_unix") is not None:
                started_candidates.append(r["started_at_unix"])
        path = reg.get("jsonl")
        if not path or not os.path.isfile(path):
            # try other regs
            for r in regs:
                if r.get("jsonl") and os.path.isfile(r["jsonl"]):
                    path = r["jsonl"]
                    reg = r
                    break
        if not path or not os.path.isfile(path):
            continue
        events = scan_jsonl_for_limits(path)
        best = _pick_best_limit(events)
        if not best:
            continue
        merged = dict(reg)
        merged["alive"] = any_alive
        if any_busy:
            merged["status"] = "busy"
        if started_candidates:
            merged["started_at_unix"] = max(started_candidates)
        item = _to_attention_item(best, merged, jsonl_path=path)
        if item is None:
            # Limit expired and work already continued — clear stale "ready"
            seen_sessions.add(sid)
            continue
        seen_sessions.add(sid)
        items.append(item)

    # Also scan recently modified project jsonl not in registry (orphans)
    if os.path.isdir(CLAUDE_PROJECTS):
        try:
            recent: List[Tuple[float, str, str, str]] = []
            for name in os.listdir(CLAUDE_PROJECTS):
                d = os.path.join(CLAUDE_PROJECTS, name)
                if not os.path.isdir(d):
                    continue
                for f in os.listdir(d):
                    if not f.endswith(".jsonl") or f.startswith("agent-"):
                        continue
                    p = os.path.join(d, f)
                    try:
                        m = os.path.getmtime(p)
                    except OSError:
                        continue
                    if time.time() - m > 3 * 86400:
                        continue
                    sid = f[: -len(".jsonl")]
                    if sid in seen_sessions:
                        continue
                    recent.append((m, p, sid, name))
            recent.sort(reverse=True)
            for _m, path, sid, _enc in recent[:15]:
                if sid in seen_sessions:
                    continue
                events = scan_jsonl_for_limits(path)
                best = _pick_best_limit(events)
                if not best:
                    continue
                # only emit if hard limit or still active
                if not (best.get("hard") or best.get("source") == "rate_limit_429"):
                    if not (
                        isinstance(best.get("utilization"), (int, float))
                        and best["utilization"] >= 0.9
                    ):
                        continue
                item = _to_attention_item(
                    best,
                    {
                        "session_id": sid,
                        "cwd": "",
                        "name": sid[:8],
                        "alive": False,
                        "jsonl": path,
                    },
                    jsonl_path=path,
                )
                if item is None:
                    seen_sessions.add(sid)
                    continue
                seen_sessions.add(sid)
                items.append(item)
                if len(items) >= 8:
                    break
        except OSError:
            pass

    return items


def _to_attention_item(
    best: Dict[str, Any],
    reg: Dict[str, Any],
    *,
    jsonl_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Map a limit event + live registry to attention, or None if already cleared."""
    now = time.time()
    ru = best.get("resets_at_unix")
    if ru is None:
        ru = _event_resets_unix(best)
    expired = bool(best.get("expired")) or (isinstance(ru, int) and ru <= now)
    limit_kind = best.get("limit_kind") or "session"
    path = jsonl_path or reg.get("jsonl")

    # Anchor for "has work continued?": prefer limit event time, else reset wall
    event_unix = _iso_to_unix(best.get("event_ts") if isinstance(best.get("event_ts"), str) else None)
    after_unix = event_unix if event_unix is not None else (float(ru) if isinstance(ru, int) else 0.0)

    if expired:
        # Live truth: already working again → do not show ready_to_resume
        if live_session_already_open(reg, resets_at_unix=ru if isinstance(ru, int) else None):
            return None
        if path and transcript_resumed_after_limit(path, after_unix=after_unix):
            return None
        kind = "ready_to_resume"
    else:
        kind = "limited"

    if limit_kind == "weekly":
        title = "Weekly limit"
        if kind == "ready_to_resume":
            detail = "Weekly limit expired — ready to resume"
        else:
            detail = _countdown_detail(best.get("resets_at"), ru if isinstance(ru, int) else None, "Weekly")
    else:
        title = "Session limit (~5h)"
        if kind == "ready_to_resume":
            detail = "Session limit expired — ready to resume"
        else:
            detail = _countdown_detail(
                best.get("resets_at"), ru if isinstance(ru, int) else None, "Session"
            )

    util = best.get("utilization")
    if util is not None and kind == "limited":
        try:
            detail = f"{detail} · {float(util)*100:.0f}% used"
        except (TypeError, ValueError):
            pass

    cwd = reg.get("cwd") or ""
    name = reg.get("name") or ""
    if name:
        title = f"{title} · {name}"

    return {
        "app": "Claude CLI",
        "session_id": reg.get("session_id"),
        "kind": kind,
        "title": title,
        "detail": detail,
        "confidence": "high",
        "resets_at": best.get("resets_at"),
        "cwd": cwd or None,
        "limit_kind": limit_kind,
        "utilization": util,
        "source": best.get("source"),
    }


def _countdown_detail(resets_at: Optional[str], ru: Optional[int], label: str) -> str:
    if ru is None and resets_at:
        try:
            dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            ru = int(dt.timestamp())
        except ValueError:
            ru = None
    if ru is None:
        return f"{label} limit · reset time unknown"
    left = max(0, ru - time.time())
    if left < 60:
        wait = "under 1 min"
    elif left < 3600:
        wait = f"{int(left // 60)} min"
    else:
        h = int(left // 3600)
        m = int((left % 3600) // 60)
        wait = f"{h}h {m}m" if m else f"{h}h"
    # local wall clock (portable: strip leading zero on hour)
    local = datetime.fromtimestamp(ru).astimezone().strftime("%I:%M%p").lower().lstrip("0")
    return f"{label} limit · resets {local} ({wait})"


def claude_budget_overlay(attention_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Budget fields for Claude CLI from attention limit items."""
    claude = [
        a
        for a in attention_items
        if a.get("app") in ("Claude CLI", "Claude Desktop")
        and a.get("kind") in ("limited", "ready_to_resume")
    ]
    if not claude:
        return {}
    # prefer weekly
    weekly = [a for a in claude if a.get("limit_kind") == "weekly"]
    pick = weekly[0] if weekly else claude[0]
    state = "ready" if pick.get("kind") == "ready_to_resume" else "limited"
    return {
        "limit_state": state,
        "resets_at": pick.get("resets_at"),
        "remaining_label": pick.get("detail"),
        "limit_kind": pick.get("limit_kind"),
    }
