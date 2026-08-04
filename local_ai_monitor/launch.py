"""Launch new AI tool sessions (Terminal / desktop apps).

Used by CLI `local-ai-monitor launch` and menubar New (Swift mirrors recipes).
Prefs: ~/.config/local-ai-monitor/launch.json  { "last_cwd": { "Codex": "/path" } }
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, Optional

from local_ai_monitor.config import CONFIG_DIR

LAUNCH_PREFS = os.path.join(CONFIG_DIR, "launch.json")
DEFAULT_CWD = os.path.expanduser("~/Projects")

# tool_id → launch recipe
LAUNCH_RECIPES: Dict[str, Dict[str, str]] = {
    "Grok": {"kind": "terminal", "command": "grok"},
    "Claude CLI": {"kind": "terminal", "command": "claude"},
    "Codex": {"kind": "terminal", "command": "codex"},
    "OpenAI CLI": {"kind": "terminal", "command": "openai"},
    "OpenClaw": {"kind": "terminal", "command": "openclaw"},
    "Claude Desktop": {
        "kind": "app",
        "bundle": "com.anthropic.claudefordesktop",
        "path": "/Applications/Claude.app",
    },
    "ChatGPT": {
        "kind": "app",
        "bundle": "com.openai.chat",
        "path": "/Applications/ChatGPT.app",
    },
    "Buzz": {
        "kind": "app",
        "bundle": "xyz.block.buzz.app",
        "path": "/Applications/Buzz.app",
    },
    "Cursor": {
        "kind": "app_folder",
        "bundle": "com.todesktop.230313mzl4w4u92",
        "path": "/Applications/Cursor.app",
    },
}


def load_launch_prefs() -> Dict[str, str]:
    if not os.path.isfile(LAUNCH_PREFS):
        return {}
    try:
        with open(LAUNCH_PREFS, "r", encoding="utf-8") as f:
            raw = json.load(f)
        m = raw.get("last_cwd") or {}
        return {str(k): str(v) for k, v in m.items()} if isinstance(m, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_last_cwd(tool_id: str, cwd: str) -> None:
    prefs = load_launch_prefs()
    prefs[tool_id] = cwd
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    with open(LAUNCH_PREFS, "w", encoding="utf-8") as f:
        json.dump({"last_cwd": prefs}, f, indent=2)
        f.write("\n")


def resolve_cwd(tool_id: str, cwd: Optional[str] = None) -> str:
    if cwd and os.path.isdir(os.path.expanduser(cwd)):
        return os.path.expanduser(cwd)
    prefs = load_launch_prefs()
    if tool_id in prefs and os.path.isdir(prefs[tool_id]):
        return prefs[tool_id]
    if os.path.isdir(DEFAULT_CWD):
        return DEFAULT_CWD
    return os.path.expanduser("~")


def _open_app(bundle: str, path: str) -> None:
    # open -b bundleId or path
    r = subprocess.run(
        ["open", "-b", bundle],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 and os.path.isdir(path):
        subprocess.run(["open", path], check=False)


def _resolve_bin(name: str) -> str:
    home = os.path.expanduser("~")
    for p in (
        os.path.join(home, ".local", "bin", name),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return name


def _is_conversation_uuid(raw: Optional[str]) -> bool:
    if not raw or raw.startswith("pid:"):
        return False
    parts = raw.split("-")
    if len(parts) != 5:
        return False
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(c in "0123456789abcdefABCDEF-" for c in raw)


def _shell_quote(s: str) -> str:
    if not s:
        return "''"
    if all(c.isalnum() or c in "/._-" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _run_in_terminal(command: str, cwd: str) -> None:
    """Open Terminal via a temp .command (no Automation TCC), AppleScript fallback."""
    import tempfile
    import uuid as _uuid

    body = (
        "#!/bin/zsh\n"
        'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"\n'
        f"cd {_shell_quote(cwd)} || exit 1\n"
        f"exec {command}\n"
    )
    launch_dir = os.path.join(tempfile.gettempdir(), "local-ai-monitor-launch")
    os.makedirs(launch_dir, mode=0o700, exist_ok=True)
    path = os.path.join(launch_dir, f"run-{_uuid.uuid4().hex[:8]}.command")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o700)
        r = subprocess.run(
            ["/usr/bin/open", "-a", "Terminal", path],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return
    except OSError:
        pass
    # AppleScript fallback
    esc_dir = cwd.replace("\\", "\\\\").replace('"', '\\"')
    esc_cmd = command.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Terminal"\n'
        "  activate\n"
        f'  do script "cd \\"{esc_dir}\\" && {esc_cmd}"\n'
        "end tell"
    )
    subprocess.run(["osascript", "-e", script], check=False)


def launch_tool(
    tool_id: str,
    cwd: Optional[str] = None,
    *,
    resume: bool = False,
    session_id: Optional[str] = None,
) -> str:
    """Launch tool; return short human status.

    resume=True: Claude CLI uses ``--resume <uuid>`` or ``--continue``.
    """
    recipe = LAUNCH_RECIPES.get(tool_id)
    if not recipe:
        return f"No launch recipe for {tool_id!r}"
    if not resume:
        try:
            from local_ai_monitor.resource.policy import evaluate

            d = evaluate()
            fc = {}
            if isinstance(d.physics, dict) and isinstance(d.physics.get("forecast"), dict):
                fc = d.physics["forecast"]
            if fc.get("can_start_heavy") is False:
                return (
                    f"Start gate closed — not launching {tool_id}. "
                    f"{fc.get('state')}: {fc.get('recommendation')}. "
                    "Checkpoint or close idle load first."
                )
        except Exception:
            # Unknown policy must not hide a user-requested CLI action forever;
            # launch remains explicit/manual.
            pass
    directory = resolve_cwd(tool_id, cwd)
    save_last_cwd(tool_id, directory)
    kind = recipe.get("kind")
    if kind == "terminal":
        bin_name = recipe["command"]
        bin_path = _resolve_bin(bin_name)
        if tool_id == "Claude CLI" and resume:
            if _is_conversation_uuid(session_id):
                cmd = f"{_shell_quote(bin_path)} --resume {_shell_quote(session_id or '')}"
                label = "Anthropic CLI (resume)"
            else:
                cmd = f"{_shell_quote(bin_path)} --continue"
                label = "Anthropic CLI (continue)"
        elif tool_id == "Codex" and resume:
            # codex resume [SESSION_ID] | codex resume --last
            if _is_conversation_uuid(session_id):
                cmd = f"{_shell_quote(bin_path)} resume {_shell_quote(session_id or '')}"
                label = "Codex (resume)"
            else:
                cmd = f"{_shell_quote(bin_path)} resume --last"
                label = "Codex (resume last)"
        else:
            cmd = _shell_quote(bin_path)
            label = tool_id
        _run_in_terminal(cmd, directory)
        base = os.path.basename(directory.rstrip("/")) or directory
        return f"Started {label} in {base}"
    if kind == "app":
        _open_app(recipe.get("bundle", ""), recipe.get("path", ""))
        return f"Opened {tool_id}"
    if kind == "app_folder":
        # Cursor: open workspace folder then app (recipe path is Cursor.app)
        if os.path.isdir(directory):
            subprocess.run(
                ["open", "-a", recipe.get("path") or "Cursor", directory],
                check=False,
            )
        else:
            _open_app(recipe.get("bundle", ""), recipe.get("path", ""))
        return f"Opened {tool_id}" + (
            f" · {os.path.basename(directory.rstrip('/'))}" if os.path.isdir(directory) else ""
        )
    return f"Unknown launch kind for {tool_id}"


def cmd_launch(argv: Optional[list] = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(prog="local-ai-monitor launch", description="Start a new AI tool session")
    p.add_argument("tool", nargs="?", help="Tool id e.g. Grok, Codex, 'Claude CLI'")
    p.add_argument("--cwd", "-C", default=None, help="Working directory")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Continue prior conversation (Claude: --resume/--continue; Codex: resume [--last])",
    )
    p.add_argument(
        "--session-id",
        default=None,
        help="Conversation UUID for Claude --resume or Codex resume",
    )
    p.add_argument("--list", action="store_true", help="List launch recipes")
    args = p.parse_args(list(argv or []))
    if args.list or not args.tool:
        for k, v in LAUNCH_RECIPES.items():
            print(f"  {k:<16}  {v.get('kind')}  {v.get('command') or v.get('path')}")
        if not args.list and not args.tool:
            print("\nusage: local-ai-monitor launch <tool> [--cwd DIR]", file=sys.stderr)
            return 0 if args.list else 2
        return 0
    # fuzzy resolve tool id
    tid = args.tool
    if tid not in LAUNCH_RECIPES:
        key = tid.casefold()
        for k in LAUNCH_RECIPES:
            if key in k.casefold() or key == k.casefold():
                tid = k
                break
        else:
            print(f"unknown tool: {args.tool!r}", file=sys.stderr)
            print("known:", ", ".join(LAUNCH_RECIPES), file=sys.stderr)
            return 2
    msg = launch_tool(
        tid, cwd=args.cwd, resume=bool(args.resume), session_id=args.session_id
    )
    print(msg)
    return 0
