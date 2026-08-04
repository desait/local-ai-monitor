"""tmux + Glances dashboard mux.

Layout (design ):
  pane 0: glances -t 2 --disable-plugin docker
  pane 1: local-ai-monitor tui

CLI resolution:
  bare interactive → dash if tools present, else tui
  $TMUX set → tui (no nested dash) unless user forces `dash`
  `local-ai-monitor dash` → force mux even inside tmux
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from local_ai_monitor.config import load_config

# Sentinel: omit arg → auto-detect; pass None → treat as missing (tests).
_AUTO: Any = object()


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def find_tmux() -> Optional[str]:
    return which("tmux")


def find_glances() -> Optional[str]:
    return which("glances")


def find_local_ai_monitor() -> str:
    """Prefer PATH local-ai-monitor, else ~/.local/bin/local-ai-monitor, else python -m local_ai_monitor."""
    w = which("local-ai-monitor")
    if w:
        return w
    home = os.path.expanduser("~")
    candidate = os.path.join(home, ".local", "bin", "local-ai-monitor")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    # Fallback: same interpreter running us
    return f"{shlex.quote(sys.executable)} -m local_ai_monitor"


def deps_available(
    tmux_path: Any = _AUTO,
    glances_path: Any = _AUTO,
) -> Tuple[bool, Optional[str], Optional[str]]:
    t = find_tmux() if tmux_path is _AUTO else tmux_path
    g = find_glances() if glances_path is _AUTO else glances_path
    return bool(t and g), t, g


def resolve_interactive_target(
    *,
    force_dash: bool = False,
    force_tui: bool = False,
    env: Optional[Dict[str, str]] = None,
    tmux_path: Any = _AUTO,
    glances_path: Any = _AUTO,
) -> str:
    """Return 'dash' or 'tui' for bare interactive launch."""
    if force_tui:
        return "tui"
    if force_dash:
        return "dash"
    e = env if env is not None else os.environ
    if e.get("TMUX"):
        # Inside tmux: never auto-nest dash
        return "tui"
    ok, _, _ = deps_available(tmux_path, glances_path)
    if ok:
        return "dash"
    return "tui"


def glances_command(
    glances_bin: str,
    glances_args: Optional[Sequence[str]] = None,
) -> List[str]:
    args = list(glances_args) if glances_args is not None else ["-t", "2", "--disable-plugin", "docker"]
    return [glances_bin] + args


def tui_command(
    local_ai_monitor: str,
    *,
    interval: Optional[float] = None,
    view: Optional[str] = None,
    threads: bool = False,
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build argv for the AI pane. local_ai_monitor may be a single path or 'python -m local_ai_monitor' string."""
    if " -m " in local_ai_monitor or local_ai_monitor.startswith(sys.executable):
        # already a shell-ish string — split carefully only if we used the fallback form
        parts = shlex.split(local_ai_monitor)
    else:
        parts = [local_ai_monitor]
    parts.append("tui")
    if interval is not None:
        parts.extend(["--interval", str(interval)])
    if view:
        parts.extend(["--view", view])
    if threads:
        parts.append("--threads")
    if extra:
        parts.extend(list(extra))
    return parts


def shell_join(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def session_exists(tmux_bin: str, name: str) -> bool:
    r = subprocess.run(
        [tmux_bin, "has-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def build_dash_plan(
    *,
    session: str = "local-ai-monitor",
    glances_bin: Optional[str] = None,
    tmux_bin: Optional[str] = None,
    local_ai_monitor: Optional[str] = None,
    glances_args: Optional[Sequence[str]] = None,
    interval: Optional[float] = None,
    view: Optional[str] = None,
    threads: bool = False,
    glances_left: bool = True,
) -> Dict[str, Any]:
    """Pure-ish plan for tests: commands that would be run."""
    tbin = tmux_bin or find_tmux() or "tmux"
    gbin = glances_bin or find_glances() or "glances"
    atop = local_ai_monitor or find_local_ai_monitor()
    gcmd = glances_command(gbin, glances_args)
    tcmd = tui_command(atop, interval=interval, view=view, threads=threads)
    return {
        "tmux": tbin,
        "session": session,
        "glances_argv": gcmd,
        "tui_argv": tcmd,
        "glances_shell": shell_join(gcmd),
        "tui_shell": shell_join(tcmd),
        "glances_left": glances_left,
    }


def run_dash(
    *,
    session: Optional[str] = None,
    interval: Optional[float] = None,
    view: Optional[str] = None,
    threads: bool = False,
    attach: bool = True,
    recreate: bool = False,
) -> int:
    """Create (or attach) the local-ai-monitor tmux session. Returns process exit code."""
    cfg = load_config()
    mux = cfg.get("mux") or {}
    session = session or str(mux.get("tmux_session") or "local-ai-monitor")
    glances_args = mux.get("glances_args")
    if not isinstance(glances_args, list) or not glances_args:
        glances_args = ["-t", "2", "--disable-plugin", "docker"]
    glances_left = bool(mux.get("glances_left", True))

    ok, tmux_bin, glances_bin = deps_available()
    if not ok or not tmux_bin or not glances_bin:
        missing = []
        if not tmux_bin:
            missing.append("tmux")
        if not glances_bin:
            missing.append("glances")
        print(
            f"local-ai-monitor dash: missing {', '.join(missing)}; falling back to tui. "
            f"Install tmux and glances, then retry",
            file=sys.stderr,
        )
        from local_ai_monitor.tui import live

        return live(
            interval if interval is not None else 2.0,
            once=False,
            as_json=False,
            view=view or "session",
            threads=bool(threads),
        )

    plan = build_dash_plan(
        session=session,
        glances_bin=glances_bin,
        tmux_bin=tmux_bin,
        glances_args=glances_args,
        interval=interval,
        view=view,
        threads=threads,
        glances_left=glances_left,
    )

    if recreate and session_exists(tmux_bin, session):
        subprocess.run(
            [tmux_bin, "kill-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not session_exists(tmux_bin, session):
        # Pane 0: glances (left when split -h puts new pane on right)
        r = subprocess.run(
            [
                tmux_bin,
                "new-session",
                "-d",
                "-s",
                session,
                "-n",
                "main",
                plan["glances_shell"],
            ]
        )
        if r.returncode != 0:
            print("error: tmux new-session failed", file=sys.stderr)
            return r.returncode or 1
        # Pane 1: AI tui on the right
        r = subprocess.run(
            [
                tmux_bin,
                "split-window",
                "-h",
                "-t",
                f"{session}:main",
                plan["tui_shell"],
            ]
        )
        if r.returncode != 0:
            print("error: tmux split-window failed", file=sys.stderr)
            return r.returncode or 1
        # Optional: even split
        subprocess.run(
            [tmux_bin, "select-layout", "-t", f"{session}:main", "even-horizontal"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Focus AI pane (right) for keyboard
        subprocess.run(
            [tmux_bin, "select-pane", "-t", f"{session}:main.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not attach:
        return 0

    # Attach or switch
    if os.environ.get("TMUX"):
        r = subprocess.run([tmux_bin, "switch-client", "-t", session])
        return r.returncode
    # Replace this process with attach so exit cleans naturally
    os.execvp(tmux_bin, [tmux_bin, "attach-session", "-t", session])
    return 1  # unreachable
