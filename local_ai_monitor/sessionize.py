"""Session identity — sessionize_all, buzz_slug, Grok UUID picker."""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.parse
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from local_ai_monitor.classify import APPS, AppStats, Proc, classify_direct
from local_ai_monitor.self_filter import filter_self

# --- Data model ---


@dataclass(frozen=True)
class SessionKey:
    app: str
    session_id: str
    label: str = ""
    detail: str = ""

    def store_key(self) -> str:
        return f"{self.app}|{self.session_id}"


@dataclass
class SessionStats:
    app: str
    session_id: str
    label: str
    detail: str = ""
    pids: Set[int] = field(default_factory=set)
    pcpu: float = 0.0
    pmem: float = 0.0
    rss_kb: int = 0
    threads: Optional[int] = None  # None = not sampled
    # Token usage (local session stores — optional)
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    tokens_total: Optional[int] = None
    tokens_cached: Optional[int] = None
    context_used: Optional[int] = None
    context_window: Optional[int] = None
    tokens_source: Optional[str] = None

    @property
    def nproc(self) -> int:
        return len(self.pids)

    @property
    def key(self) -> SessionKey:
        return SessionKey(self.app, self.session_id, self.label, self.detail)


# --- Buzz channel + agent identity ---
# Channel = nest project / XPC service (sample-app, sample-meshsim, …)
# Agent   = who is working on that channel (ACP title, tool, or role: tunnel/trace)
# session_id = buzz:{channel}|{agent}

ROLE_SUFFIXES = ("-tunnel", "-access-expiry", "-tunnel-log")

_XPC_RE = re.compile(r"(?:^|\s)XPC_SERVICE_NAME=([^\s]+)")
_BUZZ_DIR_RE = re.compile(
    r"/\.buzz/(?:PROJECTS|REPOS)/([^/\s]+)", re.IGNORECASE
)
_PWD_RE = re.compile(r"(?:^|\s)PWD=([^\s]+)")
_ENV_RE = re.compile(r"(?:^|\s)([A-Z][A-Z0-9_]{2,})=([^\s]*)")
_SESS_EVENTS_RE = re.compile(
    r"/\.grok/sessions/[^/]+/([0-9a-fA-F-]{20,})/events\.jsonl(?:$|\s)"
)
_SESS_ANY_RE = re.compile(
    r"/\.grok/sessions/[^/]+/([0-9a-fA-F-]{20,})/"
)
_SESS_ENC_RE = re.compile(r"/\.grok/sessions/([^/]+)/([0-9a-fA-F-]{20,})/")
_OPENCLAW_PORT_RE = re.compile(r"--port[= ](\d+)")


def _norm_slug(s: str) -> str:
    return s.casefold().strip()


def _parse_xpc(cmd: str) -> Optional[str]:
    m = _XPC_RE.search(cmd)
    return m.group(1) if m else None


def _env_get(cmd: str, key: str) -> Optional[str]:
    """Extract KEY=value from ps eww command+env tail."""
    # Prefer last occurrence (env often after argv)
    found = None
    for m in _ENV_RE.finditer(cmd):
        if m.group(1) == key:
            found = m.group(2)
    return found


def _strip_buzz_prefix(name: str) -> str:
    if name.casefold().startswith("buzz-"):
        return name[len("buzz-") :]
    return name


def buzz_channel(proc: Proc) -> str:
    """Channel id: project / XPC nest (not the worker persona)."""
    xpc = _parse_xpc(proc.cmd)
    if xpc and xpc.startswith("com.buzz."):
        rest = xpc[len("com.buzz.") :]
        for suf in ROLE_SUFFIXES:
            if rest.endswith(suf):
                rest = rest[: -len(suf)]
                break
        if rest:
            return _norm_slug(rest)

    m = _BUZZ_DIR_RE.search(proc.cmd)
    if m:
        return _norm_slug(_strip_buzz_prefix(m.group(1)))

    pwd = _env_get(proc.cmd, "PWD") or ""
    m2 = _BUZZ_DIR_RE.search(pwd) if pwd else None
    if m2:
        return _norm_slug(_strip_buzz_prefix(m2.group(1)))
    # PWD may be nested: .../PROJECTS/buzz-foo/...
    m3 = re.search(r"/\.buzz/(?:PROJECTS|REPOS)/([^/\s]+)", pwd, re.I)
    if m3:
        return _norm_slug(_strip_buzz_prefix(m3.group(1)))

    if "/Applications/Buzz.app/" in proc.cmd or "Application Support/Buzz/" in proc.cmd:
        return "desktop"

    return "unknown"


def buzz_agent(proc: Proc) -> str:
    """Agent working on the channel: ACP persona/tool, or process role."""
    title = _env_get(proc.cmd, "BUZZ_ACP_SESSION_TITLE")
    agent_cmd = _env_get(proc.cmd, "BUZZ_ACP_AGENT_COMMAND")
    model = _env_get(proc.cmd, "BUZZ_ACP_MODEL")
    tool = ""
    if agent_cmd:
        tool = os.path.basename(agent_cmd.rstrip("/"))
        if tool in ("", "agent"):
            tool = ""
    if title:
        title = title.strip()
        if tool:
            return _norm_slug(f"{title}/{tool}")
        if model:
            # keep short: Operator·grok-4.5 → Operator/grok-4.5
            return _norm_slug(f"{title}/{model}")
        return _norm_slug(title)

    xpc = _parse_xpc(proc.cmd) or ""
    for suf, role in (
        ("-tunnel", "tunnel"),
        ("-tunnel-log", "tunnel"),
        ("-access-expiry", "access"),
    ):
        if xpc.endswith(suf) or f"{suf}" in xpc:
            return role
    if "buzz_channel_trace" in proc.cmd:
        return "channel-trace"
    if proc.basename == "ngrok" or "/ngrok" in proc.exe:
        return "tunnel"
    if "BUZZ_MANAGED_AGENT=" in proc.cmd and "BUZZ_ACP_" not in proc.cmd:
        return "managed"
    if "BUZZ_ACP_" in proc.cmd:
        return "acp-worker"
    if proc.basename in ("node", "next-server", "Python", "python3", "python"):
        return "runtime"
    if proc.basename:
        return _norm_slug(proc.basename)
    return "worker"


def buzz_identity(proc: Proc) -> Tuple[str, str, str, str]:
    """Return (session_id, label, channel, agent).

    session_id: buzz:{channel}|{agent}
    label:      {channel} · {agent}   (shown under Buzz)
    """
    channel = buzz_channel(proc)
    agent = buzz_agent(proc)
    sid = f"buzz:{channel}|{agent}"
    label = f"{channel} · {agent}"
    return sid, label, channel, agent


def buzz_slug(proc: Proc) -> str:
    """Back-compat: channel only (not agent). Prefer buzz_identity for new code."""
    return buzz_channel(proc)


def openclaw_port(cmd: str) -> str:
    m = _OPENCLAW_PORT_RE.search(cmd)
    return m.group(1) if m else "?"


def label_openclaw(p: Proc) -> str:
    return f"gateway :{openclaw_port(p.cmd)}"


# --- Grok UUID cache / picker ---


@dataclass
class CachedUuid:
    uuid: str
    sticky_samples: int = 0


class GrokUuidCache:
    def __init__(self) -> None:
        self._by_pid: Dict[int, CachedUuid] = {}

    def purge(self, live_pids: Set[int]) -> None:
        dead = [pid for pid in self._by_pid if pid not in live_pids]
        for pid in dead:
            del self._by_pid[pid]

    def pick(
        self,
        root_pid: int,
        lsof_paths: List[str],
        mtimes: Optional[Dict[str, float]] = None,
    ) -> Optional[str]:
        """Deterministic multi-UUID picker (design §Grok UUID picker)."""
        mtimes = mtimes or {}

        def mtime(path: str) -> float:
            if path in mtimes:
                return mtimes[path]
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        candidates: List[Tuple[str, str, float]] = []
        for path in lsof_paths:
            m = _SESS_EVENTS_RE.search(path)
            if m:
                candidates.append((m.group(1), path, mtime(path)))
        if not candidates:
            for path in lsof_paths:
                m = _SESS_ANY_RE.search(path)
                if m:
                    candidates.append((m.group(1), path, mtime(path)))
        if not candidates:
            return None

        # max (mtime, uuid) for total order
        best = max(candidates, key=lambda t: (t[2], t[0]))
        uuid = best[0]

        prev = self._by_pid.get(root_pid)
        if prev is None:
            self._by_pid[root_pid] = CachedUuid(uuid, 0)
            return uuid
        if prev.uuid == uuid:
            prev.sticky_samples = 0
            return uuid

        # sticky: keep prev if still open
        if _uuid_still_open(prev.uuid, lsof_paths):
            return prev.uuid
        prev.sticky_samples += 1
        if prev.sticky_samples >= 2:
            self._by_pid[root_pid] = CachedUuid(uuid, 0)
            return uuid
        return prev.uuid


def _uuid_still_open(uuid: str, paths: List[str]) -> bool:
    needle = f"/{uuid}/"
    return any(needle in p for p in paths)


# --- cwd / start_unix ---


def parse_pwd_from_cmd(cmd: str) -> Optional[str]:
    m = _PWD_RE.search(cmd)
    return m.group(1) if m else None


def is_trustworthy_pwd(pwd: str, home: Optional[str] = None) -> bool:
    """Reject known-truncated prefixes (…/Grok, …/Claude under $HOME)."""
    if not pwd or not pwd.startswith("/"):
        return False
    home = home or os.path.expanduser("~")
    base = os.path.basename(pwd.rstrip("/"))
    # Ambiguous single-segment under home
    if base in ("Grok", "Claude") and (
        pwd == os.path.join(home, base) or pwd.startswith(home + "/")
    ):
        # if path is exactly $HOME/Grok or $HOME/Claude — untrustworthy
        if pwd.rstrip("/") in (
            os.path.join(home, "Grok"),
            os.path.join(home, "Claude"),
        ):
            return False
        # if only one segment under home after home itself
        rel = pwd[len(home) :].lstrip("/")
        if rel.count("/") == 0 and base in ("Grok", "Claude"):
            return False
    # Prefer at least home + 2 components
    if pwd.startswith(home + "/"):
        parts = [x for x in pwd[len(home) :].split("/") if x]
        if len(parts) < 2:
            return False
    return True


def cwd_from_session_paths(paths: List[str]) -> Optional[str]:
    for path in paths:
        m = _SESS_ENC_RE.search(path)
        if m:
            try:
                return urllib.parse.unquote(m.group(1))
            except Exception:
                continue
    return None


def resolve_cwd(
    pid: int,
    cmd: str,
    *,
    lsof_cwd: Optional[str] = None,
    session_open_paths: Optional[List[str]] = None,
    home: Optional[str] = None,
) -> Optional[str]:
    if lsof_cwd and lsof_cwd.startswith("/"):
        return lsof_cwd
    from_sess = cwd_from_session_paths(session_open_paths or [])
    if from_sess:
        return from_sess
    pwd = parse_pwd_from_cmd(cmd)
    if pwd and is_trustworthy_pwd(pwd, home=home):
        return pwd
    # Never return truncated/untrusted PWD as a fallback label path
    return lsof_cwd if (lsof_cwd and lsof_cwd.startswith("/")) else None


def label_cwd(cwd: Optional[str], pid: int) -> str:
    if not cwd:
        return f"pid{pid}"
    base = os.path.basename(cwd.rstrip("/"))
    return base or f"pid{pid}"


# Known project folder spellings for human labels (Codex / Cursor / generic cwd).
_PROJECT_LABELS = {
    "local-ai-monitor": "Local AI Monitor",
    "local_ai_monitor": "Local AI Monitor",
    "localaimonitor": "Local AI Monitor",
    "sample-app": "Sample App",
    "sample_project": "Sample Project",
    "native-340b": "Native 340B",
    "native_340b": "Native 340B",
    "openclaw": "OpenClaw",
    "projects": "Projects",
    "claude projects": "Claude Projects",
}


def label_project(cwd: Optional[str], pid: int) -> str:
    """Human project name from cwd basename — never bare pid if path known."""
    if not cwd or not str(cwd).startswith("/"):
        return f"pid{pid}"
    base = os.path.basename(str(cwd).rstrip("/")) or ""
    if not base or base in (".", ".."):
        return f"pid{pid}"
    key = base.casefold().replace("_", "-")
    # try exact / hyphenated map
    for k, v in _PROJECT_LABELS.items():
        if k.replace("_", "-") == key or k.casefold() == base.casefold():
            return v
    # Title words without inventing vendor jargon
    parts = re.split(r"[-_\s]+", base)
    return " ".join(p[:1].upper() + p[1:] if p else "" for p in parts if p) or f"pid{pid}"


def label_grok(cwd: Optional[str], pid: int, uuid: str) -> str:
    return f"{label_cwd(cwd, pid)} · {uuid[:8]}"


def start_unix(pid: int) -> int:
    """Parse LC_ALL=C ps -o lstart= for pid. 0 on failure."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            errors="replace",
            env={**os.environ, "LC_ALL": "C"},
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0
    if not out:
        return 0
    try:
        # "Tue Jul 28 21:54:34 2026"
        t = time.strptime(out, "%a %b %d %H:%M:%S %Y")
        return int(time.mktime(t))
    except ValueError:
        return 0


def _lsof_bin() -> str:
    # LaunchAgent PATH often omits /usr/sbin — never rely on bare `lsof`.
    for cand in ("/usr/sbin/lsof", "/usr/bin/lsof", "lsof"):
        if cand.startswith("/") and os.path.isfile(cand):
            return cand
    return "lsof"


def lsof_paths_for_pid(pid: int, timeout: float = 2.0) -> List[str]:
    """lsof -a -p PID -F n → list of open paths."""
    try:
        out = subprocess.check_output(
            [_lsof_bin(), "-a", "-p", str(pid), "-F", "n"],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    paths: List[str] = []
    for line in out.splitlines():
        if line.startswith("n"):
            paths.append(line[1:])
    return paths


def lsof_cwd_for_pid(pid: int, timeout: float = 2.0) -> Optional[str]:
    try:
        out = subprocess.check_output(
            [_lsof_bin(), "-a", "-p", str(pid), "-d", "cwd", "-F", "n"],
            text=True,
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            p = line[1:]
            if p.startswith("/"):
                return p
    return None


# --- is_session_root ---


def is_session_root(p: Proc) -> bool:
    """Frozen predicate — requires direct_app set before BFS.

    Catalog tools (Codex, Cursor, …) are roots when directly classified.
    Adding a ToolDef must not require a new branch here.
    """
    if p.app is None:
        return False
    direct = getattr(p, "direct_app", None)
    if not direct:
        return False
    if p.app == "Grok":
        return (
            p.basename == "grok"
            or p.exe.endswith("/grok")
            or p.exe.endswith("/bin/grok")
        )
    if p.app == "Claude CLI":
        return (
            p.basename == "claude"
            or p.exe.endswith("/claude")
            or p.exe.endswith("/bin/claude")
        ) and "/Applications/Claude.app/" not in p.cmd
    if p.app == "Buzz":
        # Shell wrappers that only host ACP workers are not their own agent row.
        # Keep shells that *are* the work (channel-trace loop, etc.).
        if p.basename in ("zsh", "bash", "sh", "dash", "fish"):
            if (
                "BUZZ_ACP_SESSION_TITLE=" in p.cmd
                or "buzz_channel_trace" in p.cmd
                or (_parse_xpc(p.cmd) or "").startswith("com.buzz.")
            ):
                return True
            return False
        return True
    if p.app in ("OpenClaw", "Claude Desktop", "ChatGPT"):
        return True
    # Any other catalog-classified product (Codex, Cursor, OpenAI CLI, future)
    return direct == p.app


def has_env_marker(cmd: str, key: str) -> bool:
    return re.search(rf"(?:^|\s){re.escape(key)}=", cmd) is not None


# --- sessionize_all ---

LsofPathsFn = Callable[[int], List[str]]
LsofCwdFn = Callable[[int], Optional[str]]
StartUnixFn = Callable[[int], int]


@dataclass
class Sessionizer:
    """Stateful sessionizer (Grok UUID sticky cache, lsof refresh)."""

    uuid_cache: GrokUuidCache = field(default_factory=GrokUuidCache)
    sample_n: int = 0
    # injectable for tests
    lsof_paths_fn: Optional[LsofPathsFn] = None
    lsof_cwd_fn: Optional[LsofCwdFn] = None
    start_unix_fn: Optional[StartUnixFn] = None
    lsof_path_overrides: Optional[Dict[int, List[str]]] = None
    lsof_cwd_overrides: Optional[Dict[int, Optional[str]]] = None
    mtime_overrides: Optional[Dict[str, float]] = None
    max_lsof_per_sample: int = 3

    def _paths(self, pid: int) -> List[str]:
        if self.lsof_path_overrides is not None and pid in self.lsof_path_overrides:
            return self.lsof_path_overrides[pid]
        if self.lsof_paths_fn:
            return self.lsof_paths_fn(pid)
        return lsof_paths_for_pid(pid)

    def _cwd(self, pid: int) -> Optional[str]:
        if self.lsof_cwd_overrides is not None and pid in self.lsof_cwd_overrides:
            return self.lsof_cwd_overrides[pid]
        if self.lsof_cwd_fn:
            return self.lsof_cwd_fn(pid)
        return lsof_cwd_for_pid(pid)

    def _start(self, pid: int) -> int:
        if self.start_unix_fn:
            return self.start_unix_fn(pid)
        return start_unix(pid)

    def sessionize(
        self,
        procs: List[Proc],
        *,
        include_threads: bool = False,
        own_pids: Optional[Set[int]] = None,
        skip_lsof: bool = False,
    ) -> List[SessionStats]:
        self.sample_n += 1
        procs = filter_self(procs, own_pids=own_pids)
        # Work on copies of mutable app fields — mutate input procs is OK (same behavior)
        by_pid = {p.pid: p for p in procs}
        children: Dict[int, List[int]] = defaultdict(list)
        for p in procs:
            children[p.ppid].append(p.pid)

        # Step 1 — direct classify
        for p in procs:
            da = classify_direct(p)
            p.direct_app = da  # type: ignore[attr-defined]
            p.app = da

        # Step 2 — BFS tree paint
        roots = [p.pid for p in procs if p.app]
        queue = deque(roots)
        seen = set(roots)
        while queue:
            pid = queue.popleft()
            parent_app = by_pid[pid].app
            if not parent_app:
                continue
            for cid in children.get(pid, []):
                child = by_pid.get(cid)
                if child is None:
                    continue
                if child.app is None:
                    child.app = parent_app
                # conflict: keep child's direct classification
                if cid not in seen:
                    seen.add(cid)
                    queue.append(cid)

        live_pids = set(by_pid)
        self.uuid_cache.purge(live_pids)

        # Step 3–4 — session roots
        pid_to_session: Dict[int, SessionKey] = {}
        lsof_budget = 0

        for p in procs:
            if not is_session_root(p):
                continue
            paths: List[str] = []
            lsof_cwd: Optional[str] = None
            has_path_override = bool(
                self.lsof_path_overrides and p.pid in self.lsof_path_overrides
            )
            has_cwd_override = bool(
                self.lsof_cwd_overrides and p.pid in self.lsof_cwd_overrides
            )
            # Resolve cwd for CLI-style tools (not desktop app aggregates)
            wants_cwd = p.app in (
                "Grok",
                "Claude CLI",
                "Codex",
                "OpenAI CLI",
                "Cursor",
            ) or (
                p.app
                not in (
                    "Buzz",
                    "OpenClaw",
                    "Claude Desktop",
                    "ChatGPT",
                )
            )
            if wants_cwd:
                # Overrides always win (tests / injected state).
                if has_path_override and p.app == "Grok":
                    paths = self._paths(p.pid)
                elif (
                    not skip_lsof
                    and p.app == "Grok"
                    and lsof_budget < self.max_lsof_per_sample
                ):
                    paths = self._paths(p.pid)
                    lsof_budget += 1
                if has_cwd_override:
                    lsof_cwd = self._cwd(p.pid)
                elif not skip_lsof and lsof_budget < self.max_lsof_per_sample:
                    lsof_cwd = self._cwd(p.pid)
                    lsof_budget += 1

            cwd = resolve_cwd(
                p.pid,
                p.cmd,
                lsof_cwd=lsof_cwd,
                session_open_paths=paths,
            )

            if p.app == "Grok":
                uuid = None
                if paths:
                    uuid = self.uuid_cache.pick(
                        p.pid, paths, mtimes=self.mtime_overrides
                    )
                if uuid:
                    key = SessionKey(
                        "Grok",
                        uuid,
                        label_grok(cwd, p.pid, uuid),
                        cwd or "",
                    )
                else:
                    su = self._start(p.pid)
                    key = SessionKey(
                        "Grok",
                        f"pid:{p.pid}:start:{su}",
                        label_cwd(cwd, p.pid),
                        cwd or "",
                    )
            elif p.app == "Claude CLI":
                su = self._start(p.pid)
                key = SessionKey(
                    "Claude CLI",
                    f"pid:{p.pid}:start:{su}",
                    label_cwd(cwd, p.pid),
                    cwd or "",
                )
            elif p.app == "Buzz":
                sid, label, channel, agent = buzz_identity(p)
                detail = f"channel={channel} agent={agent}"
                m = _BUZZ_DIR_RE.search(p.cmd)
                if m:
                    detail = f"{detail} path={m.group(1)}"
                key = SessionKey("Buzz", sid, label, detail)
            elif p.app == "OpenClaw":
                key = SessionKey(
                    "OpenClaw", "svc:gateway", label_openclaw(p), ""
                )
            elif p.app == "Claude Desktop":
                key = SessionKey(
                    "Claude Desktop", "app:claude-desktop", "Claude Desktop", ""
                )
            elif p.app == "ChatGPT":
                key = SessionKey("ChatGPT", "app:chatgpt", "ChatGPT", "")
            elif p.app == "Codex":
                # Prefer human project label; UUID when rollout matches cwd (resume).
                su = self._start(p.pid)
                lab = label_project(cwd, p.pid) if cwd else "Codex"
                sid = f"pid:{p.pid}:start:{su}"
                if cwd:
                    try:
                        from local_ai_monitor.codex_local import codex_session_for_cwd

                        hit = codex_session_for_cwd(cwd)
                        if hit and hit.get("session_id"):
                            sid = str(hit["session_id"])
                            if hit.get("title"):
                                lab = str(hit["title"])[:48]
                    except Exception:
                        pass
                key = SessionKey("Codex", sid, lab, cwd or "")
            elif p.app == "Cursor":
                su = self._start(p.pid)
                lab = label_project(cwd, p.pid) if cwd else "Workspace"
                key = SessionKey(
                    "Cursor",
                    f"pid:{p.pid}:start:{su}",
                    lab,
                    cwd or "",
                )
            elif p.app == "OpenAI CLI":
                su = self._start(p.pid)
                lab = label_project(cwd, p.pid) if cwd else "OpenAI CLI"
                key = SessionKey(
                    "OpenAI CLI",
                    f"pid:{p.pid}:start:{su}",
                    lab,
                    cwd or "",
                )
            else:
                # Generic catalog tool (future entries)
                su = self._start(p.pid)
                lab = label_project(cwd, p.pid) if cwd else (p.app or "Session")
                key = SessionKey(
                    p.app,
                    f"pid:{p.pid}:start:{su}",
                    lab,
                    cwd or "",
                )
            pid_to_session[p.pid] = key

        # Step 5 — attach non-roots to nearest ancestor session
        def nearest_session(pid: int) -> Optional[int]:
            cur = by_pid.get(pid)
            guard = 0
            while cur is not None and guard < 64:
                if cur.ppid in pid_to_session:
                    return cur.ppid
                # walk up
                parent = by_pid.get(cur.ppid)
                if parent is None:
                    return None
                if parent.pid in pid_to_session:
                    return parent.pid
                cur = parent
                guard += 1
            return None

        for p in procs:
            if p.pid in pid_to_session or not p.app:
                continue
            # walk ancestors
            anc_pid: Optional[int] = None
            cur = p
            for _ in range(64):
                parent = by_pid.get(cur.ppid)
                if parent is None:
                    break
                if parent.pid in pid_to_session:
                    anc_pid = parent.pid
                    break
                cur = parent
            if anc_pid is not None:
                pid_to_session[p.pid] = pid_to_session[anc_pid]
            elif p.app == "Grok" and has_env_marker(p.cmd, "GROK_AGENT"):
                su = self._start(p.pid)
                pid_to_session[p.pid] = SessionKey(
                    "Grok", f"pid:{p.pid}:start:{su}", "worker", ""
                )

        # Step 6 — aggregate
        buckets: Dict[Tuple[str, str], SessionStats] = {}
        for p in procs:
            sk = pid_to_session.get(p.pid)
            if sk is None:
                continue
            k = (sk.app, sk.session_id)
            if k not in buckets:
                buckets[k] = SessionStats(
                    app=sk.app,
                    session_id=sk.session_id,
                    label=sk.label,
                    detail=sk.detail,
                )
            s = buckets[k]
            if p.pid in s.pids:
                continue
            s.pids.add(p.pid)
            s.pcpu += p.pcpu
            s.pmem += p.pmem
            s.rss_kb += p.rss_kb
            # prefer non-empty label/detail from root
            if sk.label and (not s.label or s.label.startswith("pid")):
                s.label = sk.label
            if sk.detail and not s.detail:
                s.detail = sk.detail

        sessions = list(buckets.values())
        # stable order: by app order then label
        app_rank = {name: i for i, name in enumerate(APPS)}
        sessions.sort(key=lambda s: (app_rank.get(s.app, 99), s.label, s.session_id))

        if include_threads and sessions:
            all_pids: Set[int] = set()
            for s in sessions:
                all_pids |= s.pids
            tc = _thread_counts(all_pids)
            for s in sessions:
                s.threads = sum(tc.get(pid, 0) for pid in s.pids)
        else:
            for s in sessions:
                s.threads = None

        return sessions


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


# module-level default sessionizer for live use
_default_sessionizer = Sessionizer()


def sessionize_all(
    procs: List[Proc],
    *,
    include_threads: bool = False,
    sessionizer: Optional[Sessionizer] = None,
    skip_lsof: bool = False,
    own_pids: Optional[Set[int]] = None,
) -> List[SessionStats]:
    sz = sessionizer or _default_sessionizer
    return sz.sessionize(
        procs,
        include_threads=include_threads,
        skip_lsof=skip_lsof,
        own_pids=own_pids,
    )


def sessions_to_app_stats(sessions: List[SessionStats]) -> Dict[str, AppStats]:
    """Roll session rows up to app-level AppStats."""
    stats = {name: AppStats(name=name) for name in APPS}
    for s in sessions:
        if s.app not in stats:
            stats[s.app] = AppStats(name=s.app)
        a = stats[s.app]
        for pid in s.pids:
            if pid in a.pids:
                continue
            a.pids.add(pid)
        a.pcpu += s.pcpu
        a.pmem += s.pmem
        a.rss_kb += s.rss_kb
        if s.threads is not None:
            a.threads += s.threads
        if len(a.samples) < 3:
            a.samples.append(f"{s.label[:24]}:{s.session_id[:12]}")
    return stats


def make_proc(
    pid: int,
    ppid: int,
    cmd: str,
    pcpu: float = 0.0,
    pmem: float = 0.0,
    rss_kb: int = 0,
) -> Proc:
    return Proc(
        pid=pid, ppid=ppid, pcpu=pcpu, pmem=pmem, rss_kb=rss_kb, cmd=cmd
    )
