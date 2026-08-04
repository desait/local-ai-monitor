"""Host *headroom* and freeze-risk — not free-page emptiness.

Product law (2026-07-30): macOS keeps free pages low by design. Alerting on
``vm.page_free_count`` alone produces false "Memory tight" while
``memory_pressure`` is comfortable and swap is idle.

What the user needs: can I open more apps / AI sessions without beachball?

Composite:
  headroom_mb  ≈ cheap reclaimable mass (free + speculative + purgeable
                 + fraction of file-backed cache)
  thrash_score ≈ rate of swap / pageouts / compressor churn (deltas)

Band (API still ok|warn|hard for menu compatibility):
  ok   = green  — multitask OK
  warn = yellow — caution
  hard = red    — freeze risk / thrash
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

from local_ai_monitor.config import ensure_state_dir, state_dir
from local_ai_monitor.resource.physics import PhysicsSample, page_size_bytes, parse_vm_stat, sample_physics

# Fraction of file-backed pages treated as reclaimable under pressure (not instant,
# but the mass macOS will drop before thrashing anonymous/swap).
FILE_BACKED_RECLAIM = 0.75

# Headroom floors on an 8 GB host (MB). Scaled lightly by RAM later.
# Calibrated initial defaults — headroom calibration can refine into headroom-calib.json.
HEADROOM_OK_MB = 1500.0  # ≥1.5 GB cheap reclaim → green
HEADROOM_WARN_MB = 600.0  # below this → at least yellow

# Thrash score weights (per 60s normalized rates).
# Swap is the beachball signal. Pageouts matter. Compressor churn alone is
# normal on 8 GB and must NOT red-line an otherwise healthy host.
THRASH_SWAP_WEIGHT = 2.0
THRASH_PAGEOUT_WEIGHT = 0.05  # pageouts accumulate; low weight
THRASH_DECOMP_WEIGHT = 0.02  # only applied when swap/page pressure present
THRASH_HARD = 25.0
THRASH_WARN = 8.0
# Short sample intervals (dt≈1s) must not amplify tiny deltas into hard band.
# Require a usable window and floor the rate-normalization denominator.
MIN_THRASH_DT_S = 10.0
MIN_THRASH_RATE_DT_S = 15.0


@dataclass(frozen=True)
class HeadroomSample:
    ok: bool
    band: str  # ok | warn | hard | unknown
    headroom_mb: Optional[float]
    thrash_score: float
    free_mb: Optional[float]
    speculative_mb: Optional[float]
    purgeable_mb: Optional[float]
    file_backed_mb: Optional[float]
    anonymous_mb: Optional[float]
    wired_mb: Optional[float]
    compressor_mb: Optional[float]
    cheap_mb: Optional[float]  # free+spec+purge
    swapins: Optional[int]
    swapouts: Optional[int]
    pageouts: Optional[int]
    decompressions: Optional[int]
    compressions: Optional[int]
    load_1: Optional[float]
    memsize_mb: Optional[float]
    reason: str
    physics: Dict[str, Any]
    prev_age_s: Optional[float]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _prev_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "headroom-prev.json")


def _calib_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "headroom-calib.json")


def load_calib(state: Optional[str] = None) -> Dict[str, float]:
    path = _calib_path(state)
    base = {
        "headroom_ok_mb": HEADROOM_OK_MB,
        "headroom_warn_mb": HEADROOM_WARN_MB,
        "thrash_warn": THRASH_WARN,
        "thrash_hard": THRASH_HARD,
        "file_backed_reclaim": FILE_BACKED_RECLAIM,
    }
    if not os.path.isfile(path):
        return base
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return base
        out = dict(base)
        for k in base:
            if k in raw:
                try:
                    out[k] = float(raw[k])
                except (TypeError, ValueError):
                    pass
        return out
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return base


def save_calib(cfg: Dict[str, float], state: Optional[str] = None) -> str:
    ensure_state_dir(state)
    path = _calib_path(state)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _pages_to_mb(pages: Optional[int], page_size: int) -> Optional[float]:
    if pages is None:
        return None
    return round(float(pages) * float(page_size) / (1024.0 * 1024.0), 1)


def _vm_full() -> Tuple[Dict[str, int], int]:
    ps = page_size_bytes()
    try:
        import subprocess

        r = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode != 0:
            return {}, ps
        return parse_vm_stat(r.stdout or ""), ps
    except (OSError, subprocess.TimeoutExpired):
        return {}, ps


def _load_prev(state: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = _prev_path(state)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_prev(row: Dict[str, Any], state: Optional[str] = None) -> None:
    ensure_state_dir(state)
    path = _prev_path(state)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(row, f, indent=2)
            f.write("\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def compute_headroom_mb(
    *,
    free_mb: float,
    speculative_mb: float,
    purgeable_mb: float,
    file_backed_mb: float,
    file_reclaim: float = FILE_BACKED_RECLAIM,
) -> float:
    cheap = free_mb + speculative_mb + purgeable_mb
    return round(cheap + max(0.0, file_backed_mb) * float(file_reclaim), 1)


def thrash_score_from_deltas(
    *,
    dt_s: float,
    d_swapins: int,
    d_swapouts: int,
    d_pageouts: int,
    d_decompressions: int,
) -> float:
    if dt_s <= 0:
        return 0.0
    # Counter init / first full vm_stat after a partial sample: lifetime counters
    # can jump by hundreds of thousands. That is not thrash — refuse the delta.
    if d_swapins < 0 or d_swapouts < 0 or d_pageouts < 0 or d_decompressions < 0:
        return 0.0
    if d_decompressions > 50_000 or d_pageouts > 20_000 or (d_swapins + d_swapouts) > 5_000:
        return 0.0
    # Normalize to per-60s rates. Floor dt so a 1s sample with 1–2 page events
    # cannot scale to a false hard band (dt≈1 → ×60 was the bug).
    effective_dt = max(float(dt_s), MIN_THRASH_RATE_DT_S)
    scale = 60.0 / effective_dt
    swap_r = min(max(0, d_swapins + d_swapouts) * scale, 500.0)
    page_r = min(max(0, d_pageouts) * scale, 5000.0)
    decomp_r = min(max(0, d_decompressions) * scale, 10000.0)
    swap_term = THRASH_SWAP_WEIGHT * swap_r
    page_term = THRASH_PAGEOUT_WEIGHT * page_r
    # Idle compressor traffic is normal — only count when memory is actually stressed.
    decomp_term = 0.0
    if swap_term + page_term >= 1.0:
        decomp_term = THRASH_DECOMP_WEIGHT * decomp_r
    return round(swap_term + page_term + decomp_term, 2)


def band_for_headroom(
    headroom_mb: Optional[float],
    thrash: float,
    *,
    ok_mb: float = HEADROOM_OK_MB,
    warn_mb: float = HEADROOM_WARN_MB,
    thrash_warn: float = THRASH_WARN,
    thrash_hard: float = THRASH_HARD,
    physics_ok: bool = True,
) -> str:
    if not physics_ok or headroom_mb is None:
        return "unknown"
    # Thrash dominates — beachball is a rate problem.
    if thrash >= thrash_hard:
        return "hard"
    if thrash >= thrash_warn:
        # At least warn; hard if headroom also thin
        if headroom_mb < warn_mb:
            return "hard"
        return "warn"
    if headroom_mb < warn_mb:
        return "hard"
    if headroom_mb < ok_mb:
        return "warn"
    return "ok"


def sample_headroom(
    *,
    state: Optional[str] = None,
    physics: Optional[PhysicsSample] = None,
    vm_map: Optional[Dict[str, int]] = None,
    page_size: Optional[int] = None,
    persist_prev: bool = True,
) -> HeadroomSample:
    """One headroom/freeze-risk sample. Stores counters for next delta."""
    calib = load_calib(state)
    phys = physics if physics is not None else sample_physics()
    ps = int(page_size or phys.page_size or page_size_bytes())
    vm = vm_map if vm_map is not None else {}
    if not vm:
        vm, ps2 = _vm_full()
        if ps2:
            ps = ps2

    def g(*keys: str) -> Optional[int]:
        for k in keys:
            if k in vm:
                return int(vm[k])
        return None

    free_p = phys.free_pages if phys.free_pages is not None else g("pages free")
    spec_p = phys.speculative_pages if phys.speculative_pages is not None else g("pages speculative")
    purg_p = phys.purgeable_pages if phys.purgeable_pages is not None else g("pages purgeable")
    file_p = g("file-backed pages")
    anon_p = g("anonymous pages")
    wired_p = g("pages wired down")
    comp_p = (
        phys.compressor_pages
        if phys.compressor_pages is not None
        else g("pages occupied by compressor")
    )
    swapins = phys.swapins if phys.swapins is not None else g("swapins")
    swapouts = phys.swapouts if phys.swapouts is not None else g("swapouts")
    pageouts = g("pageouts")
    decomp = g("decompressions")
    compn = g("compressions")

    free_mb = _pages_to_mb(free_p, ps)
    spec_mb = _pages_to_mb(spec_p or 0, ps) or 0.0
    purg_mb = _pages_to_mb(purg_p or 0, ps) or 0.0
    file_mb = _pages_to_mb(file_p or 0, ps) or 0.0
    anon_mb = _pages_to_mb(anon_p, ps)
    wired_mb = _pages_to_mb(wired_p, ps)
    comp_mb = _pages_to_mb(comp_p, ps)
    cheap = (free_mb or 0.0) + spec_mb + purg_mb

    mem_mb = None
    if phys.memsize_bytes:
        mem_mb = round(phys.memsize_bytes / (1024.0 * 1024.0), 1)

    # Scale ok/warn floors lightly by RAM (base 8 GB).
    scale = 1.0
    if mem_mb and mem_mb > 0:
        scale = max(0.5, min(4.0, mem_mb / 8192.0))
    ok_mb = float(calib["headroom_ok_mb"]) * scale
    warn_mb = float(calib["headroom_warn_mb"]) * scale

    if free_mb is None:
        return HeadroomSample(
            ok=False,
            band="unknown",
            headroom_mb=None,
            thrash_score=0.0,
            free_mb=None,
            speculative_mb=spec_mb,
            purgeable_mb=purg_mb,
            file_backed_mb=file_mb,
            anonymous_mb=anon_mb,
            wired_mb=wired_mb,
            compressor_mb=comp_mb,
            cheap_mb=cheap,
            swapins=swapins,
            swapouts=swapouts,
            pageouts=pageouts,
            decompressions=decomp,
            compressions=compn,
            load_1=phys.load_1,
            memsize_mb=mem_mb,
            reason="free pages unknown",
            physics=phys.to_dict(),
            prev_age_s=None,
            error="no free pages",
        )

    headroom = compute_headroom_mb(
        free_mb=free_mb,
        speculative_mb=spec_mb,
        purgeable_mb=purg_mb,
        file_backed_mb=file_mb,
        file_reclaim=float(calib["file_backed_reclaim"]),
    )

    now = time.time()
    prev = _load_prev(state)
    thrash = 0.0
    prev_age = None
    if prev and prev.get("ts"):
        try:
            dt = now - float(prev["ts"])
            prev_age = dt
            # Sub-MIN_THRASH_DT_S windows are noise — leave thrash=0 until a
            # real interval accumulates (prevents 1s LaunchAgent thrash false hard).
            if MIN_THRASH_DT_S <= dt <= 3600.0:
                thrash = thrash_score_from_deltas(
                    dt_s=dt,
                    d_swapins=int(swapins or 0) - int(prev.get("swapins") or 0),
                    d_swapouts=int(swapouts or 0) - int(prev.get("swapouts") or 0),
                    d_pageouts=int(pageouts or 0) - int(prev.get("pageouts") or 0),
                    d_decompressions=int(decomp or 0) - int(prev.get("decompressions") or 0),
                )
        except (TypeError, ValueError):
            thrash = 0.0

    band = band_for_headroom(
        headroom,
        thrash,
        ok_mb=ok_mb,
        warn_mb=warn_mb,
        thrash_warn=float(calib["thrash_warn"]),
        thrash_hard=float(calib["thrash_hard"]),
        physics_ok=True,
    )

    if thrash >= float(calib["thrash_hard"]):
        reason = (
            f"freeze-risk thrash_score={thrash} "
            f"(swap/pageout/compressor rates high); headroom≈{headroom:.0f} MB"
        )
    elif thrash >= float(calib["thrash_warn"]):
        reason = (
            f"elevated thrash_score={thrash}; headroom≈{headroom:.0f} MB "
            f"(cheap≈{cheap:.0f} + file-cache reclaim)"
        )
    elif headroom < warn_mb:
        reason = (
            f"thin headroom≈{headroom:.0f} MB "
            f"(free {free_mb:.0f} + cache reclaim; thrash={thrash})"
        )
    elif headroom < ok_mb:
        reason = (
            f"moderate headroom≈{headroom:.0f} MB — caution for large new loads; thrash={thrash}"
        )
    else:
        reason = (
            f"headroom≈{headroom:.0f} MB (free {free_mb:.0f} is normal emptiness); "
            f"thrash={thrash} — multitask OK"
        )

    if persist_prev:
        _save_prev(
            {
                "ts": now,
                "swapins": swapins,
                "swapouts": swapouts,
                "pageouts": pageouts,
                "decompressions": decomp,
                "compressions": compn,
                "headroom_mb": headroom,
                "band": band,
                "thrash_score": thrash,
            },
            state=state,
        )

    return HeadroomSample(
        ok=True,
        band=band,
        headroom_mb=headroom,
        thrash_score=thrash,
        free_mb=free_mb,
        speculative_mb=spec_mb,
        purgeable_mb=purg_mb,
        file_backed_mb=file_mb,
        anonymous_mb=anon_mb,
        wired_mb=wired_mb,
        compressor_mb=comp_mb,
        cheap_mb=round(cheap, 1),
        swapins=swapins,
        swapouts=swapouts,
        pageouts=pageouts,
        decompressions=decomp,
        compressions=compn,
        load_1=phys.load_1,
        memsize_mb=mem_mb,
        reason=reason,
        physics=phys.to_dict(),
        prev_age_s=prev_age,
        error=None,
    )


def append_headroom_evidence(
    sample: HeadroomSample,
    *,
    tag: str = "observe",
    path: Optional[str] = None,
) -> str:
    """Append JSONL evidence under project or state."""
    if path is None:
        # Prefer in-repo evidence when running from checkout
        root = os.environ.get("LOCAL_AI_MONITOR_HEADROOM_EVIDENCE")
        if not root:
            # default state
            ensure_state_dir()
            root = os.path.join(state_dir(), "headroom-samples.jsonl")
        path = root
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    row = sample.to_dict()
    row["tag"] = tag
    row["ts_unix"] = time.time()
    row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path
