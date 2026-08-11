"""Physics-first host profile and local adaptive capacity baselines.

The profile is local-only. It stores machine shape and pressure samples, never
commands, paths, prompts, or transcript content.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple

from local_ai_monitor.config import ensure_state_dir, state_dir

PROFILE_VERSION = 1
PROVISIONAL_SECONDS = 60.0
ROLLING_SECONDS = 24.0 * 60.0 * 60.0
MAX_SAMPLES = 7200


@dataclass(frozen=True)
class HostThresholds:
    headroom_ok_mb: float
    headroom_warn_mb: float
    source: str


@dataclass
class HostProfile:
    version: int
    host_id: str
    created_ts: float
    updated_ts: float
    memsize_bytes: Optional[int]
    memsize_mb: Optional[float]
    page_size: int
    cpu_count: int
    machine: str
    status: str
    confidence: float
    headroom_ok_mb: float
    headroom_warn_mb: float
    threshold_source: str
    sample_count: int = 0
    learned: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def profile_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "host-profile.json")


def samples_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "host-profile-samples.jsonl")


def host_id_for(*, memsize_bytes: Optional[int], page_size: int, cpu_count: int, machine: str) -> str:
    mem = int(memsize_bytes or 0)
    return f"v1:{machine}:{mem}:{int(page_size)}:{int(cpu_count)}"


def thresholds_for_memsize(memsize_bytes: Optional[int]) -> HostThresholds:
    """Bounded host-derived headroom thresholds.

    8 GB machines keep the old floor. 24 GB machines land near 3 GB green.
    Larger machines do not scale forever; this is a ceiling, not linear theory.
    """
    if not memsize_bytes or memsize_bytes <= 0:
        return HostThresholds(1500.0, 600.0, "default:unknown-host")
    mem_mb = float(memsize_bytes) / (1024.0 * 1024.0)
    ok_mb = min(4096.0, max(1500.0, mem_mb * 0.125))
    warn_mb = min(1400.0, max(600.0, mem_mb * 0.040))
    if warn_mb >= ok_mb:
        warn_mb = max(600.0, ok_mb * 0.40)
    return HostThresholds(round(ok_mb, 1), round(warn_mb, 1), "host-bounded:v1")


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    else:
        ensure_state_dir()
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _recent_samples(path: str, now: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                ts = _float(row.get("ts"))
                if ts is None or now - ts > ROLLING_SECONDS:
                    continue
                rows.append(row)
    except OSError:
        return []
    return rows[-MAX_SAMPLES:]


def _percentile(values: Iterable[float], pct: float) -> Optional[float]:
    vals = sorted(v for v in values if v >= 0)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _learned(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    headrooms = [_float(r.get("headroom_mb")) for r in rows]
    thrash = [_float(r.get("thrash_score")) for r in rows]
    ai = [_float(r.get("ai_rss_mb")) for r in rows]
    headroom_vals = [v for v in headrooms if v is not None]
    thrash_vals = [v for v in thrash if v is not None]
    ai_vals = [v for v in ai if v is not None]
    out: Dict[str, Any] = {}
    if headroom_vals:
        out["headroom_p10_mb"] = round(_percentile(headroom_vals, 0.10) or 0.0, 1)
        out["headroom_p50_mb"] = round(median(headroom_vals), 1)
    if thrash_vals:
        out["thrash_p95"] = round(_percentile(thrash_vals, 0.95) or 0.0, 2)
    if ai_vals:
        out["ai_rss_p50_mb"] = round(median(ai_vals), 1)
        out["ai_rss_p90_mb"] = round(_percentile(ai_vals, 0.90) or 0.0, 1)
    return out


def _append_sample(
    *,
    state: Optional[str],
    host_id: str,
    headroom_mb: Optional[float],
    thrash_score: float,
    ai_rss_mb: Optional[float] = None,
) -> None:
    if headroom_mb is None:
        return
    ensure_state_dir(state)
    row = {
        "ts": time.time(),
        "host_id": host_id,
        "headroom_mb": round(float(headroom_mb), 1),
        "thrash_score": round(float(thrash_score), 2),
        "ai_rss_mb": round(float(ai_rss_mb), 1) if ai_rss_mb is not None else None,
    }
    try:
        with open(samples_path(state), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    except OSError:
        pass


def get_or_update_profile(
    *,
    state: Optional[str],
    memsize_bytes: Optional[int],
    page_size: int,
    headroom_mb: Optional[float] = None,
    thrash_score: float = 0.0,
    ai_rss_mb: Optional[float] = None,
) -> HostProfile:
    now = time.time()
    cpu = os.cpu_count() or 1
    machine = platform.machine() or "unknown"
    hid = host_id_for(
        memsize_bytes=memsize_bytes,
        page_size=page_size,
        cpu_count=cpu,
        machine=machine,
    )
    thresholds = thresholds_for_memsize(memsize_bytes)
    raw = _read_json(profile_path(state))
    created = now
    if raw and raw.get("host_id") == hid and int(raw.get("version") or 0) == PROFILE_VERSION:
        created = _float(raw.get("created_ts")) or now
    rows = _recent_samples(samples_path(state), now)
    rows = [r for r in rows if r.get("host_id") == hid]
    _append_sample(
        state=state,
        host_id=hid,
        headroom_mb=headroom_mb,
        thrash_score=thrash_score,
        ai_rss_mb=ai_rss_mb,
    )
    if headroom_mb is not None:
        rows.append(
            {
                "ts": now,
                "host_id": hid,
                "headroom_mb": headroom_mb,
                "thrash_score": thrash_score,
                "ai_rss_mb": ai_rss_mb,
            }
        )
    age = max(0.0, now - created)
    status = "ready" if age >= PROVISIONAL_SECONDS and len(rows) >= 3 else "provisional"
    confidence = min(1.0, max(0.0, age / PROVISIONAL_SECONDS) * 0.5 + min(len(rows), 120) / 240.0)
    prof = HostProfile(
        version=PROFILE_VERSION,
        host_id=hid,
        created_ts=created,
        updated_ts=now,
        memsize_bytes=memsize_bytes,
        memsize_mb=round(float(memsize_bytes) / (1024.0 * 1024.0), 1)
        if memsize_bytes
        else None,
        page_size=int(page_size),
        cpu_count=cpu,
        machine=machine,
        status=status,
        confidence=round(confidence, 3),
        headroom_ok_mb=thresholds.headroom_ok_mb,
        headroom_warn_mb=thresholds.headroom_warn_mb,
        threshold_source=thresholds.source,
        sample_count=len(rows),
        learned=_learned(rows),
    )
    try:
        _write_json(profile_path(state), prof.to_dict())
    except OSError:
        pass
    return prof


def append_usage_sample(
    *,
    state: Optional[str],
    host_id: Optional[str],
    headroom_mb: Optional[float],
    thrash_score: float,
    ai_rss_mb: Optional[float],
) -> None:
    """Append a local learning row without rebuilding the profile."""
    if not host_id:
        return
    _append_sample(
        state=state,
        host_id=host_id,
        headroom_mb=headroom_mb,
        thrash_score=thrash_score,
        ai_rss_mb=ai_rss_mb,
    )
