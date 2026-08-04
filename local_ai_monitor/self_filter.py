"""Self-exclusion — never attribute the monitor stack to AI apps."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set

from local_ai_monitor.classify import Proc

SELF_PATTERNS = (
    r"(?:^|/|\s)local-ai-monitor(?:\s|$)",
    r"\blocal_ai_monitor\b",  # python -m local_ai_monitor
    r"local_ai_monitor(?:\.|/)",
    r"local-ai-monitor-menubar",
    r"Local AI Monitor Menu",
    r"com\.user\.local-ai-monitor",
    r"(?:^|/|\s)btop-ai(?:\s|$)",
    r"(?:^|/)glances(?:\s|$)",
    r"Python.*\bglances\b",
    r"(?:^|/)tmux(?:\s|$|:)",  # client and "tmux: server"
)

_COMPILED = [re.compile(p, re.IGNORECASE) for p in SELF_PATTERNS]
_ENV_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def argv_portion(cmd: str) -> str:
    """Command + args only — strip trailing KEY=val env block from `ps eww`.

    Critical: shells export LOCAL_AI_MONITOR=…/local-ai-monitor into every child; matching the full
    eww line false-positives real Grok/Claude sessions as 'self'.
    """
    parts = cmd.split()
    out = []
    for t in parts:
        if _ENV_TOKEN.match(t):
            break
        out.append(t)
    return " ".join(out) if out else (parts[0] if parts else "")


def is_self_pattern(p: Proc) -> bool:
    # Match argv + basename only — never the env tail (LOCAL_AI_MONITOR=…/local-ai-monitor).
    c = argv_portion(p.cmd)
    base = p.basename
    for rx in _COMPILED:
        if rx.search(c) or rx.search(base):
            return True
    # Package path in argv (running the monitor as a script module)
    if "/.local/lib/local-ai-monitor/" in c or "/Projects/local-ai-monitor/" in c:
        if "python" in c.lower() or base.startswith("Python") or base == "python3":
            return True
    return False


def is_self_pid(p: Proc, own_pids: Optional[Set[int]] = None) -> bool:
    """True if pid is this process (or explicitly listed).

    Note: do **not** treat ppid alone as self for tree expansion — under
    launchd the parent is pid 1 (launchd) and expanding that tree removes
    every process.
    """
    own = set(own_pids or ())
    own.add(os.getpid())
    return p.pid in own


def expand_self_tree(
    procs: List[Proc], seed: Set[int]
) -> Set[int]:
    """BFS: seed self roots + all descendants."""
    children: Dict[int, List[int]] = defaultdict(list)
    for p in procs:
        children[p.ppid].append(p.pid)
    out = set(seed)
    queue = list(seed)
    while queue:
        pid = queue.pop()
        for cid in children.get(pid, []):
            if cid not in out:
                out.add(cid)
                queue.append(cid)
    return out


def filter_self(
    procs: List[Proc], own_pids: Optional[Set[int]] = None
) -> List[Proc]:
    """Drop monitor stack PIDs.

    Seeds: own pid(s) + pattern matches. Tree-expand only from those seeds.
    Parent pid is excluded singly when it is a real user process (ppid > 1)
    so we do not wipe the machine when parent is launchd/init.
    """
    me = os.getpid()
    try:
        parent = os.getppid()
    except Exception:
        parent = 0

    seed: Set[int] = set(own_pids or ())
    seed.add(me)
    for p in procs:
        if is_self_pattern(p):
            seed.add(p.pid)
    bad = expand_self_tree(procs, seed) if seed else set()
    # Exclude immediate parent only (not its whole tree). Skip init/launchd.
    if parent > 1:
        bad.add(parent)
    if not bad:
        return procs
    return [p for p in procs if p.pid not in bad]
