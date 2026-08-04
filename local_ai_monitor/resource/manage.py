"""Self-resource manager — reclaim *idle* load so 8 GB stays usable.

Physics: free pages → when.
Activity: active vs idle → what is legal.
Law: never auto-kill mid-stream active work (this Grok session, needs_you, high CPU).

Legal auto-reclaim:
  - Quarantined tools (pre-seeded OpenClaw)
  - idle_service (background gateway, no task)
  - idle AI sessions (low CPU, stale activity)
  - Apple apps (Safari/Chrome/…) via graceful quit under hard band

Illegal auto-reclaim:
  - active CPU / recent activity / attention rows
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from local_ai_monitor.activity import enrich_sessions_activity, pick_reclaim_candidate
from local_ai_monitor.config import ensure_state_dir, state_dir
from local_ai_monitor.humanize import tool_display_name
from local_ai_monitor.resource.audit import append_audit
from local_ai_monitor.resource.config import load_resource_config
from local_ai_monitor.resource.human import humanize_decision
from local_ai_monitor.resource.policy import evaluate
from local_ai_monitor.soft_stop import soft_stop_session
from local_ai_monitor.store import atomic_write_json, live_path, read_json


def _state_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "resource-manage.json")


def _calib_path(state: Optional[str] = None) -> str:
    return os.path.join(state_dir(state), "resource-calib.jsonl")


def _load_state(state: Optional[str] = None) -> Dict[str, Any]:
    path = _state_path(state)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_state(data: Dict[str, Any], state: Optional[str] = None) -> None:
    ensure_state_dir(state)
    path = _state_path(state)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def append_calib_sample(
    *,
    free_pages: Optional[int],
    band: str,
    memsize: Optional[int],
    state: Optional[str] = None,
    acted: bool = False,
) -> None:
    try:
        ensure_state_dir(state)
        path = _calib_path(state)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "free_pages": free_pages,
            "band": band,
            "memsize_bytes": memsize,
            "acted": acted,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def merge_resource_into_live(
    resource: Dict[str, Any],
    *,
    state: Optional[str] = None,
) -> bool:
    path = live_path(state)
    live = read_json(path)
    if not isinstance(live, dict):
        return False
    live["resource"] = resource
    try:
        atomic_write_json(path, live)
        return True
    except OSError:
        return False


def post_macos_notification(title: str, body: str) -> bool:
    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(body)}" with title "{esc(title)}" '
        f'subtitle "Local AI Monitor · Memory"'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def manage_once(*, state: Optional[str] = None) -> Dict[str, Any]:
    """One orchestration tick."""
    cfg = load_resource_config()
    auto_end = bool(cfg.get("auto_end")) is True
    if os.environ.get("LOCAL_AI_MONITOR_NO_AUTO_END", "").strip() in ("1", "true", "yes"):
        auto_end = False
    # Absolute law — config cannot enable active kills
    auto_end_active = False
    _ = auto_end_active
    auto_band = str(cfg.get("auto_end_band") or "hard")
    cooldown = int(cfg.get("cooldown_s") or 60)
    protect = [str(x) for x in (cfg.get("protect_tools") or [])]
    from local_ai_monitor.activity import INTERACTIVE_APPS

    for _t in sorted(INTERACTIVE_APPS):
        if _t not in protect:
            protect.append(_t)
    notify_mode = str(cfg.get("notify") or "both")
    max_ends = int(cfg.get("max_auto_ends_per_tick") or 1)
    min_rss = int(cfg.get("min_rss_kb") or 512 * 1024)
    # Opt-in only — surprise browser quits are hostile.
    apple_relief = bool(cfg.get("apple_relief", False))
    apple_min = int(cfg.get("apple_min_rss_kb") or 1024 * 1024)
    # Interactive CLIs never auto soft-stopped unless user sets auto_end_cli.
    auto_end_cli = bool(cfg.get("auto_end_cli", False))
    # Thrash ladder: pause→mute→soft-stop→force→apple (opt-in). Default ON.
    thrash_ladder_on = bool(cfg.get("thrash_ladder", True))
    thrash_act_floor = float(cfg.get("thrash_act_floor") or 8.0)

    d = evaluate(state=state, config=cfg)
    phys = d.physics if isinstance(d.physics, dict) else {}
    hr = phys.get("headroom") if isinstance(phys.get("headroom"), dict) else {}
    try:
        thrash_score = float(hr.get("thrash_score") or 0.0)
    except (TypeError, ValueError):
        thrash_score = 0.0

    acted = False
    act_results: List[Dict[str, Any]] = []
    skip = "band_ok"

    st = _load_state(state)
    last_act = float(st.get("last_act_ts") or 0)
    now = time.time()
    in_cooldown = (now - last_act) < cooldown

    live = read_json(live_path(state)) or {}
    sessions_raw = live.get("sessions") if isinstance(live, dict) else []
    if not isinstance(sessions_raw, list):
        sessions_raw = []
    attention = live.get("attention") if isinstance(live, dict) else []
    sessions = enrich_sessions_activity(sessions_raw, attention if isinstance(attention, list) else [])

    # --- Always: seed quarantine list (enforce only under thrash / auto_end) ---
    try:
        from local_ai_monitor.quarantine import ensure_default_quarantine, enforce_quarantine

        ensure_default_quarantine()
        if auto_end and not in_cooldown and thrash_score >= thrash_act_floor:
            eq = enforce_quarantine(state=state)
            if eq.get("stopped"):
                acted = True
                act_results.append(
                    {
                        "ok": True,
                        "app": ",".join(eq["stopped"]),
                        "message": "Quarantine enforced: " + ", ".join(eq["stopped"]),
                        "kind": "quarantine",
                    }
                )
                skip = "quarantine_enforced"
    except Exception as exc:
        act_results.append({"ok": False, "error": f"quarantine: {exc}"})

    # --- Thrash ladder (primary orchestrator action) ---
    # Free-page-only hard/warn does NOT climb the ladder — thrash_score does.
    thrash_candidate = thrash_score >= thrash_act_floor or (
        d.band == "hard" and thrash_score >= thrash_act_floor * 0.5
    )
    # Require two consecutive hot samples before acting (anti false-spike).
    prev_thrash = float(st.get("last_thrash_score") or 0.0)
    thrash_hot = thrash_candidate and prev_thrash >= thrash_act_floor * 0.5
    st["last_thrash_score"] = thrash_score
    if (
        thrash_ladder_on
        and thrash_hot
        and not in_cooldown
        and skip != "quarantine_enforced"
        and os.environ.get("LOCAL_AI_MONITOR_NO_LADDER", "").strip() not in ("1", "true", "yes")
    ):
        from local_ai_monitor.resource.thrash_ladder import next_step_after, run_ladder_step

        prev = st.get("ladder_step")
        try:
            prev_i = int(prev) if prev is not None else None
        except (TypeError, ValueError):
            prev_i = None
        step = next_step_after(prev_i, thrash_still_high=True)
        result = run_ladder_step(
            step,
            sessions=sessions,
            protect=protect,
            min_rss_kb=min_rss,
            state=state,
            apple_relief=apple_relief,
            apple_min_rss_kb=apple_min,
            dry_run=False,
        )
        act_results.append(result)
        st["ladder_step"] = step
        st["ladder_ts"] = now
        if result.get("acted"):
            acted = True
            skip = f"ladder_{result.get('step') or step}"
        else:
            skip = f"ladder_{result.get('step') or step}_noop"
            st["ladder_step"] = step
    elif thrash_ladder_on and thrash_candidate and not thrash_hot:
        skip = "thrash_confirm_wait"
        _save_state(st, state)
    elif thrash_ladder_on and not thrash_candidate:
        # Cool down ladder when thrash gone
        if st.get("ladder_step") is not None and thrash_score < thrash_act_floor * 0.25:
            st["ladder_step"] = 0
            _save_state(st, state)

    # --- Optional idle soft-stop when auto_end AND thrash (services only) ---
    cand = None
    if (
        auto_end
        and thrash_hot
        and not in_cooldown
        and not acted
        and skip not in ("quarantine_enforced",)
        and not str(skip).startswith("ladder_")
    ):
        want_idle_sessions = bool(auto_end_cli) and d.band == "hard"
        cand = pick_reclaim_candidate(
            sessions,
            protect=protect,
            min_rss_kb=min_rss,
            prefer_services=True,
        )
        if cand is not None:
            is_svc = (
                cand.get("activity_state") == "idle_service"
                or cand.get("kind") == "service"
            )
            app = str(cand.get("app") or "")
            if app in protect or (not is_svc and not want_idle_sessions):
                cand = None
        if cand is not None:
            app = str(cand.get("app") or "")
            sid = str(cand.get("session_id") or "")
            result = soft_stop_session(
                app,
                sid,
                state=state,
                session_row=cand,
                dry_run=False,
                force=False,
            )
            act_results.append(result)
            if result.get("refused"):
                skip = "refused_active"
            elif result.get("ok"):
                acted = True
                skip = "soft_stopped_idle"
            else:
                skip = "soft_stop_failed"
        elif d.band == "hard" and not acted:
            active = [
                s
                for s in sessions
                if isinstance(s, dict)
                and s.get("activity_state") in ("active", "interactive")
            ]
            if active:
                skip = "only_active_remain"
            else:
                skip = "no_idle_candidate"
    elif d.band in ("warn", "hard") and in_cooldown and not acted:
        skip = "cooldown"
    elif d.band in ("warn", "hard") and not thrash_hot and not acted:
        # Thin headroom without thrash: notify only — do not kill
        skip = "headroom_caution_no_thrash"
    elif d.band == "ok" and not acted:
        skip = "band_ok"

    if acted:
        st["last_act_ts"] = now
        last = act_results[-1] if act_results else {}
        st["last_act_app"] = last.get("app")
        st["last_act_session"] = last.get("session_id")
        st["last_act_message"] = last.get("message") or last.get("error")
        _save_state(st, state)

    d_after = evaluate(state=state, config=cfg)
    human = humanize_decision(d_after)
    human["auto_end"] = auto_end
    human["auto_end_idle_only"] = True
    human["manager"] = True

    if acted and act_results:
        last = act_results[-1]
        tool = tool_display_name(str(last.get("app") or "")) if last.get("app") else "a process"
        human["managed"] = True
        if last.get("kind") == "quarantine" or skip == "quarantine_enforced":
            human["managed_label"] = last.get("message")
        elif str(skip).startswith("ladder_"):
            human["managed_label"] = last.get("message") or f"Thrash ladder: {skip}"
        elif skip == "apple_quit":
            human["managed_label"] = last.get("message") or "Quit a memory-heavy Mac app"
        elif last.get("ok"):
            human["managed_label"] = f"Reclaimed idle · stopped {tool}"
        else:
            human["managed_label"] = last.get("message") or last.get("error")
        if d_after.band == "ok":
            human["show"] = True
            human["title"] = "Headroom recovered"
            human["chip"] = "AI · Recovered"
            human["detail"] = str(human.get("managed_label") or "")
        elif thrash_score >= thrash_act_floor:
            human["show"] = True
            human["title"] = "Easing memory thrash"
            human["chip"] = "AI · Thrash ladder"
            human["detail"] = str(human.get("managed_label") or "")
    human["thrash_score"] = thrash_score
    human["ladder_step"] = st.get("ladder_step")
        # If only active remain, explain
    if skip == "only_active_remain":
        human["show"] = True
        human["title"] = "Active work is using headroom"
        human["detail"] = (
            "Headroom is still thin, but every AI session looks mid-stream. "
            "Local AI Monitor will not kill active work. Pause or end a session yourself if needed."
        )
        human["chip"] = "AI · Active work"
        human["action_label"] = "OK"

    if notify_mode in ("menubar_field", "both", "notification"):
        merge_resource_into_live(human, state=state)

    notified = False
    if notify_mode in ("notification", "both"):
        last_n = float(st.get("last_notify_ts") or 0)
        if acted:
            title = "Idle load reclaimed on this Mac"
            body = str(
                human.get("managed_label") or human.get("detail") or "Idle load reclaimed."
            )
            notified = post_macos_notification(title, body[:180])
            st["last_notify_ts"] = now
            _save_state(st, state)
        elif skip == "only_active_remain" and (now - last_n) >= cooldown:
            notified = post_macos_notification(
                "Active AI work using headroom",
                "Won't kill mid-stream. Pause a session if the Mac is unusable.",
            )
            st["last_notify_ts"] = now
            _save_state(st, state)
        elif human.get("show") and d_after.band in ("warn", "hard") and (now - last_n) >= cooldown:
            title = str(human.get("title") or "Headroom is low")
            body = str(human.get("detail") or "Managing idle load only.")
            notified = post_macos_notification(title, body[:180])
            st["last_notify_ts"] = now
            _save_state(st, state)

    append_calib_sample(
        free_pages=d_after.free_pages,
        band=d_after.band,
        memsize=phys.get("memsize_bytes") if isinstance(phys, dict) else None,
        state=state,
        acted=acted,
    )
    append_audit(
        "manage",
        {
            "band": d_after.band,
            "free_pages": d_after.free_pages,
            "acted": acted,
            "skip": skip,
            "auto_end": auto_end,
            "idle_only": True,
            "app": (act_results[-1].get("app") if act_results else None),
            "ok": (act_results[-1].get("ok") if act_results else None),
            "notified": notified,
        },
        state=state,
    )

    return {
        "ok": True,
        "band": d_after.band,
        "acted": acted,
        "skip": skip,
        "auto_end": auto_end,
        "idle_only": True,
        "results": act_results,
        "notified": notified,
        "human": human,
        "free_pages": d_after.free_pages,
    }


def notify_once(*, state: Optional[str] = None) -> Dict[str, Any]:
    """Banner / live field only — NEVER ends sessions (use manage_once for reclaim)."""
    from local_ai_monitor.resource.notify import notify_once as _notify_safe

    return _notify_safe(state=state)


def cmd_manage(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="local-ai-rm manage",
        description="Reclaim idle RAM only. Never kills mid-stream active work.",
    )
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=45.0)
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate path with auto_end forced off",
    )
    args = p.parse_args(list(argv or []))

    def one() -> Dict[str, Any]:
        if args.dry_run:
            os.environ["LOCAL_AI_MONITOR_NO_AUTO_END"] = "1"
        try:
            return manage_once()
        finally:
            if args.dry_run:
                os.environ.pop("LOCAL_AI_MONITOR_NO_AUTO_END", None)

    if args.once or args.interval <= 0:
        out = one()
        if args.json:
            print(json.dumps(out, indent=2, default=str))
        else:
            print(
                f"band={out.get('band')} acted={out.get('acted')} "
                f"skip={out.get('skip')} idle_only={out.get('idle_only')}"
            )
            h = out.get("human") or {}
            if h.get("managed_label"):
                print(f"  {h.get('managed_label')}")
            elif h.get("title"):
                print(f"  {h.get('title')}")
            for r in out.get("results") or []:
                if isinstance(r, dict):
                    print(f"  result: {r.get('message') or r.get('error') or r.get('skip')}")
        return 0 if out.get("ok") else 1

    while True:
        try:
            one()
        except Exception as exc:
            print(f"manage error: {exc}", file=sys.stderr)
        time.sleep(max(15.0, float(args.interval)))


def cmd_notify(argv: Optional[List[str]] = None) -> int:
    from local_ai_monitor.resource.notify import cmd_notify as _cmd

    return _cmd(argv)
