"""Host memory physics — free pages, pressure band inputs, loadavg.

Stdlib + stock macOS tools only. Unknown beats lying: failed sample → ok=False.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


_VM_STAT_LINE = re.compile(
    r'^["\']?([^"\':]+)["\']?\s*:\s*([0-9]+)\.?\s*$'
)


@dataclass(frozen=True)
class PhysicsSample:
    ok: bool
    page_size: int
    free_pages: Optional[int]
    free_mb: Optional[float]
    speculative_pages: Optional[int]
    purgeable_pages: Optional[int]
    compressor_pages: Optional[int]
    swapins: Optional[int]
    swapouts: Optional[int]
    memsize_bytes: Optional[int]
    memory_pressure_raw: Optional[int]
    load_1: Optional[float]
    load_5: Optional[float]
    load_15: Optional[float]
    source: str
    swap_used_mb: Optional[float] = None
    swap_total_mb: Optional[float] = None
    swap_avail_mb: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def page_size_bytes() -> int:
    try:
        n = int(os.sysconf("SC_PAGE_SIZE"))
        if n > 0:
            return n
    except (AttributeError, OSError, ValueError):
        pass
    return 16384


def parse_vm_stat(text: str) -> Dict[str, int]:
    """Parse `vm_stat` stdout into a flat int map (keys normalized)."""
    out: Dict[str, int] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("mach virtual"):
            continue
        m = _VM_STAT_LINE.match(line)
        if not m:
            continue
        key = m.group(1).strip().strip('"').lower()
        try:
            out[key] = int(m.group(2))
        except ValueError:
            continue
    return out


def _sysctl_int(name: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode != 0:
            return None
        return int((r.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _physical_memory_bytes_fallback() -> Optional[int]:
    """Best-effort physical RAM when `sysctl hw.memsize` is unavailable."""
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _sysctl_swapusage() -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (used_mb, total_mb, avail_mb) from `sysctl vm.swapusage`."""
    try:
        r = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode != 0:
            return None, None, None
        text = r.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return None, None, None

    vals: Dict[str, float] = {}
    for key in ("total", "used", "available"):
        m = re.search(rf"{key}\s*=\s*([0-9.]+)([KMG])", text, re.IGNORECASE)
        if not m:
            continue
        n = float(m.group(1))
        unit = m.group(2).upper()
        if unit == "K":
            n /= 1024.0
        elif unit == "G":
            n *= 1024.0
        vals[key] = round(n, 1)
    return vals.get("used"), vals.get("total"), vals.get("available")


def _vm_stat_raw() -> Optional[str]:
    try:
        r = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode != 0:
            return None
        return r.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return None


def _physics_json_path() -> str:
    from local_ai_monitor.config import state_dir

    return os.path.join(state_dir(), "physics.json")


def _try_native_physics_json() -> Optional[PhysicsSample]:
    """Prefer C sensor output when fresh (<20s) — lower overhead path."""
    path = _physics_json_path()
    try:
        st = os.stat(path)
        age = time.time() - st.st_mtime
        if age > 20.0:
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or not raw.get("ok"):
            return None
        free = raw.get("free_pages")
        if free is None:
            return None
        free = int(free)
        ps = int(raw.get("page_size") or page_size_bytes())
        free_mb = raw.get("free_mb")
        if free_mb is None:
            free_mb = round(free * ps / (1024.0 * 1024.0), 1)
        return PhysicsSample(
            ok=True,
            page_size=ps,
            free_pages=free,
            free_mb=float(free_mb) if free_mb is not None else None,
            speculative_pages=None,
            purgeable_pages=None,
            compressor_pages=int(raw["compressor_pages"])
            if raw.get("compressor_pages") is not None
            else None,
            swapins=int(raw["swapins"]) if raw.get("swapins") is not None else None,
            swapouts=int(raw["swapouts"]) if raw.get("swapouts") is not None else None,
            memsize_bytes=int(raw["memsize_bytes"])
            if raw.get("memsize_bytes") is not None
            else None,
            memory_pressure_raw=int(raw["memory_pressure_raw"])
            if raw.get("memory_pressure_raw") is not None
            else None,
            load_1=float(raw["load_1"]) if raw.get("load_1") is not None else None,
            load_5=None,
            load_15=None,
            source="native:physics.json",
            swap_used_mb=float(raw["swap_used_mb"])
            if raw.get("swap_used_mb") is not None
            else None,
            swap_total_mb=float(raw["swap_total_mb"])
            if raw.get("swap_total_mb") is not None
            else None,
            swap_avail_mb=float(raw["swap_avail_mb"])
            if raw.get("swap_avail_mb") is not None
            else None,
            error=None,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return None


def sample_physics(
    *,
    vm_stat_text: Optional[str] = None,
    free_pages_override: Optional[int] = None,
    page_size: Optional[int] = None,
) -> PhysicsSample:
    """Sample host memory physics.

    Prefer C `local-ai-monitor-sensor` physics.json when fresh; else sysctl / vm_stat.
    Test hooks: vm_stat_text / free_pages_override.
    """
    ps = int(page_size or page_size_bytes())
    src_parts = []
    free: Optional[int] = free_pages_override
    if free is not None:
        src_parts.append("override")
    elif vm_stat_text is None:
        native = _try_native_physics_json()
        if native is not None:
            return native
        free = _sysctl_int("vm.page_free_count")
        if free is not None:
            src_parts.append("sysctl:vm.page_free_count")
    else:
        free = _sysctl_int("vm.page_free_count")
        if free is not None:
            src_parts.append("sysctl:vm.page_free_count")

    vm_map: Dict[str, int] = {}
    raw = vm_stat_text
    if raw is None:
        # Always pull vm_stat when possible (compressor/swap explain-only).
        raw = _vm_stat_raw()
    if raw is not None:
        vm_map = parse_vm_stat(raw)
        if "vm_stat" not in "+".join(src_parts):
            src_parts.append("vm_stat")
        if free is None and "pages free" in vm_map:
            free = vm_map["pages free"]

    load_1 = load_5 = load_15 = None
    try:
        la = os.getloadavg()
        load_1, load_5, load_15 = float(la[0]), float(la[1]), float(la[2])
        src_parts.append("loadavg")
    except (OSError, AttributeError):
        pass

    memsize = _sysctl_int("hw.memsize")
    if memsize is not None:
        src_parts.append("sysctl:hw.memsize")
    else:
        memsize = _physical_memory_bytes_fallback()
        if memsize is not None:
            src_parts.append("sysconf:physical_memory")
    pressure = _sysctl_int("vm.memory_pressure")
    if pressure is not None:
        src_parts.append("sysctl:vm.memory_pressure")
    swap_used_mb, swap_total_mb, swap_avail_mb = _sysctl_swapusage()
    if swap_used_mb is not None or swap_total_mb is not None:
        src_parts.append("sysctl:vm.swapusage")

    if free is None and free_pages_override is None:
        return PhysicsSample(
            ok=False,
            page_size=ps,
            free_pages=None,
            free_mb=None,
            speculative_pages=vm_map.get("pages speculative"),
            purgeable_pages=vm_map.get("pages purgeable"),
            compressor_pages=vm_map.get("pages occupied by compressor"),
            swapins=vm_map.get("swapins"),
            swapouts=vm_map.get("swapouts"),
            memsize_bytes=memsize,
            memory_pressure_raw=pressure,
            load_1=load_1,
            load_5=load_5,
            load_15=load_15,
            source="+".join(src_parts) if src_parts else "none",
            swap_used_mb=swap_used_mb,
            swap_total_mb=swap_total_mb,
            swap_avail_mb=swap_avail_mb,
            error="could not read free pages",
        )

    free_mb = (float(free) * float(ps) / (1024.0 * 1024.0)) if free is not None else None
    return PhysicsSample(
        ok=True,
        page_size=ps,
        free_pages=free,
        free_mb=round(free_mb, 1) if free_mb is not None else None,
        speculative_pages=vm_map.get("pages speculative"),
        purgeable_pages=vm_map.get("pages purgeable"),
        compressor_pages=vm_map.get("pages occupied by compressor"),
        swapins=vm_map.get("swapins"),
        swapouts=vm_map.get("swapouts"),
        memsize_bytes=memsize,
        memory_pressure_raw=pressure,
        load_1=load_1,
        load_5=load_5,
        load_15=load_15,
        source="+".join(src_parts) if src_parts else "unknown",
        swap_used_mb=swap_used_mb,
        swap_total_mb=swap_total_mb,
        swap_avail_mb=swap_avail_mb,
        error=None,
    )
