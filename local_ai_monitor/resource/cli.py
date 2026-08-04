"""CLI for local-ai-rm / local-ai-monitor resource.

  local-ai-rm status | suggest | dry-run | apply | preflight | config
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from local_ai_monitor.end_session import end_heaviest, end_session
from local_ai_monitor.resource.audit import append_audit
from local_ai_monitor.resource.config import (
    DEFAULT_RESOURCE,
    load_resource_config,
    resource_config_path,
    write_default_resource_config,
)
from local_ai_monitor.resource.policy import evaluate, preflight_ok


def _print_decision(d: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(d.to_dict(), indent=2))
        return
    print(f"band:     {d.band}")
    print(f"action:   {d.action}")
    print(f"reason:   {d.reason}")
    if d.free_pages is not None:
        mb = f" ({d.free_mb} MB)" if d.free_mb is not None else ""
        print(f"free:     {d.free_pages} pages{mb}")
    print(f"waterline warn/hard: {d.waterline_warn} / {d.waterline_hard} pages")
    print(f"AI sessions: {d.ai_session_count}  total RSS: {d.ai_rss_kb // 1024} MB")
    if d.live_ts:
        print(f"live.ts:  {d.live_ts}")
    if d.candidate:
        c = d.candidate
        print(
            f"candidate: {c.app}  session={c.session_id}  "
            f"rss={c.rss_kb // 1024} MB"
            + ("  [protected]" if c.protected else "")
        )
    if d.protected_skipped:
        print("protected skipped:", ", ".join(d.protected_skipped))
    if d.error:
        print(f"error:    {d.error}", file=sys.stderr)


def _apply_candidate(
    d: Any,
    *,
    dry_run: bool,
    state: Optional[str],
    yes: bool,
    force_heaviest: bool,
) -> Dict[str, Any]:
    """End policy candidate (or absolute heaviest if force). Never auto without --yes."""
    if not yes and not dry_run:
        return {
            "ok": False,
            "error": "refusing apply without --yes (L4: confirm before destroy)",
        }
    if force_heaviest or d.candidate is None:
        result = end_heaviest(state=state, dry_run=dry_run)
    else:
        result = end_session(
            d.candidate.app,
            d.candidate.session_id,
            state=state,
            dry_run=dry_run,
        )
    append_audit(
        "dry_run" if dry_run else "apply",
        {
            "band": d.band,
            "free_pages": d.free_pages,
            "app": result.get("app"),
            "session_id": result.get("session_id"),
            "pids": result.get("pids"),
            "ok": result.get("ok"),
            "error": result.get("error"),
            "dry_run": dry_run,
        },
        state=state,
    )
    return result


def cmd_resource(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or [])
    # Early dispatch: notify NEVER kills; manage may reclaim idle services only.
    # Incident 2026-07-30: notify was wired to manage_once and soft-stopped Grok.
    if argv and argv[0] == "notify":
        from local_ai_monitor.resource.notify import cmd_notify

        return cmd_notify(argv[1:] if len(argv) > 1 else ["--once"])
    if argv and argv[0] == "manage":
        from local_ai_monitor.resource.manage import cmd_manage

        return cmd_manage(argv[1:] if len(argv) > 1 else ["--once"])

    p = argparse.ArgumentParser(
        prog="local-ai-rm",
        description=(
            "Physics-first AI resource policy. "
            "Reads local-ai-monitor live.json + free pages; ends sessions only via end-session. "
            "Default never auto-kills."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  local-ai-rm status\n"
            "  local-ai-rm suggest\n"
            "  local-ai-rm dry-run\n"
            "  local-ai-rm apply --heaviest --yes\n"
            "  local-ai-rm preflight && xcodebuild ...\n"
            "  local-ai-rm notify --once   # LaunchAgent / banner only\n"
        ),
    )
    p.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=[
            "status",
            "suggest",
            "dry-run",
            "apply",
            "preflight",
            "config",
            "notify",
            "manage",
        ],
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--yes", action="store_true", help="Required for apply (confirm)")
    p.add_argument(
        "--heaviest",
        action="store_true",
        help="With apply/dry-run: use absolute heaviest (ignore protect skip ranking)",
    )
    p.add_argument("--tool", default=None, help="With apply: specific tool id")
    p.add_argument("--session", default=None, help="With apply: session_id")
    p.add_argument(
        "--require",
        choices=["ok", "warn"],
        default="ok",
        help="preflight: ok = above warn waterline; warn = above hard only",
    )
    p.add_argument(
        "--config-path",
        default=None,
        help="Override resource.json path",
    )
    p.add_argument(
        "--write-default-config",
        action="store_true",
        help="With config: write defaults if missing",
    )
    args = p.parse_args(argv)

    cfg = load_resource_config(args.config_path)
    state = None  # LOCAL_AI_MONITOR_STATE env honored inside store

    if args.command == "config":
        if args.write_default_config:
            path = write_default_resource_config(args.config_path)
            print(f"wrote defaults if missing: {path}")
        path = resource_config_path(args.config_path)
        if args.json:
            print(json.dumps({"path": path, "config": cfg}, indent=2))
        else:
            print(f"path: {path}")
            print(json.dumps(cfg, indent=2))
            print("\ndefaults (host-calibrated):")
            print(json.dumps(DEFAULT_RESOURCE, indent=2))
        return 0

    if args.command == "notify":
        from local_ai_monitor.resource.notify import cmd_notify

        return cmd_notify(["--once"])
    if args.command == "manage":
        from local_ai_monitor.resource.manage import cmd_manage

        return cmd_manage(["--once"])

    if args.command == "preflight":
        d = preflight_ok(state=state, config=cfg, require_band=args.require)
        _print_decision(d, as_json=args.json)
        append_audit(
            "preflight",
            {
                "ok": d.ok,
                "band": d.band,
                "free_pages": d.free_pages,
                "require": args.require,
            },
            state=state,
        )
        return 0 if d.ok else 1

    d = evaluate(state=state, config=cfg)

    if args.command == "status":
        _print_decision(d, as_json=args.json)
        return 0 if d.ok or d.band == "unknown" else 0

    if args.command == "suggest":
        _print_decision(d, as_json=args.json)
        if d.action == "suggest_end" and d.candidate:
            return 0
        if d.band in ("warn", "hard") and d.action == "none":
            return 2
        return 0

    if args.command == "dry-run":
        if args.tool and args.session:
            result = end_session(args.tool, args.session, state=state, dry_run=True)
            append_audit(
                "dry_run",
                {
                    "app": args.tool,
                    "session_id": args.session,
                    "pids": result.get("pids"),
                    "ok": result.get("ok"),
                },
                state=state,
            )
        else:
            # Only dry-run end when pressure says so, unless --heaviest forced
            if d.action != "suggest_end" and not args.heaviest:
                _print_decision(d, as_json=args.json)
                if not args.json:
                    print("dry-run: no end suggested (pressure ok or no candidate)")
                return 0
            result = _apply_candidate(
                d,
                dry_run=True,
                state=state,
                yes=True,
                force_heaviest=args.heaviest,
            )
        if args.json:
            print(json.dumps({"decision": d.to_dict(), "result": result}, indent=2))
        else:
            _print_decision(d, as_json=False)
            if result.get("ok"):
                print("dry-run ok:", result.get("app"), result.get("session_id"))
                if result.get("pids"):
                    print("pids:", " ".join(str(x) for x in result["pids"]))
            else:
                print(result.get("error") or result, file=sys.stderr)
        return 0 if result.get("ok") else 1

    if args.command == "apply":
        # Hard guard: never apply without --yes (L4). auto_end LaunchAgent is later.
        if not args.yes:
            print(
                "refusing apply without --yes (L4: confirm before destroy)",
                file=sys.stderr,
            )
            return 2
        if args.tool and args.session:
            result = end_session(args.tool, args.session, state=state, dry_run=False)
            append_audit(
                "apply",
                {
                    "app": args.tool,
                    "session_id": args.session,
                    "pids": result.get("pids"),
                    "ok": result.get("ok"),
                    "error": result.get("error"),
                },
                state=state,
            )
        else:
            if d.action != "suggest_end" and not args.heaviest:
                _print_decision(d, as_json=args.json)
                if not args.json:
                    print("apply: no end suggested (use --heaviest --yes to force)")
                return 0
            result = _apply_candidate(
                d,
                dry_run=False,
                state=state,
                yes=True,
                force_heaviest=args.heaviest,
            )
        if args.json:
            print(json.dumps({"decision": d.to_dict(), "result": result}, indent=2))
        else:
            if result.get("ok"):
                print(result.get("message") or "ok")
                if result.get("pids"):
                    print("pids:", " ".join(str(x) for x in result["pids"]))
            else:
                print(
                    result.get("error") or result.get("message") or "failed",
                    file=sys.stderr,
                )
        return 0 if result.get("ok") else 1

    p.error(f"unknown command {args.command}")
    return 2
