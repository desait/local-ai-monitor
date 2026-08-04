"""Hybrid store: live.json + per-bucket accum.json v2 + SQLite history.

Design  / rollup algorithm — boundary-safe, idempotent, crash-durable.
Python 3.9+; stdlib only. Never log cmd/env.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from local_ai_monitor.config import ensure_state_dir, state_dir
from local_ai_monitor.sessionize import SessionStats

# --- time helpers (local wall clock) ---

BUCKET_SECONDS = 1800  # 30 minutes


def floor_bucket_start(ts: float) -> float:
    """Floor epoch seconds to 30-minute local wall-clock boundary."""
    # Use local broken-down time so DST edges follow wall clock, not pure UTC.
    lt = time.localtime(ts)
    # minutes floored to 0 or 30
    minute = 0 if lt.tm_min < 30 else 30
    floored = time.struct_time(
        (
            lt.tm_year,
            lt.tm_mon,
            lt.tm_mday,
            lt.tm_hour,
            minute,
            0,
            lt.tm_wday,
            lt.tm_yday,
            lt.tm_isdst,
        )
    )
    return float(time.mktime(floored))


def format_local_iso(ts: float) -> str:
    """Local ISO without timezone suffix (design convention)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def format_local_iso_tz(ts: float) -> str:
    """ISO with offset when available (for live.json ts)."""
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except Exception:
        return format_local_iso(ts)


def parse_local_iso(s: str) -> float:
    """Parse design ISO (with or without offset) to epoch seconds."""
    s = (s or "").strip()
    if not s:
        return 0.0
    # Try fromisoformat (3.9 supports offset forms with colon)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return time.mktime(dt.timetuple()) + dt.microsecond / 1e6
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(s[:19], fmt))
        except ValueError:
            continue
    return 0.0


def split_interval(prev_ts: float, now_ts: float) -> List[Tuple[float, str]]:
    """Split [prev_ts, now_ts] into (sub_dt, bucket_start_iso) slices.

    Each slice is wholly within one 30m bucket. Empty if now <= prev.
    """
    if now_ts <= prev_ts:
        return []
    out: List[Tuple[float, str]] = []
    t = prev_ts
    while t < now_ts:
        b_start = floor_bucket_start(t)
        b_end = b_start + BUCKET_SECONDS
        # if t is exactly on a boundary that came from prev, still use that bucket
        # until we cross b_end
        slice_end = min(now_ts, b_end)
        # Guard: if floating point lands exactly on b_end as start of next
        if slice_end <= t:
            # advance to next bucket
            t = b_end
            continue
        sub_dt = slice_end - t
        out.append((sub_dt, format_local_iso(b_start)))
        t = slice_end
    return out


def clamp_dt(
    prev_ts: float,
    now_ts: float,
    sample_interval: float,
    max_factor: float = 5.0,
) -> float:
    dt = now_ts - prev_ts
    if dt <= 0 or dt > max_factor * sample_interval:
        return float(sample_interval)
    return dt


# --- atomic JSON ---


def atomic_write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    data = json.dumps(obj, indent=2, sort_keys=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def read_json(path: str) -> Optional[Any]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# --- paths ---


def live_path(state: Optional[str] = None) -> str:
    """Resolve the live feed path.

    Lean mode (default, ``LOCAL_AI_MONITOR_LEAN=1``): prefer ``live.min.json`` from the
    C plane (``local-ai-monitord``). Full ``live.json`` from Python ``local-ai-monitor collect`` is
    historical / on-demand only — kept in tree but not always-on.

    Override with ``LOCAL_AI_MONITOR_LIVE=/abs/path``.
    """
    env = (os.environ.get("LOCAL_AI_MONITOR_LIVE") or "").strip()
    if env:
        return os.path.expanduser(env)
    base = state_dir(state)
    full = os.path.join(base, "live.json")
    mini = os.path.join(base, "live.min.json")
    lean_raw = (os.environ.get("LOCAL_AI_MONITOR_LEAN") or "1").strip().lower()
    lean = lean_raw not in ("0", "false", "no", "off")
    if lean:
        # Prefer min when present (even if older full exists — full is not refreshed).
        if os.path.isfile(mini):
            return mini
        return full
    if os.path.isfile(full):
        return full
    return mini if os.path.isfile(mini) else full


def accum_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "accum.json")


def history_db_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "history.db")


def collect_lock_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "collect.lock")


# --- live.json ---


def session_stats_to_live_row(s: SessionStats) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "app": s.app,
        "session_id": s.session_id,
        "label": s.label,
        "detail": s.detail or "",
        "cpu_pct": round(s.pcpu, 2),
        "rss_kb": int(s.rss_kb),
        "nproc": s.nproc,
        "alive": True,
        # Cap pids for kill path (menu bar / end-session). Sorted for stability.
        "pids": sorted(int(p) for p in s.pids)[:64],
    }
    if s.threads is None:
        row["nthreads"] = None
    else:
        row["nthreads"] = int(s.threads)
    # Token usage (local stores)
    row["tokens_in"] = s.tokens_in
    row["tokens_out"] = s.tokens_out
    row["tokens_total"] = s.tokens_total
    row["tokens_cached"] = s.tokens_cached
    row["context_used"] = s.context_used
    row["context_window"] = s.context_window
    row["tokens_source"] = s.tokens_source
    try:
        from local_ai_monitor.surface import enrich_live_row

        row = enrich_live_row(row)
    except Exception:
        row.setdefault("openable", False)
        row.setdefault("kind", "unknown")
    try:
        from local_ai_monitor.activity import classify_session

        cls = classify_session(row)
        row["activity_state"] = cls.get("activity")
        row["activity_reason"] = cls.get("reason")
        row["auto_reclaim"] = bool(cls.get("auto_reclaim"))
    except Exception:
        row.setdefault("activity_state", "unknown")
        row.setdefault("auto_reclaim", False)
    return row


def build_live_payload(
    sessions: Sequence[SessionStats],
    *,
    collector_pid: int,
    sample_interval_s: float,
    ts: Optional[float] = None,
    debug: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = ts if ts is not None else time.time()
    rows = [session_stats_to_live_row(s) for s in sessions if s.nproc > 0]
    total_cpu = sum(r["cpu_pct"] for r in rows)
    total_rss = sum(r["rss_kb"] for r in rows)
    total_nproc = sum(r["nproc"] for r in rows)
    top: Optional[Dict[str, Any]] = None
    if rows:
        best = max(rows, key=lambda r: (r["cpu_pct"], r["rss_kb"]))
        top = {
            "app": best["app"],
            "session_id": best["session_id"],
            "label": best["label"],
            "cpu_pct": best["cpu_pct"],
            "rss_kb": best["rss_kb"],
        }
    # Catalog: installed tools + user visibility (for menu bar / simple)
    tools_meta: Dict[str, Any] = {}
    attention: List[Dict[str, Any]] = []
    budgets: Dict[str, Any] = {}
    ccm_meta: Dict[str, Any] = {}
    try:
        from local_ai_monitor.catalog import catalog_snapshot, load_tool_prefs, visible_tool_ids

        running = sorted({r["app"] for r in rows if r.get("app")})
        snap = catalog_snapshot()
        prefs = load_tool_prefs()
        tools_meta = {
            "installed": snap.get("installed") or [],
            "hidden": snap.get("hidden") or [],
            "known": snap.get("known") or [],
            "show_idle_installed": prefs.show_idle_installed,
            "visible": visible_tool_ids(running=running, prefs=prefs),
            "running": running,
        }
    except Exception:
        tools_meta = {}

    # Phase 2: attention + budgets (fail-closed)
    try:
        from local_ai_monitor.attention import collect_attention_from_sessions
        from local_ai_monitor.budget import build_budgets

        attention = collect_attention_from_sessions(sessions)
        apps = sorted(
            set(tools_meta.get("visible") or [])
            | set(tools_meta.get("running") or [])
            | {r["app"] for r in rows if r.get("app")}
        )
        budgets = build_budgets(sessions, attention, apps)
    except Exception:
        attention = []
        budgets = {}

    try:
        from local_ai_monitor.ccm_observer import ccm_status_meta

        ccm_meta = ccm_status_meta()
    except Exception:
        ccm_meta = {}

    # Compact 12h CPU series for menu-bar sparklines (fail-closed)
    sparks: Dict[str, List[float]] = {}
    try:
        hist = HistoryStore()
        sparks = hist.series_by_app(hours=12.0)
        hist.close()
    except Exception:
        sparks = {}

    # Physics-first resource policy for menu bar (fail-closed; never auto-kill)
    resource: Dict[str, Any] = {}
    try:
        from local_ai_monitor.resource.human import resource_live_block

        resource = resource_live_block()
    except Exception:
        resource = {"band": "unknown", "show": False}

    quarantine: Dict[str, Any] = {}
    try:
        from local_ai_monitor.quarantine import ensure_default_quarantine, quarantine_meta

        ensure_default_quarantine()
        quarantine = quarantine_meta()
    except Exception:
        quarantine = {"tools": []}

    # Re-classify with attention so needs_you blocks auto_reclaim
    try:
        from local_ai_monitor.activity import enrich_sessions_activity

        rows = enrich_sessions_activity(rows, attention)
    except Exception:
        pass

    return {
        "version": 2,
        "ts": format_local_iso_tz(now),
        "collector_pid": collector_pid,
        "sample_interval_s": sample_interval_s,
        "debug": debug or {},
        "totals": {
            "cpu_pct": round(total_cpu, 2),
            "rss_kb": int(total_rss),
            "nproc": int(total_nproc),
            "nsessions": len(rows),
        },
        "top": top,
        "sessions": rows,
        "tools": tools_meta,
        "attention": attention,
        "budgets": budgets,
        "ccm": ccm_meta,
        "sparks": sparks,
        "resource": resource,
        "quarantine": quarantine,
    }


class LiveStore:
    def __init__(self, state: Optional[str] = None) -> None:
        self.state = state
        self.path = live_path(state)

    def write(
        self,
        sessions: Sequence[SessionStats],
        *,
        collector_pid: int,
        sample_interval_s: float,
        ts: Optional[float] = None,
        debug: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ensure_state_dir(self.state)
        payload = build_live_payload(
            sessions,
            collector_pid=collector_pid,
            sample_interval_s=sample_interval_s,
            ts=ts,
            debug=debug,
        )
        atomic_write_json(self.path, payload)
        return payload

    def read(self) -> Optional[Dict[str, Any]]:
        data = read_json(self.path)
        if isinstance(data, dict):
            return data
        return None


# --- accum.json v2 (per-bucket) ---


@dataclass
class BucketDelta:
    cpu_seconds_delta: float = 0.0
    rss_dt_sum: float = 0.0
    dt_sum: float = 0.0
    sample_count_delta: int = 0
    nproc_max: int = 0
    peak_rss_kb: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cpu_seconds_delta": self.cpu_seconds_delta,
            "rss_dt_sum": self.rss_dt_sum,
            "dt_sum": self.dt_sum,
            "sample_count_delta": self.sample_count_delta,
            "nproc_max": self.nproc_max,
            "peak_rss_kb": self.peak_rss_kb,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BucketDelta":
        return cls(
            cpu_seconds_delta=float(d.get("cpu_seconds_delta", 0) or 0),
            rss_dt_sum=float(d.get("rss_dt_sum", 0) or 0),
            dt_sum=float(d.get("dt_sum", 0) or 0),
            sample_count_delta=int(d.get("sample_count_delta", 0) or 0),
            nproc_max=int(d.get("nproc_max", 0) or 0),
            peak_rss_kb=int(d.get("peak_rss_kb", 0) or 0),
        )


@dataclass
class AccumSession:
    label: str = ""
    detail: str = ""
    first_seen: str = ""
    last_seen: str = ""
    peak_rss_kb: int = 0
    buckets: Dict[str, BucketDelta] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "detail": self.detail,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "peak_rss_kb": self.peak_rss_kb,
            "buckets": {k: v.to_dict() for k, v in sorted(self.buckets.items())},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AccumSession":
        buckets_raw = d.get("buckets") or {}
        buckets: Dict[str, BucketDelta] = {}
        if isinstance(buckets_raw, dict):
            for bk, bv in buckets_raw.items():
                if isinstance(bv, dict):
                    buckets[str(bk)] = BucketDelta.from_dict(bv)
        return cls(
            label=str(d.get("label") or ""),
            detail=str(d.get("detail") or ""),
            first_seen=str(d.get("first_seen") or ""),
            last_seen=str(d.get("last_seen") or ""),
            peak_rss_kb=int(d.get("peak_rss_kb", 0) or 0),
            buckets=buckets,
        )


@dataclass
class AccumState:
    version: int = 2
    last_rollup_ts: str = ""
    last_sample_ts: str = ""
    sessions: Dict[str, AccumSession] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "last_rollup_ts": self.last_rollup_ts,
            "last_sample_ts": self.last_sample_ts,
            "sessions": {k: v.to_dict() for k, v in sorted(self.sessions.items())},
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AccumState":
        version = int(d.get("version") or 1)
        sessions: Dict[str, AccumSession] = {}
        raw_sess = d.get("sessions") or {}
        if not isinstance(raw_sess, dict):
            raw_sess = {}

        if version < 2:
            # Best-effort migrate flat v1 → single bucket at last_sample_ts
            last = str(d.get("last_sample_ts") or "")
            bkey = last if last else format_local_iso(time.time())
            # floor to bucket
            ts = parse_local_iso(bkey) or time.time()
            bkey = format_local_iso(floor_bucket_start(ts))
            for sk, sv in raw_sess.items():
                if not isinstance(sv, dict):
                    continue
                # flat fields may be at session level
                delta = BucketDelta(
                    cpu_seconds_delta=float(
                        sv.get("cpu_seconds_delta", sv.get("cpu_seconds", 0)) or 0
                    ),
                    rss_dt_sum=float(sv.get("rss_dt_sum", 0) or 0),
                    dt_sum=float(sv.get("dt_sum", 0) or 0),
                    sample_count_delta=int(
                        sv.get("sample_count_delta", sv.get("sample_count", 0)) or 0
                    ),
                    nproc_max=int(sv.get("nproc_max", 0) or 0),
                    peak_rss_kb=int(sv.get("peak_rss_kb", 0) or 0),
                )
                sessions[str(sk)] = AccumSession(
                    label=str(sv.get("label") or ""),
                    detail=str(sv.get("detail") or ""),
                    first_seen=str(sv.get("first_seen") or ""),
                    last_seen=str(sv.get("last_seen") or last),
                    peak_rss_kb=int(sv.get("peak_rss_kb", 0) or 0),
                    buckets={bkey: delta} if (
                        delta.cpu_seconds_delta
                        or delta.dt_sum
                        or delta.sample_count_delta
                    ) else {},
                )
            return cls(
                version=2,
                last_rollup_ts=str(d.get("last_rollup_ts") or ""),
                last_sample_ts=last,
                sessions=sessions,
            )

        for sk, sv in raw_sess.items():
            if isinstance(sv, dict):
                sessions[str(sk)] = AccumSession.from_dict(sv)
        return cls(
            version=2,
            last_rollup_ts=str(d.get("last_rollup_ts") or ""),
            last_sample_ts=str(d.get("last_sample_ts") or ""),
            sessions=sessions,
        )


def store_key(app: str, session_id: str) -> str:
    return f"{app}|{session_id}"


class AccumStore:
    """Per-bucket unflushed deltas (design accum.json v2)."""

    def __init__(self, state: Optional[str] = None) -> None:
        self.state = state
        self.path = accum_path(state)
        self.data = AccumState()

    def load(self) -> AccumState:
        raw = read_json(self.path)
        if isinstance(raw, dict):
            self.data = AccumState.from_dict(raw)
        else:
            self.data = AccumState()
        return self.data

    def save(self) -> None:
        ensure_state_dir(self.state)
        atomic_write_json(self.path, self.data.to_dict())

    def on_sample(
        self,
        prev_ts: float,
        now_ts: float,
        sessions: Sequence[SessionStats],
        *,
        sample_interval: float = 10.0,
        write: bool = True,
    ) -> AccumState:
        """Apply one sample interval into per-bucket deltas.

        Uses design split_interval. sample_count increments only on the
        bucket that contains now_ts.
        """
        # Clamp wall dt for skew, but still split the *actual* interval used
        dt = clamp_dt(prev_ts, now_ts, sample_interval)
        if now_ts <= prev_ts:
            # synthetic advance for zero/negative
            now_ts = prev_ts + dt
        elif abs((now_ts - prev_ts) - dt) > 1e-9:
            # clock skew clamp: attribute using clamped dt ending at now_ts
            prev_ts = now_ts - dt

        slices = split_interval(prev_ts, now_ts)
        if not slices:
            slices = [(dt, format_local_iso(floor_bucket_start(now_ts)))]

        now_bucket = format_local_iso(floor_bucket_start(now_ts))
        now_iso = format_local_iso(now_ts)

        for s in sessions:
            if s.nproc <= 0 and s.pcpu <= 0 and s.rss_kb <= 0:
                continue
            sk = store_key(s.app, s.session_id)
            meta = self.data.sessions.get(sk)
            if meta is None:
                meta = AccumSession(
                    label=s.label,
                    detail=s.detail or "",
                    first_seen=now_iso,
                    last_seen=now_iso,
                    peak_rss_kb=int(s.rss_kb),
                )
                self.data.sessions[sk] = meta
            else:
                if s.label:
                    meta.label = s.label
                if s.detail:
                    meta.detail = s.detail
                if not meta.first_seen:
                    meta.first_seen = now_iso
                meta.last_seen = now_iso
                meta.peak_rss_kb = max(meta.peak_rss_kb, int(s.rss_kb))

            for sub_dt, bucket in slices:
                b = meta.buckets.get(bucket)
                if b is None:
                    b = BucketDelta()
                    meta.buckets[bucket] = b
                b.cpu_seconds_delta += (s.pcpu / 100.0) * sub_dt
                b.rss_dt_sum += float(s.rss_kb) * sub_dt
                b.dt_sum += sub_dt
                b.nproc_max = max(b.nproc_max, s.nproc)
                b.peak_rss_kb = max(b.peak_rss_kb, int(s.rss_kb))
                if bucket == now_bucket:
                    b.sample_count_delta += 1

        self.data.last_sample_ts = now_iso
        self.data.version = 2
        if write:
            self.save()
        return self.data

    def clear_buckets_after_flush(self, write: bool = True) -> None:
        """Clear all bucket deltas after successful SQLite commit."""
        empty_keys = []
        for sk, meta in self.data.sessions.items():
            meta.buckets.clear()
            # keep session shell for continuity? design: empty buckets or drop
            # Drop sessions with no residual buckets to keep file small
            empty_keys.append(sk)
        for sk in empty_keys:
            del self.data.sessions[sk]
        if write:
            self.save()


# --- SQLite history ---

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_dim (
  app         TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  label       TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT '',
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  PRIMARY KEY (app, session_id)
);

CREATE TABLE IF NOT EXISTS rollup_30m (
  bucket_start  TEXT NOT NULL,
  app           TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  cpu_seconds   REAL NOT NULL DEFAULT 0,
  peak_rss_kb   INTEGER NOT NULL DEFAULT 0,
  rss_dt_sum    REAL NOT NULL DEFAULT 0,
  dt_sum        REAL NOT NULL DEFAULT 0,
  avg_rss_kb    REAL NOT NULL DEFAULT 0,
  sample_count  INTEGER NOT NULL DEFAULT 0,
  nproc_max     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bucket_start, app, session_id)
);

CREATE INDEX IF NOT EXISTS idx_rollup_app_time
  ON rollup_30m(app, bucket_start);
"""


class HistoryStore:
    def __init__(self, state: Optional[str] = None, db_path: Optional[str] = None) -> None:
        self.state = state
        self.db_path = db_path or history_db_path(state)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        ensure_state_dir(self.state)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(DDL)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def rollup(self, accum: AccumState) -> int:
        """Flush unflushed bucket deltas into rollup_30m. Returns rows touched.

        Idempotent when accum.buckets already empty (no double-count).
        Caller must clear accum buckets only AFTER this returns successfully.
        """
        conn = self.connect()
        n = 0
        with conn:  # transaction
            for sk, meta in accum.sessions.items():
                if "|" not in sk:
                    continue
                app, session_id = sk.split("|", 1)
                conn.execute(
                    """
                    INSERT INTO session_dim (app, session_id, label, detail, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(app, session_id) DO UPDATE SET
                      label = excluded.label,
                      detail = excluded.detail,
                      first_seen = CASE
                        WHEN session_dim.first_seen = '' OR session_dim.first_seen > excluded.first_seen
                        THEN excluded.first_seen ELSE session_dim.first_seen END,
                      last_seen = CASE
                        WHEN session_dim.last_seen < excluded.last_seen
                        THEN excluded.last_seen ELSE session_dim.last_seen END
                    """,
                    (
                        app,
                        session_id,
                        meta.label,
                        meta.detail,
                        meta.first_seen,
                        meta.last_seen,
                    ),
                )
                for bucket_start, b in meta.buckets.items():
                    if (
                        b.cpu_seconds_delta == 0
                        and b.dt_sum == 0
                        and b.sample_count_delta == 0
                        and b.rss_dt_sum == 0
                    ):
                        continue
                    # UPSERT add deltas
                    conn.execute(
                        """
                        INSERT INTO rollup_30m (
                          bucket_start, app, session_id,
                          cpu_seconds, peak_rss_kb, rss_dt_sum, dt_sum,
                          avg_rss_kb, sample_count, nproc_max
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(bucket_start, app, session_id) DO UPDATE SET
                          cpu_seconds = rollup_30m.cpu_seconds + excluded.cpu_seconds,
                          peak_rss_kb = MAX(rollup_30m.peak_rss_kb, excluded.peak_rss_kb),
                          rss_dt_sum = rollup_30m.rss_dt_sum + excluded.rss_dt_sum,
                          dt_sum = rollup_30m.dt_sum + excluded.dt_sum,
                          avg_rss_kb = CASE
                            WHEN (rollup_30m.dt_sum + excluded.dt_sum) > 0
                            THEN (rollup_30m.rss_dt_sum + excluded.rss_dt_sum)
                                 / (rollup_30m.dt_sum + excluded.dt_sum)
                            ELSE 0 END,
                          sample_count = rollup_30m.sample_count + excluded.sample_count,
                          nproc_max = MAX(rollup_30m.nproc_max, excluded.nproc_max)
                        """,
                        (
                            bucket_start,
                            app,
                            session_id,
                            float(b.cpu_seconds_delta),
                            int(b.peak_rss_kb),
                            float(b.rss_dt_sum),
                            float(b.dt_sum),
                            (float(b.rss_dt_sum) / b.dt_sum) if b.dt_sum > 0 else 0.0,
                            int(b.sample_count_delta),
                            int(b.nproc_max),
                        ),
                    )
                    n += 1
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ('last_rollup_ts', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (format_local_iso(time.time()),),
            )
        return n

    def prune(self, retention_days: int = 14) -> int:
        """Delete rollup rows older than retention_days. Returns deleted count."""
        conn = self.connect()
        cutoff_ts = time.time() - retention_days * 86400
        cutoff = format_local_iso(cutoff_ts)
        with conn:
            cur = conn.execute(
                "DELETE FROM rollup_30m WHERE bucket_start < ?",
                (cutoff,),
            )
            return cur.rowcount if cur.rowcount is not None else 0

    def query(
        self,
        *,
        hours: float = 24.0,
        app: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self.connect()
        since = format_local_iso(time.time() - hours * 3600)
        sql = """
          SELECT r.bucket_start, r.app, r.session_id, r.cpu_seconds, r.peak_rss_kb,
                 r.rss_dt_sum, r.dt_sum, r.avg_rss_kb, r.sample_count, r.nproc_max,
                 COALESCE(d.label, '') AS label,
                 COALESCE(d.detail, '') AS detail
          FROM rollup_30m r
          LEFT JOIN session_dim d
            ON d.app = r.app AND d.session_id = r.session_id
          WHERE r.bucket_start >= ?
        """
        params: List[Any] = [since]
        if app:
            sql += " AND r.app = ?"
            params.append(app)
        sql += " ORDER BY r.bucket_start DESC, r.cpu_seconds DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def summarize_by_app(
        self,
        *,
        hours: float = 24.0,
    ) -> List[Dict[str, Any]]:
        """24h (or range) CPU-seconds and peak RSS per app."""
        conn = self.connect()
        since = format_local_iso(time.time() - hours * 3600)
        rows = conn.execute(
            """
            SELECT app,
                   SUM(cpu_seconds) AS cpu_seconds,
                   MAX(peak_rss_kb) AS peak_rss_kb,
                   SUM(sample_count) AS sample_count,
                   COUNT(*) AS buckets
            FROM rollup_30m
            WHERE bucket_start >= ?
            GROUP BY app
            ORDER BY cpu_seconds DESC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def series_by_app(
        self,
        *,
        hours: float = 12.0,
    ) -> Dict[str, List[float]]:
        """Fixed-length per-app CPU-seconds series over the last ``hours``.

        One point per 30-minute rollup bucket (oldest → newest). Missing
        buckets are 0.0. Empty dict when history has no rows in range.
        """
        n = max(1, int(round(hours * 3600 / BUCKET_SECONDS)))
        now_b = floor_bucket_start(time.time())
        bucket_keys = [
            format_local_iso(now_b - (n - 1 - i) * BUCKET_SECONDS) for i in range(n)
        ]
        since = bucket_keys[0]
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT bucket_start, app, SUM(cpu_seconds) AS cpu_seconds
            FROM rollup_30m
            WHERE bucket_start >= ?
            GROUP BY bucket_start, app
            """,
            (since,),
        ).fetchall()
        by_app: Dict[str, Dict[str, float]] = {}
        for r in rows:
            app = str(r["app"] or "")
            if not app:
                continue
            by_app.setdefault(app, {})[str(r["bucket_start"])] = float(
                r["cpu_seconds"] or 0.0
            )
        out: Dict[str, List[float]] = {}
        for app, m in by_app.items():
            series = [float(m.get(k, 0.0)) for k in bucket_keys]
            if any(v > 0 for v in series):
                out[app] = series
        return out

    def db_size_bytes(self) -> int:
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0


def rollup_and_clear(
    accum: AccumStore,
    history: HistoryStore,
    *,
    retention_days: int = 14,
) -> int:
    """Commit accum buckets to SQLite then clear them. Returns rows flushed."""
    n = history.rollup(accum.data)
    history.prune(retention_days)
    accum.data.last_rollup_ts = format_local_iso(time.time())
    accum.clear_buckets_after_flush(write=True)
    return n
