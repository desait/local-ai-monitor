"""Package + LaunchAgent install/uninstall.

local-ai-monitor install [--collector-only] [--no-menubar] [--no-bootstrap] [--no-package]
local-ai-monitor uninstall [--purge]

Installs the collector LaunchAgent, menu bar app, and resource notifier.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from local_ai_monitor.config import ensure_state_dir, load_config, state_dir, write_default_config

COLLECT_LABEL = "com.user.local-ai-monitor.collect"
MENUBAR_LABEL = "com.user.local-ai-monitor.menubar"
RESOURCE_LABEL = "com.user.local-ai-monitor.resource"
CCM_LABEL = "com.user.local-ai-monitor.ccm-observer"

DEFAULT_SOURCE = ""
LIB_DIR = os.path.expanduser("~/.local/lib/local-ai-monitor")
BIN_DIR = os.path.expanduser("~/.local/bin")
LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
MENUBAR_APP_NAME = "Local AI Monitor Menu.app"
MENUBAR_EXEC_REL = os.path.join(
    "menubar", "dist", MENUBAR_APP_NAME, "Contents", "MacOS", "local-ai-monitor-menubar"
)


def resolve_python3() -> str:
    """Absolute python path for LaunchAgent."""
    candidates = [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ]
    which = shutil.which("python3")
    if which:
        candidates.append(which)
    candidates.append("/usr/bin/python3")
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            try:
                return os.path.realpath(c)
            except OSError:
                return c
    return "/usr/bin/python3"


def resolve_source() -> str:
    env = os.environ.get("LOCAL_AI_MONITOR_SRC")
    if env and os.path.isdir(os.path.join(env, "local_ai_monitor")):
        return os.path.abspath(env)
    if DEFAULT_SOURCE and os.path.isdir(os.path.join(DEFAULT_SOURCE, "local_ai_monitor")):
        return DEFAULT_SOURCE
    # Dev: package living next to this module's parent tree
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if os.path.isdir(os.path.join(here, "local_ai_monitor")):
        return here
    raise FileNotFoundError(
        "local-ai-monitor source not found (set LOCAL_AI_MONITOR_SRC)"
    )


def template_path(source: Optional[str] = None) -> str:
    src = source or resolve_source()
    p = os.path.join(src, "share", "com.user.local-ai-monitor.collect.plist.template")
    if os.path.isfile(p):
        return p
    # Installed copy
    p2 = os.path.join(LIB_DIR, "share", "com.user.local-ai-monitor.collect.plist.template")
    if os.path.isfile(p2):
        return p2
    raise FileNotFoundError("collect plist template not found under share/")


def render_collect_plist(
    *,
    python: str,
    pythonpath: str,
    state: str,
    throttle: int = 10,
    template: Optional[str] = None,
) -> str:
    """Render plist XML with absolute paths. Never embeds another user's home via relative."""
    path = template or template_path()
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__PYTHON__", python)
    text = text.replace("__PYTHONPATH__", pythonpath)
    text = text.replace("__STATE_DIR__", state)
    text = text.replace("__THROTTLE__", str(int(throttle)))
    text = text.replace("__HOME__", os.path.expanduser("~"))
    return text


def parse_plist_bytes(xml: str) -> Dict[str, Any]:
    return plistlib.loads(xml.encode("utf-8"))


def validate_collect_plist(data: Dict[str, Any]) -> List[str]:
    """Return list of contract errors (empty = OK)."""
    errs: List[str] = []
    if data.get("Label") != COLLECT_LABEL:
        errs.append(f"Label != {COLLECT_LABEL}")
    args = data.get("ProgramArguments") or []
    if not isinstance(args, list) or len(args) < 4:
        errs.append("ProgramArguments missing")
        return errs
    py = args[0]
    if not os.path.isabs(str(py)):
        errs.append(f"python not absolute: {py}")
    if "env" in str(py) or str(py).endswith("#!/usr/bin/env"):
        errs.append("must not use env python in LaunchAgent")
    if args[1:4] != ["-m", "local_ai_monitor", "collect"]:
        errs.append(f"ProgramArguments tail wrong: {args[1:4]}")
    env = data.get("EnvironmentVariables") or {}
    if env.get("LC_ALL") != "C":
        errs.append("LC_ALL != C")
    if not env.get("PYTHONPATH"):
        errs.append("PYTHONPATH missing")
    if not data.get("KeepAlive"):
        errs.append("KeepAlive not true")
    if not data.get("RunAtLoad"):
        errs.append("RunAtLoad not true")
    thr = data.get("ThrottleInterval")
    if thr is None or int(thr) < 5:
        errs.append(f"ThrottleInterval too small: {thr}")
    return errs


def install_package(source: Optional[str] = None) -> Tuple[str, str, str]:
    """rsync local_ai_monitor + share → ~/.local/lib/local-ai-monitor; write launcher. Returns (lib, bin, python)."""
    src = source or resolve_source()
    python = resolve_python3()
    lib = LIB_DIR
    bin_dir = BIN_DIR
    os.makedirs(lib, mode=0o755, exist_ok=True)
    os.makedirs(bin_dir, mode=0o755, exist_ok=True)

    pkg_src = os.path.join(src, "local_ai_monitor")
    pkg_dst = os.path.join(lib, "local_ai_monitor")
    if shutil.which("rsync"):
        subprocess.check_call(
            [
                "rsync",
                "-a",
                "--delete",
                "--exclude",
                "__pycache__",
                "--exclude",
                "*.pyc",
                pkg_src + "/",
                pkg_dst + "/",
            ]
        )
    else:
        if os.path.isdir(pkg_dst):
            shutil.rmtree(pkg_dst)
        shutil.copytree(
            pkg_src,
            pkg_dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    share_src = os.path.join(src, "share")
    share_dst = os.path.join(lib, "share")
    if os.path.isdir(share_src):
        if shutil.which("rsync"):
            subprocess.check_call(
                ["rsync", "-a", "--delete", share_src + "/", share_dst + "/"]
            )
        else:
            if os.path.isdir(share_dst):
                shutil.rmtree(share_dst)
            shutil.copytree(share_src, share_dst)

    launcher = os.path.join(bin_dir, "local-ai-monitor")

    content = f"""#!/usr/bin/env bash
# local-ai-monitor launcher generated by the installer.
set -euo pipefail
SOURCE={_shell_single(src)}
LIB={_shell_single(lib)}
PYTHON={_shell_single(python)}

if [[ "${{LOCAL_AI_MONITOR_DEV:-0}}" == "1" ]]; then
  export PYTHONPATH="${{SOURCE}}${{PYTHONPATH:+:$PYTHONPATH}}"
else
  export PYTHONPATH="${{LIB}}${{PYTHONPATH:+:$PYTHONPATH}}"
fi
exec "$PYTHON" -m local_ai_monitor "$@"
"""
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(launcher, 0o755)

    return lib, launcher, python


def _shell_single(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def write_collect_agent_plist(
    *,
    python: Optional[str] = None,
    pythonpath: Optional[str] = None,
    state: Optional[str] = None,
    throttle: Optional[int] = None,
    dest: Optional[str] = None,
) -> str:
    """Render and write LaunchAgent plist. Returns path written."""
    py = python or resolve_python3()
    pp = pythonpath or LIB_DIR
    st = state or ensure_state_dir()
    if throttle is None:
        cfg = load_config()
        throttle = int(cfg.get("sample_interval_s") or 10)
        throttle = max(10, throttle)  # ≥ sample interval
    xml = render_collect_plist(
        python=py, pythonpath=pp, state=st, throttle=int(throttle)
    )
    data = parse_plist_bytes(xml)
    errs = validate_collect_plist(data)
    if errs:
        raise ValueError("plist contract failed: " + "; ".join(errs))

    os.makedirs(LAUNCH_AGENTS, mode=0o755, exist_ok=True)
    out = dest or os.path.join(LAUNCH_AGENTS, f"{COLLECT_LABEL}.plist")
    with open(out, "wb") as f:
        plistlib.dump(data, f)
    try:
        os.chmod(out, 0o644)
    except OSError:
        pass
    # Also keep a rendered copy under lib/share for inspection
    try:
        share = os.path.join(LIB_DIR, "share")
        os.makedirs(share, exist_ok=True)
        with open(os.path.join(share, f"{COLLECT_LABEL}.plist"), "wb") as f:
            plistlib.dump(data, f)
    except OSError:
        pass
    return out


def uid_domain() -> str:
    return f"gui/{os.getuid()}"


def bootstrap_collect(plist_path: Optional[str] = None) -> Tuple[int, str]:
    """launchctl bootout (ignore fail) + bootstrap. Returns (rc, message)."""
    path = plist_path or os.path.join(LAUNCH_AGENTS, f"{COLLECT_LABEL}.plist")
    domain = uid_domain()
    # bootout if loaded
    subprocess.run(
        ["launchctl", "bootout", domain, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # also try label form
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{COLLECT_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", domain, path],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        # try enable + kickstart older style fallback
        subprocess.run(
            ["launchctl", "enable", f"{domain}/{COLLECT_LABEL}"],
            capture_output=True,
            text=True,
        )
        r2 = subprocess.run(
            ["launchctl", "bootstrap", domain, path],
            capture_output=True,
            text=True,
        )
        if r2.returncode != 0:
            msg = (r2.stderr or r.stderr or r2.stdout or r.stdout or "").strip()
            return r2.returncode, msg or "bootstrap failed"
    # kickstart to run now
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{COLLECT_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0, "bootstrapped"


def bootout_collect() -> Tuple[int, str]:
    domain = uid_domain()
    path = os.path.join(LAUNCH_AGENTS, f"{COLLECT_LABEL}.plist")
    r = subprocess.run(
        ["launchctl", "bootout", domain, path],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{COLLECT_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0 and "No such" not in (r.stderr or "") and r.returncode not in (3, 5, 125):
        # 3/5 often not loaded — treat soft
        pass
    return 0, "booted out"


def launchctl_print_collect() -> Optional[str]:
    domain = uid_domain()
    r = subprocess.run(
        ["launchctl", "print", f"{domain}/{COLLECT_LABEL}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def agent_python_from_plist(path: Optional[str] = None) -> Optional[str]:
    p = path or os.path.join(LAUNCH_AGENTS, f"{COLLECT_LABEL}.plist")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "rb") as f:
            data = plistlib.load(f)
        args = data.get("ProgramArguments") or []
        return str(args[0]) if args else None
    except Exception:
        return None


def menubar_exec_path(lib: Optional[str] = None) -> str:
    return os.path.join(lib or LIB_DIR, MENUBAR_EXEC_REL)


def menubar_app_path(lib: Optional[str] = None) -> str:
    return os.path.join(lib or LIB_DIR, "menubar", "dist", MENUBAR_APP_NAME)


def menubar_template_path(source: Optional[str] = None) -> str:
    src = source or resolve_source()
    p = os.path.join(src, "share", "com.user.local-ai-monitor.menubar.plist.template")
    if os.path.isfile(p):
        return p
    p2 = os.path.join(LIB_DIR, "share", "com.user.local-ai-monitor.menubar.plist.template")
    if os.path.isfile(p2):
        return p2
    raise FileNotFoundError("menubar plist template not found under share/")


def render_menubar_plist(
    *,
    exec_path: str,
    state: str,
    home: Optional[str] = None,
    template: Optional[str] = None,
) -> str:
    path = template or menubar_template_path()
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__MENUBAR_EXEC__", exec_path)
    text = text.replace("__STATE_DIR__", state)
    text = text.replace("__HOME__", home or os.path.expanduser("~"))
    return text


def validate_menubar_plist(data: Dict[str, Any], expect_exec: Optional[str] = None) -> List[str]:
    errs: List[str] = []
    if data.get("Label") != MENUBAR_LABEL:
        errs.append(f"Label != {MENUBAR_LABEL}")
    args = data.get("ProgramArguments") or []
    if not args:
        errs.append("ProgramArguments empty")
        return errs
    exe = str(args[0])
    if not os.path.isabs(exe):
        errs.append(f"menubar exec not absolute: {exe}")
    if expect_exec and exe != expect_exec:
        errs.append(f"exec mismatch: {exe}")
    if not data.get("KeepAlive"):
        errs.append("KeepAlive not true")
    if not data.get("RunAtLoad"):
        errs.append("RunAtLoad not true")
    return errs


def build_menubar_app(source: Optional[str] = None) -> Tuple[int, str, str]:
    """Run menubar/scripts/build.sh; rsync app into LIB/menubar/dist.

    Returns (rc, app_path, message).
    """
    src = source or resolve_source()
    build_sh = os.path.join(src, "menubar", "scripts", "build.sh")
    if not os.path.isfile(build_sh):
        return 1, "", f"missing build script: {build_sh}"
    # Build into source menubar/dist first
    env = os.environ.copy()
    r = subprocess.run(
        ["bash", build_sh],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.join(src, "menubar"),
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "swift build failed").strip()
        return r.returncode, "", msg

    src_app = os.path.join(src, "menubar", "dist", MENUBAR_APP_NAME)
    if not os.path.isdir(src_app):
        return 1, "", "build produced no .app bundle"

    dest_root = os.path.join(LIB_DIR, "menubar", "dist")
    os.makedirs(dest_root, mode=0o755, exist_ok=True)
    dest_app = os.path.join(dest_root, MENUBAR_APP_NAME)
    if os.path.isdir(dest_app):
        shutil.rmtree(dest_app)
    if shutil.which("rsync"):
        subprocess.check_call(["rsync", "-a", src_app + "/", dest_app + "/"])
    else:
        shutil.copytree(src_app, dest_app)

    exe = os.path.join(dest_app, "Contents", "MacOS", "local-ai-monitor-menubar")
    if not os.path.isfile(exe):
        return 1, dest_app, "executable missing in installed app"
    try:
        os.chmod(exe, 0o755)
        cmd = os.path.join(dest_app, "Contents", "Resources", "open-dash.command")
        if os.path.isfile(cmd):
            os.chmod(cmd, 0o755)
    except OSError:
        pass
    return 0, dest_app, "built"


def write_menubar_agent_plist(
    *,
    exec_path: Optional[str] = None,
    state: Optional[str] = None,
    dest: Optional[str] = None,
) -> str:
    exe = exec_path or menubar_exec_path()
    st = state or ensure_state_dir()
    xml = render_menubar_plist(exec_path=exe, state=st)
    data = parse_plist_bytes(xml)
    errs = validate_menubar_plist(data, expect_exec=exe)
    if errs:
        raise ValueError("menubar plist contract failed: " + "; ".join(errs))
    os.makedirs(LAUNCH_AGENTS, mode=0o755, exist_ok=True)
    out = dest or os.path.join(LAUNCH_AGENTS, f"{MENUBAR_LABEL}.plist")
    with open(out, "wb") as f:
        plistlib.dump(data, f)
    try:
        share = os.path.join(LIB_DIR, "share")
        os.makedirs(share, exist_ok=True)
        with open(os.path.join(share, f"{MENUBAR_LABEL}.plist"), "wb") as f:
            plistlib.dump(data, f)
    except OSError:
        pass
    return out


def bootstrap_menubar(plist_path: Optional[str] = None) -> Tuple[int, str]:
    path = plist_path or os.path.join(LAUNCH_AGENTS, f"{MENUBAR_LABEL}.plist")
    domain = uid_domain()
    subprocess.run(
        ["launchctl", "bootout", domain, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{MENUBAR_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", domain, path],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        return r.returncode, msg or "menubar bootstrap failed"
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{MENUBAR_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0, "bootstrapped"


def bootout_menubar() -> Tuple[int, str]:
    domain = uid_domain()
    path = os.path.join(LAUNCH_AGENTS, f"{MENUBAR_LABEL}.plist")
    subprocess.run(
        ["launchctl", "bootout", domain, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{MENUBAR_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0, "booted out"


def launchctl_print_menubar() -> Optional[str]:
    domain = uid_domain()
    r = subprocess.run(
        ["launchctl", "print", f"{domain}/{MENUBAR_LABEL}"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def menubar_pid() -> Optional[int]:
    """Best-effort PID of running menubar executable."""
    printed = launchctl_print_menubar()
    if printed:
        for line in printed.splitlines():
            line = line.strip()
            if line.startswith("pid = "):
                raw = line.split("=", 1)[1].strip()
                if raw.isdigit():
                    return int(raw)
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "local-ai-monitor-menubar"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def format_menubar_title(cpu_pct: float, rss_kb: int) -> str:
    """Match Swift LiveModel.formatTitle for tests."""
    cpu_part = f"AI {int(round(cpu_pct))}%"
    rss_mb = rss_kb / 1024.0
    if rss_mb >= 1024.0:
        return f"{cpu_part} · {rss_mb / 1024.0:.1f}G"
    return f"{cpu_part} · {rss_mb:.0f}M"


def resource_template_path(source: Optional[str] = None) -> str:
    src = source or resolve_source()
    p = os.path.join(src, "share", "com.user.local-ai-monitor.resource.plist.template")
    if os.path.isfile(p):
        return p
    p2 = os.path.join(LIB_DIR, "share", "com.user.local-ai-monitor.resource.plist.template")
    if os.path.isfile(p2):
        return p2
    raise FileNotFoundError("resource plist template not found under share/")


def render_resource_plist(
    *,
    python: str,
    pythonpath: str,
    state: str,
    interval: int = 60,
    template: Optional[str] = None,
) -> str:
    path = template or resource_template_path()
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__PYTHON__", python)
    text = text.replace("__PYTHONPATH__", pythonpath)
    text = text.replace("__STATE_DIR__", state)
    text = text.replace("__INTERVAL__", str(int(interval)))
    text = text.replace("__HOME__", os.path.expanduser("~"))
    return text


def validate_resource_plist(data: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    if data.get("Label") != RESOURCE_LABEL:
        errs.append(f"Label != {RESOURCE_LABEL}")
    args = data.get("ProgramArguments") or []
    if not isinstance(args, list) or len(args) < 5:
        errs.append("ProgramArguments missing")
        return errs
    py = args[0]
    if not os.path.isabs(str(py)):
        errs.append(f"python not absolute: {py}")
    if args[1:5] != ["-m", "local_ai_monitor", "resource", "notify"]:
        errs.append(f"ProgramArguments wrong: {args[1:5]}")
    if "--once" not in args:
        errs.append("must use notify --once (no KeepAlive kill loop)")
    if data.get("KeepAlive"):
        errs.append("resource agent must not KeepAlive (StartInterval only)")
    return errs


def write_resource_agent_plist(
    *,
    python: Optional[str] = None,
    pythonpath: Optional[str] = None,
    state: Optional[str] = None,
    interval: int = 60,
    dest: Optional[str] = None,
) -> str:
    py = python or resolve_python3()
    pp = pythonpath or LIB_DIR
    st = state or ensure_state_dir()
    xml = render_resource_plist(
        python=py, pythonpath=pp, state=st, interval=int(interval)
    )
    data = parse_plist_bytes(xml)
    errs = validate_resource_plist(data)
    if errs:
        raise ValueError("resource plist contract failed: " + "; ".join(errs))
    os.makedirs(LAUNCH_AGENTS, mode=0o755, exist_ok=True)
    out = dest or os.path.join(LAUNCH_AGENTS, f"{RESOURCE_LABEL}.plist")
    with open(out, "wb") as f:
        plistlib.dump(data, f)
    try:
        os.chmod(out, 0o644)
    except OSError:
        pass
    return out


def bootstrap_resource(plist_path: Optional[str] = None) -> Tuple[int, str]:
    path = plist_path or os.path.join(LAUNCH_AGENTS, f"{RESOURCE_LABEL}.plist")
    domain = uid_domain()
    subprocess.run(
        ["launchctl", "bootout", domain, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{RESOURCE_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", domain, path],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        return r.returncode, msg or "bootstrap failed"
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{RESOURCE_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0, "bootstrapped"


def bootout_resource() -> Tuple[int, str]:
    domain = uid_domain()
    path = os.path.join(LAUNCH_AGENTS, f"{RESOURCE_LABEL}.plist")
    subprocess.run(
        ["launchctl", "bootout", domain, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{RESOURCE_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0, "booted out"


def bootout_ccm_observer() -> Tuple[int, str]:
    domain = uid_domain()
    path = os.path.join(LAUNCH_AGENTS, f"{CCM_LABEL}.plist")
    subprocess.run(
        ["launchctl", "bootout", domain, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{CCM_LABEL}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0, "booted out"


def cmd_install(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-monitor install",
        description="Install package + collector + menubar LaunchAgents",
    )
    p.add_argument(
        "--collector-only",
        action="store_true",
        help="install collector agent only (skip menubar)",
    )
    p.add_argument(
        "--no-menubar",
        action="store_true",
        help="skip MenuBarExtra .app and menubar agent",
    )
    p.add_argument(
        "--no-resource",
        action="store_true",
        help="skip local-ai-rm notify-only LaunchAgent",
    )
    p.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="write plists but do not launchctl bootstrap",
    )
    p.add_argument(
        "--no-package",
        action="store_true",
        help="skip rsync package (agents only)",
    )
    p.add_argument("--source", default=None, help="override source tree")
    p.add_argument(
        "--throttle",
        type=int,
        default=None,
        help="collector ThrottleInterval seconds",
    )
    args = p.parse_args(argv)

    write_default_config()
    ensure_state_dir()
    try:
        from local_ai_monitor.resource.config import write_default_resource_config

        write_default_resource_config()
    except Exception:
        pass
    want_menubar = not (args.collector_only or args.no_menubar)
    want_resource = not (args.collector_only or args.no_resource)

    python = resolve_python3()
    lib = LIB_DIR
    if not args.no_package:
        try:
            lib, launcher, python = install_package(args.source)
            print(f"package:  {lib}/local_ai_monitor")
            print(f"launcher: {launcher}")
            print(f"python:   {python}")
        except Exception as exc:
            print(f"error: package install failed: {exc}", file=sys.stderr)
            return 1
    else:
        print(f"python:   {python} (no-package)")

    # --- collector ---
    try:
        plist_path = write_collect_agent_plist(
            python=python, pythonpath=lib, throttle=args.throttle
        )
        print(f"plist:    {plist_path}")
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        errs = validate_collect_plist(data)
        if errs:
            print("error: plist validation: " + "; ".join(errs), file=sys.stderr)
            return 1
        print(f"  ProgramArguments[0]={data['ProgramArguments'][0]}")
        print(f"  ThrottleInterval={data.get('ThrottleInterval')}")
    except Exception as exc:
        print(f"error: write collect plist failed: {exc}", file=sys.stderr)
        return 1

    if not args.no_bootstrap:
        rc, msg = bootstrap_collect(plist_path)
        if rc != 0:
            print(f"error: launchctl bootstrap collect: {msg}", file=sys.stderr)
            return 1
        print(f"launchd:  {COLLECT_LABEL} {msg}")
    else:
        print("launchd:  collect skipped (--no-bootstrap)")

    # --- menubar ---
    if not want_menubar:
        print("menubar:  skipped (--collector-only / --no-menubar)")
        print("done. check: local-ai-monitor status")
        return 0

    rc, app_path, msg = build_menubar_app(args.source)
    if rc != 0:
        print(f"warn: menubar Swift build failed: {msg}", file=sys.stderr)
        print(
            "menubar:  skipped; background monitor installed. "
            "Monitor package installer should provision Swift/toolchain prerequisites.",
            file=sys.stderr,
        )
        print("done. check: local-ai-monitor status")
        return 0

    print(f"menubar:  {app_path}")
    exe = menubar_exec_path(lib)
    print(f"  exec:   {exe}")
    try:
        mb_plist = write_menubar_agent_plist(exec_path=exe)
        print(f"plist:    {mb_plist}")
    except Exception as exc:
        print(f"error: write menubar plist failed: {exc}", file=sys.stderr)
        return 1

    if not args.no_bootstrap:
        rc, msg = bootstrap_menubar(mb_plist)
        if rc != 0:
            print(f"warn: launchctl bootstrap menubar: {msg}", file=sys.stderr)
            print("      try opening the .app once, then re-run install")
        else:
            print(f"launchd:  {MENUBAR_LABEL} {msg}")
    else:
        print("launchd:  menubar skipped (--no-bootstrap)")

    # Background session log (CCM engine optional; not shown in menu bar)
    try:
        from local_ai_monitor.ccm_observer import install_ccm_launchagent, run_observer_once

        if not args.no_bootstrap:
            ccm_plist = install_ccm_launchagent()
            print(f"session_log: {ccm_plist}")
            try:
                run_observer_once()
                print("session_log: first pack written")
            except Exception as exc:
                print(f"warn: session log first run: {exc}", file=sys.stderr)
        else:
            print("session_log: skipped (--no-bootstrap)")
    except Exception as exc:
        print(f"warn: session log install: {exc}", file=sys.stderr)

    # Notify-only resource agent (local-ai-rm) — never auto-kills
    if want_resource:
        try:
            res_plist = write_resource_agent_plist(
                python=python, pythonpath=lib, interval=60
            )
            print(f"resource: {res_plist} (notify only, no auto-kill)")
            if not args.no_bootstrap:
                rc, msg = bootstrap_resource(res_plist)
                if rc != 0:
                    print(f"warn: launchctl bootstrap resource: {msg}", file=sys.stderr)
                else:
                    print(f"launchd:  {RESOURCE_LABEL} {msg}")
            else:
                print("launchd:  resource skipped (--no-bootstrap)")
        except Exception as exc:
            print(f"warn: resource notify agent: {exc}", file=sys.stderr)
    else:
        print("resource: skipped")

    print("done. check: local-ai-monitor status · open menu bar for headroom / reclaim-idle card")
    return 0


def cmd_uninstall(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-monitor uninstall",
        description="Remove collector + menubar LaunchAgents; optional --purge state",
    )
    p.add_argument(
        "--purge",
        action="store_true",
        help="also delete ~/.local/state/local-ai-monitor (live/accum/history)",
    )
    p.add_argument(
        "--keep-package",
        action="store_true",
        help="leave ~/.local/lib/local-ai-monitor and launcher",
    )
    args = p.parse_args(argv)

    bootout_collect()
    print(f"launchd:  {COLLECT_LABEL} removed (if loaded)")
    bootout_menubar()
    print(f"launchd:  {MENUBAR_LABEL} removed (if loaded)")
    bootout_resource()
    print(f"launchd:  {RESOURCE_LABEL} removed (if loaded)")
    bootout_ccm_observer()
    print(f"launchd:  {CCM_LABEL} removed (if loaded)")

    for label in (COLLECT_LABEL, MENUBAR_LABEL, RESOURCE_LABEL, CCM_LABEL):
        plist = os.path.join(LAUNCH_AGENTS, f"{label}.plist")
        if os.path.isfile(plist):
            try:
                os.remove(plist)
                print(f"removed:  {plist}")
            except OSError as exc:
                print(f"warn: could not remove {plist}: {exc}", file=sys.stderr)

    if args.purge:
        st = state_dir()
        if os.path.isdir(st):
            shutil.rmtree(st, ignore_errors=True)
            print(f"purged:   {st}")

    if not args.keep_package:
        print("package:  kept (launcher + lib remain)")

    return 0
