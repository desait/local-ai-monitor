"""Codex local session discovery — labels + resume UUID (fail-closed).

Sources (observed on disk):
- ~/.codex/sessions/**/rollout-*.jsonl  (session_meta payload with cwd + session_id)
- ~/.codex/history.jsonl                 (session_id + text tails; no cwd)

Never invent remaining quota % from history text.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

CODEX_HOME = os.path.expanduser("~/.codex")
CODEX_SESSIONS = os.path.join(CODEX_HOME, "sessions")
CODEX_HISTORY = os.path.join(CODEX_HOME, "history.jsonl")


def _read_session_meta(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            # session_meta is almost always line 1
            for i, line in enumerate(f):
                if i > 5:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(o, dict):
                    continue
                if o.get("type") == "session_meta" and isinstance(o.get("payload"), dict):
                    return o["payload"]
                # some builds put meta fields at top level
                if o.get("session_id") and o.get("cwd"):
                    return o
    except OSError:
        return None
    return None


def _iter_rollout_files(max_files: int = 80) -> List[str]:
    if not os.path.isdir(CODEX_SESSIONS):
        return []
    found: List[tuple] = []
    try:
        for root, _dirs, files in os.walk(CODEX_SESSIONS):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                if "rollout-" not in name and not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                try:
                    m = os.path.getmtime(path)
                except OSError:
                    continue
                found.append((m, path))
            # depth guard
            if root.count(os.sep) - CODEX_SESSIONS.count(os.sep) > 5:
                _dirs.clear()
    except OSError:
        return []
    found.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in found[:max_files]]


def codex_session_for_cwd(cwd: str) -> Optional[Dict[str, Any]]:
    """Most recent Codex rollout whose session_meta.cwd matches.

    Returns {session_id, cwd, path, title?, mtime} or None.
    """
    if not cwd or not cwd.startswith("/"):
        return None
    norm = os.path.realpath(cwd) if os.path.isdir(cwd) else cwd.rstrip("/")
    best: Optional[Dict[str, Any]] = None
    best_m = 0.0
    for path in _iter_rollout_files():
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        meta = _read_session_meta(path)
        if not meta:
            continue
        mcwd = str(meta.get("cwd") or "").rstrip("/")
        if not mcwd:
            continue
        try:
            mnorm = os.path.realpath(mcwd) if os.path.isdir(mcwd) else mcwd
        except OSError:
            mnorm = mcwd
        if mnorm != norm and mcwd != cwd.rstrip("/"):
            continue
        sid = str(meta.get("session_id") or meta.get("id") or "")
        if not sid:
            continue
        if m >= best_m:
            best_m = m
            title = None
            # optional display title from meta
            for k in ("title", "display_title", "session_name", "name"):
                v = meta.get(k)
                if isinstance(v, str) and v.strip() and len(v.strip()) < 80:
                    title = v.strip()
                    break
            best = {
                "session_id": sid,
                "cwd": mcwd,
                "path": path,
                "title": title,
                "mtime": m,
            }
    return best


def latest_codex_session() -> Optional[Dict[str, Any]]:
    """Most recent rollout overall (for resume --last fallback metadata)."""
    files = _iter_rollout_files(max_files=1)
    if not files:
        return None
    path = files[0]
    meta = _read_session_meta(path) or {}
    sid = str(meta.get("session_id") or meta.get("id") or "")
    if not sid:
        # parse from filename rollout-...-UUID.jsonl
        base = os.path.basename(path)
        parts = base.replace(".jsonl", "").split("-")
        # last 5 segments may form uuid if 8-4-4-4-12
        if len(parts) >= 5:
            maybe = "-".join(parts[-5:])
            if len(maybe) >= 32:
                sid = maybe
    if not sid:
        return None
    try:
        m = os.path.getmtime(path)
    except OSError:
        m = 0.0
    return {
        "session_id": sid,
        "cwd": str(meta.get("cwd") or ""),
        "path": path,
        "title": meta.get("title") if isinstance(meta.get("title"), str) else None,
        "mtime": m,
    }


def codex_transcript_path(session_id: str) -> Optional[str]:
    if not session_id or session_id.startswith("pid:"):
        return None
    for path in _iter_rollout_files(max_files=120):
        if session_id in os.path.basename(path):
            return path
        meta = _read_session_meta(path)
        if meta and str(meta.get("session_id") or meta.get("id") or "") == session_id:
            return path
    return None
