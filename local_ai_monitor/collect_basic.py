"""App-level process collection (app-level parity — no session identity)."""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set

from local_ai_monitor.classify import APPS, AppStats, Proc, classify_direct
from local_ai_monitor.sessionize import SessionStats, Sessionizer, sessionize_all


def _parse_ps_table(text: str) -> List[Proc]:
    procs: List[Proc] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pcpu = float(parts[2])
            pmem = float(parts[3])
            rss_kb = int(parts[4])
        except ValueError:
            continue
        procs.append(
            Proc(
                pid=pid,
                ppid=ppid,
                pcpu=pcpu,
                pmem=pmem,
                rss_kb=rss_kb,
                cmd=parts[5],
            )
        )
    return procs


def _run_ps() -> List[Proc]:
    # macOS: eww appends environment after args (GROK_AGENT / OPENCLAW markers).
    # Linux procps rejects BSD -x and prints to stderr; use -eo first there.
    darwin = [
        "ps",
        "eww",
        "-axo",
        "pid=,ppid=,pcpu=,pmem=,rss=,command=",
    ]
    linux = [
        "ps",
        "-eo",
        "pid=,ppid=,pcpu=,pmem=,rss=,args=",
    ]
    cmds = [darwin, linux] if sys.platform == "darwin" else [linux, darwin]
    for cmd in cmds:
        try:
            out = subprocess.check_output(
                cmd,
                text=True,
                errors="replace",
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue
        procs = _parse_ps_table(out)
        if procs:
            return procs
    return []


def _thread_counts(pids: Set[int]) -> Dict[int, int]:
    counts: Dict[int, int] = defaultdict(int)
    if not pids:
        return counts
    plist = sorted(pids)
    for i in range(0, len(plist), 60):
        chunk = plist[i : i + 60]
        try:
            out = subprocess.check_output(
                ["ps", "-M", "-o", "pid="] + [str(x) for x in chunk],
                text=True,
                errors="replace",
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line.split()[0])
            except ValueError:
                continue
            counts[pid] += 1
    return counts


def collect(include_threads: bool = True) -> Dict[str, AppStats]:
    procs = _run_ps()
    by_pid = {p.pid: p for p in procs}
    children: Dict[int, List[int]] = defaultdict(list)
    for p in procs:
        children[p.ppid].append(p.pid)

    for p in procs:
        p.app = classify_direct(p)

    # Attribute entire process trees under classified roots (workers, helpers).
    # BFS from each classified pid so reparented orphans are only caught by
    # direct markers (GROK_AGENT, etc.), not by walking through launchd.
    roots = [p.pid for p in procs if p.app]
    queue = list(roots)
    seen = set(roots)
    while queue:
        pid = queue.pop()
        parent_app = by_pid[pid].app
        if not parent_app:
            continue
        for cid in children.get(pid, []):
            child = by_pid.get(cid)
            if child is None:
                continue
            if child.app is None:
                child.app = parent_app
            if cid not in seen:
                seen.add(cid)
                queue.append(cid)

    stats = {name: AppStats(name=name) for name in APPS}
    classified: Set[int] = set()
    for p in procs:
        if not p.app:
            continue
        if p.app not in stats:
            stats[p.app] = AppStats(name=p.app)
        s = stats[p.app]
        if p.pid in s.pids:
            continue
        s.pids.add(p.pid)
        s.pcpu += p.pcpu
        s.pmem += p.pmem
        s.rss_kb += p.rss_kb
        classified.add(p.pid)
        if len(s.samples) < 3:
            s.samples.append(f"{p.basename[:24]}:{p.pid}")

    if include_threads and classified:
        tc = _thread_counts(classified)
        for s in stats.values():
            s.threads = sum(tc.get(pid, 0) for pid in s.pids)

    return stats


def collect_sessions(
    include_threads: bool = True,
    sessionizer: Optional[Sessionizer] = None,
    skip_lsof: bool = False,
    include_tokens: bool = True,
) -> List[SessionStats]:
    """Session-level collection + optional token enrichment."""
    procs = _run_ps()
    sessions = sessionize_all(
        procs,
        include_threads=include_threads,
        sessionizer=sessionizer,
        skip_lsof=skip_lsof,
    )
    if include_tokens:
        from local_ai_monitor.tokens import enrich_sessions_with_tokens

        enrich_sessions_with_tokens(sessions)
    return sessions
