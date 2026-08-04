"""Attention signals: needs_you, limited, ready_to_resume.

Fail-closed: only emit high-confidence hits from local evidence.
Heuristics are labeled confidence=low and filtered from menu-bar chip.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

GROK_SESSIONS = os.path.expanduser("~/.grok/sessions")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")

# Assistant last-turn patterns that usually mean waiting on the human
_ASK_RE = re.compile(
    r"(\?|please (confirm|choose|select|reply|answer)|waiting for (your |you)|"
    r"need(s)? your|shall I|would you like|permission|approve|"
    r"Do you want me to|Let me know)",
    re.I,
)
_LIMIT_RE = re.compile(
    r"(rate.?limit|usage.?limit|session.?limit|hit (the |your )?limit|"
    r"try again (in|after)|resets? (at|in)|limit reached)",
    re.I,
)
_RESET_ISO_RE = re.compile(
    r"(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
)
_RESET_IN_RE = re.compile(r"resets?\s+in\s+(\d+)\s*(m|min|minutes|h|hr|hours)", re.I)


def _read_tail_lines(path: str, max_bytes: int = 120_000) -> List[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard partial
            return f.readlines()
    except OSError:
        return []


def _parse_json_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        o = json.loads(line)
        return o if isinstance(o, dict) else None
    except json.JSONDecodeError:
        return None


def _text_from_obj(o: dict) -> str:
    for k in ("text", "content", "message", "body", "display"):
        v = o.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            t = _text_from_obj(v)
            if t:
                return t
        if isinstance(v, list):
            parts = []
            for it in v:
                if isinstance(it, dict):
                    if it.get("type") == "text" and isinstance(it.get("text"), str):
                        parts.append(it["text"])
                    elif isinstance(it.get("text"), str):
                        parts.append(it["text"])
                elif isinstance(it, str):
                    parts.append(it)
            if parts:
                return "\n".join(parts)
    msg = o.get("message")
    if isinstance(msg, dict):
        return _text_from_obj(msg)
    return ""


def _role_from_obj(o: dict) -> str:
    r = (o.get("role") or o.get("type") or "").lower()
    if r in ("assistant", "ai", "model"):
        return "assistant"
    if r in ("user", "human"):
        return "user"
    if o.get("type") == "assistant":
        return "assistant"
    if o.get("type") == "user":
        return "user"
    msg = o.get("message")
    if isinstance(msg, dict):
        mr = (msg.get("role") or "").lower()
        if mr in ("assistant", "user"):
            return mr
    return ""


def scan_transcript_for_attention(
    path: str,
    *,
    app: str,
    session_id: str,
    title: str = "",
    cwd: str = "",
) -> List[Dict[str, Any]]:
    """Scan a jsonl transcript; return 0–2 attention items."""
    lines = _read_tail_lines(path)
    if not lines:
        return []
    out: List[Dict[str, Any]] = []
    last_assistant = ""
    last_user_after = False
    # walk from end for last meaningful turns
    for line in reversed(lines[-200:]):
        o = _parse_json_line(line)
        if not o:
            # plain text limit messages
            if _LIMIT_RE.search(line):
                item = _limit_item(app, session_id, title, cwd, line)
                if item:
                    out.append(item)
                    break
            continue
        role = _role_from_obj(o)
        text = _text_from_obj(o)
        if not text and not role:
            blob = json.dumps(o)[:2000]
            if _LIMIT_RE.search(blob):
                item = _limit_item(app, session_id, title, cwd, blob)
                if item:
                    out.append(item)
            continue
        if role == "user" and last_assistant:
            last_user_after = True
            break
        if role == "assistant" and not last_assistant:
            last_assistant = text
            if _LIMIT_RE.search(text):
                item = _limit_item(app, session_id, title, cwd, text)
                if item:
                    out.append(item)
        if role == "user" and not last_assistant:
            # last turn is user — not waiting
            break

    if last_assistant and not last_user_after and _ASK_RE.search(last_assistant):
        out.append(
            {
                "app": app,
                "session_id": session_id,
                "kind": "needs_you",
                "title": title or "Session",
                "detail": "Waiting for your reply",
                "confidence": "high",
                "resets_at": None,
                "cwd": cwd or None,
            }
        )
    return out


def _limit_item(
    app: str, session_id: str, title: str, cwd: str, text: str
) -> Optional[Dict[str, Any]]:
    resets = None
    m = _RESET_ISO_RE.search(text)
    if m:
        resets = m.group(1)
    else:
        m2 = _RESET_IN_RE.search(text)
        if m2:
            n = int(m2.group(1))
            unit = m2.group(2).lower()
            sec = n * 3600 if unit.startswith("h") else n * 60
            resets = datetime.fromtimestamp(
                time.time() + sec, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
    kind = "limited"
    if resets:
        try:
            # if already past, ready_to_resume
            ts = resets.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= dt:
                kind = "ready_to_resume"
        except ValueError:
            pass
    return {
        "app": app,
        "session_id": session_id,
        "kind": kind,
        "title": title or "Session",
        "detail": "Session limit" if kind != "ready_to_resume" else "Limit expired — ready to resume",
        "confidence": "high",
        "resets_at": resets,
        "cwd": cwd or None,
    }


def collect_attention_from_sessions(
    sessions: Sequence[Any],
) -> List[Dict[str, Any]]:
    """Best-effort scan of live session rows + Claude limit registry."""
    items: List[Dict[str, Any]] = []
    seen = set()

    # Claude session/weekly limits from ~/.claude (authoritative for stuck CLI)
    try:
        from local_ai_monitor.claude_limits import collect_claude_limit_attention

        for it in collect_claude_limit_attention():
            k2 = (it.get("app"), it.get("session_id"), it.get("kind"), it.get("limit_kind"))
            if k2 in seen:
                continue
            seen.add(k2)
            items.append(it)
    except Exception:
        pass

    for s in sessions:
        app = getattr(s, "app", None) or (s.get("app") if isinstance(s, dict) else None)
        sid = getattr(s, "session_id", None) or (
            s.get("session_id") if isinstance(s, dict) else None
        )
        detail = getattr(s, "detail", None) or (
            s.get("detail") if isinstance(s, dict) else None
        )
        label = getattr(s, "label", None) or (
            s.get("label") if isinstance(s, dict) else None
        )
        if not app or not sid:
            continue
        key = f"{app}:{sid}"
        if key in seen:
            continue
        path = None
        cwd = ""
        if isinstance(detail, str) and detail.startswith("/"):
            cwd = detail
        if app == "Grok" and isinstance(sid, str) and len(sid) > 20 and not sid.startswith("pid:"):
            # find session dir by uuid
            path = _find_grok_updates(sid)
        elif app == "Claude CLI" and isinstance(detail, str) and detail.startswith("/"):
            path = _find_claude_jsonl(detail)
        elif app == "Codex":
            # Fail-closed: only scan real rollout jsonl (local evidence)
            path = _find_codex_jsonl(str(sid or ""), cwd if cwd else str(detail or ""))
        # Cursor: no reliable local agent transcript on this host — omit
        if path:
            found = scan_transcript_for_attention(
                path,
                app=app,
                session_id=str(sid),
                title=(label or "")[:48],
                cwd=cwd,
            )
            for it in found:
                k2 = (it["app"], it["session_id"], it["kind"])
                if k2 not in seen:
                    seen.add(k2)
                    items.append(it)
            seen.add(key)
    # high confidence first; limited/ready before needs_you noise
    def _rank(x: Dict[str, Any]) -> tuple:
        conf = 0 if x.get("confidence") == "high" else 1
        kind_order = {"limited": 0, "ready_to_resume": 1, "needs_you": 2}.get(
            x.get("kind") or "", 9
        )
        weekly = 0 if x.get("limit_kind") == "weekly" else 1
        return (conf, kind_order, weekly)

    items.sort(key=_rank)
    return items[:12]


def _find_grok_updates(uuid: str) -> Optional[str]:
    if not os.path.isdir(GROK_SESSIONS):
        return None
    for root, dirs, files in os.walk(GROK_SESSIONS):
        if uuid in root and "updates.jsonl" in files:
            return os.path.join(root, "updates.jsonl")
        # prune deep
        if root.count(os.sep) - GROK_SESSIONS.count(os.sep) > 4:
            dirs.clear()
    return None


def _find_claude_jsonl(project_path: str) -> Optional[str]:
    # Claude projects use path-encoded dirs
    if not os.path.isdir(CLAUDE_PROJECTS):
        return None
    enc = project_path.replace("/", "-")
    if enc.startswith("-"):
        pass
    else:
        enc = "-" + enc.replace("/", "-")
    # try common encodings
    candidates = [
        os.path.join(CLAUDE_PROJECTS, project_path.replace("/", "-")),
        os.path.join(CLAUDE_PROJECTS, "-" + project_path.lstrip("/").replace("/", "-")),
    ]
    for d in candidates:
        if os.path.isdir(d):
            jsonls = [
                os.path.join(d, f)
                for f in os.listdir(d)
                if f.endswith(".jsonl")
            ]
            if jsonls:
                jsonls.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                return jsonls[0]
    # slow scan by mtime among all — limited
    best = None
    best_m = 0.0
    try:
        for name in os.listdir(CLAUDE_PROJECTS):
            d = os.path.join(CLAUDE_PROJECTS, name)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not f.endswith(".jsonl"):
                    continue
                p = os.path.join(d, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m > best_m:
                    best_m = m
                    best = p
    except OSError:
        return None
    # only if recent (2h)
    if best and time.time() - best_m < 7200:
        return best
    return None


def _find_codex_jsonl(session_id: str, cwd: str) -> Optional[str]:
    """Codex rollout path by UUID or cwd (fail-closed)."""
    try:
        from local_ai_monitor.codex_local import codex_session_for_cwd, codex_transcript_path

        if session_id and not session_id.startswith("pid:"):
            p = codex_transcript_path(session_id)
            if p:
                return p
        if cwd and cwd.startswith("/"):
            hit = codex_session_for_cwd(cwd)
            if hit and hit.get("path"):
                return str(hit["path"])
    except Exception:
        return None
    return None
