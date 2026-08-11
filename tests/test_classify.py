#!/usr/bin/env python3
"""Contract tests for session identity.

IDs from design: C-GROK-*, C-BUZZ-*, C-SELF-*, C-TREE-*, C-CLAUDE-*.
"""

from __future__ import annotations

import os
import sys
import unittest

# Prefer source tree
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.classify import classify_direct  # noqa: 
from local_ai_monitor.self_filter import filter_self, is_self_pattern  # noqa: 
from local_ai_monitor.sessionize import (  # noqa: 
    GrokUuidCache,
    Sessionizer,
    buzz_agent,
    buzz_channel,
    buzz_identity,
    buzz_slug,
    is_session_root,
    is_trustworthy_pwd,
    make_proc,
    resolve_cwd,
    sessionize_all,
)


class TestBuzzSlug(unittest.TestCase):
    """C-BUZZ channel ids (legacy buzz_slug = channel)."""

    def test_c_buzz_01_xpc_tunnel(self):
        p = make_proc(1, 0, "node app XPC_SERVICE_NAME=com.buzz.sample-app-tunnel")
        self.assertEqual(buzz_slug(p), "sample-app")
        self.assertEqual(buzz_channel(p), "sample-app")
        self.assertEqual(buzz_agent(p), "tunnel")

    def test_c_buzz_01b_xpc_plain(self):
        p = make_proc(1, 0, "node app XPC_SERVICE_NAME=com.buzz.sample-app")
        self.assertEqual(buzz_slug(p), "sample-app")

    def test_c_buzz_02_projects_path(self):
        p = make_proc(
            1,
            0,
            "node /Users/u/.buzz/PROJECTS/buzz-sample-app/app/server.js",
        )
        self.assertEqual(buzz_slug(p), "sample-app")

    def test_c_buzz_03_encyclopedia_strip(self):
        p = make_proc(
            1, 0, "node /Users/u/.buzz/PROJECTS/buzz-encyclopedia/index.js"
        )
        self.assertEqual(buzz_slug(p), "encyclopedia")

    def test_c_buzz_05_casefold_larder(self):
        a = make_proc(1, 0, "node /Users/u/.buzz/REPOS/Larder/x.js")
        b = make_proc(2, 0, "node /Users/u/.buzz/PROJECTS/buzz-larder/x.js")
        self.assertEqual(buzz_slug(a), "larder")
        self.assertEqual(buzz_slug(b), "larder")
        self.assertEqual(buzz_slug(a), buzz_slug(b))

    def test_c_buzz_repos_long(self):
        p = make_proc(
            1,
            0,
            "node /Users/u/.buzz/REPOS/health-system-optimization/server.js",
        )
        self.assertEqual(buzz_slug(p), "health-system-optimization")

    def test_desktop(self):
        p = make_proc(1, 0, "/Applications/Buzz.app/Contents/MacOS/Buzz")
        self.assertEqual(buzz_slug(p), "desktop")


class TestBuzzChannelAndAgent(unittest.TestCase):
    """Channel + agent (who works on the channel)."""

    def test_acp_operator_on_channel(self):
        p = make_proc(
            1,
            0,
            "python3 -m http.server "
            "PWD=/Users/u/.buzz/PROJECTS/buzz-sample-meshsim/MeshSim/viz "
            "BUZZ_ACP_SESSION_TITLE=Operator "
            "BUZZ_ACP_AGENT_COMMAND=/Users/u/.local/bin/grok "
            "BUZZ_ACP_MODEL=grok-4.5 "
            "BUZZ_MANAGED_AGENT=xyz.block.buzz.app",
        )
        ch = buzz_channel(p)
        ag = buzz_agent(p)
        sid, label, _, _ = buzz_identity(p)
        self.assertEqual(ch, "sample-meshsim")
        self.assertEqual(ag, "operator/grok")
        self.assertEqual(sid, "buzz:sample-meshsim|operator/grok")
        self.assertEqual(label, "sample-meshsim · operator/grok")

    def test_same_channel_two_agents_separate_rows(self):
        tunnel = make_proc(
            1,
            0,
            "ngrok http 3000 XPC_SERVICE_NAME=com.buzz.sample-app-tunnel "
            "path=/.buzz/PROJECTS/buzz-sample-app/.scratch/t.yml",
            pcpu=0.1,
            rss_kb=1000,
        )
        worker = make_proc(
            2,
            0,
            "node server.js "
            "PWD=/Users/u/.buzz/PROJECTS/buzz-sample-app "
            "BUZZ_ACP_SESSION_TITLE=Fizz "
            "BUZZ_ACP_AGENT_COMMAND=/usr/local/bin/claude",
            pcpu=1.0,
            rss_kb=2000,
        )
        for p in (tunnel, worker):
            p.direct_app = classify_direct(p)
            p.app = p.direct_app
        # force classify if path markers enough
        tunnel.direct_app = "Buzz"
        tunnel.app = "Buzz"
        worker.direct_app = "Buzz"
        worker.app = "Buzz"
        sessions = sessionize_all(
            [tunnel, worker],
            sessionizer=Sessionizer(start_unix_fn=lambda p: 0),
            skip_lsof=True,
        )
        ids = {s.session_id for s in sessions}
        self.assertIn("buzz:sample-app|tunnel", ids)
        self.assertIn("buzz:sample-app|fizz/claude", ids)
        self.assertEqual(len(sessions), 2)

    def test_acp_child_without_channel_inherits_buzz_parent(self):
        parent = make_proc(
            100,
            1,
            "/Applications/Buzz.app/Contents/MacOS/buzz-acp "
            "BUZZ_ACP_SESSION_TITLE=Munger.Claude "
            "BUZZ_MANAGED_AGENT=xyz.block.buzz.app",
            pcpu=0.1,
            rss_kb=1000,
        )
        child = make_proc(
            101,
            100,
            "/Users/u/.local/bin/claude --session-id=x "
            "BUZZ_ACP_SESSION_TITLE=Munger.Claude "
            "BUZZ_MANAGED_AGENT=xyz.block.buzz.app",
            pcpu=2.5,
            rss_kb=2000,
        )
        for p in (parent, child):
            p.direct_app = classify_direct(p)
            p.app = p.direct_app
        self.assertTrue(is_session_root(parent))
        self.assertFalse(is_session_root(child))

        sessions = sessionize_all(
            [parent, child],
            sessionizer=Sessionizer(start_unix_fn=lambda p: 0),
            skip_lsof=True,
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "buzz:desktop|munger.claude")
        self.assertEqual(sessions[0].pids, {100, 101})
        self.assertAlmostEqual(sessions[0].pcpu, 2.6)
        self.assertEqual(sessions[0].rss_kb, 3000)

    def test_acp_process_with_channel_evidence_stays_root(self):
        p = make_proc(
            100,
            1,
            "/Users/u/.local/bin/claude --session-id=x "
            "XPC_SERVICE_NAME=com.buzz.sample-app "
            "BUZZ_ACP_SESSION_TITLE=Munger.Claude "
            "BUZZ_MANAGED_AGENT=xyz.block.buzz.app",
            pcpu=1.0,
            rss_kb=1000,
        )
        p.direct_app = classify_direct(p)
        p.app = p.direct_app
        self.assertTrue(is_session_root(p))

        sessions = sessionize_all(
            [p],
            sessionizer=Sessionizer(start_unix_fn=lambda pid: 0),
            skip_lsof=True,
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "buzz:sample-app|munger.claude")


class TestBuzzWinsOverGrok(unittest.TestCase):
    """C-BUZZ-04"""

    def test_c_buzz_04_buzz_wins_over_grok_agent(self):
        p = make_proc(
            10,
            1,
            "/opt/homebrew/bin/grok "
            "GROK_AGENT=1 "
            "PWD=/Users/u/.buzz/PROJECTS/buzz-sample-app "
            "XPC_SERVICE_NAME=com.buzz.sample-app",
        )
        self.assertEqual(classify_direct(p), "Buzz")
        p.direct_app = classify_direct(p)
        p.app = p.direct_app
        self.assertTrue(is_session_root(p))
        sz = Sessionizer(
            start_unix_fn=lambda pid: 1000,
            lsof_path_overrides={},
        )
        sessions = sessionize_all([p], sessionizer=sz, skip_lsof=True)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].app, "Buzz")
        # channel|agent (default agent for plain service = runtime/worker/basename)
        self.assertTrue(sessions[0].session_id.startswith("buzz:sample-app|"))
        self.assertIn("sample-app", sessions[0].label)


class TestGrokUuid(unittest.TestCase):
    """C-GROK-01..04, C-GROK-06"""

    def test_c_grok_01_single_uuid(self):
        paths = [
            "/Users/u/.grok/sessions/%2Ftmp%2Fws/019fae27-d955-7e71-b0ca-45fe8250e2ee/events.jsonl",
        ]
        cache = GrokUuidCache()
        u = cache.pick(100, paths, mtimes={paths[0]: 100.0})
        self.assertEqual(u, "019fae27-d955-7e71-b0ca-45fe8250e2ee")

    def test_c_grok_02_dual_uuid_sticky(self):
        old = "/Users/u/.grok/sessions/enc/019fae43-aaaa-7e71-b0ca-45fe8250e2ee/events.jsonl"
        new = "/Users/u/.grok/sessions/enc/019fae55-bbbb-7e71-b0ca-45fe8250e2ee/events.jsonl"
        cache = GrokUuidCache()
        # first sample: only old
        u1 = cache.pick(100, [old], mtimes={old: 50.0})
        self.assertEqual(u1, "019fae43-aaaa-7e71-b0ca-45fe8250e2ee")
        # both open: newer mtime wins candidate but sticky keeps old while still open
        u2 = cache.pick(
            100, [old, new], mtimes={old: 50.0, new: 200.0}
        )
        self.assertEqual(u2, "019fae43-aaaa-7e71-b0ca-45fe8250e2ee")
        # old gone; need N=2 sticky samples
        u3 = cache.pick(100, [new], mtimes={new: 200.0})
        self.assertEqual(u3, "019fae43-aaaa-7e71-b0ca-45fe8250e2ee")  # sticky 1
        u4 = cache.pick(100, [new], mtimes={new: 200.0})
        self.assertEqual(u4, "019fae55-bbbb-7e71-b0ca-45fe8250e2ee")  # sticky 2 → switch

    def test_c_grok_03_no_uuid_in_ps_cmd(self):
        # grok cmd without sessions path still classifies as Grok binary
        p = make_proc(5, 1, "/opt/homebrew/bin/grok --help PWD=/tmp")
        self.assertEqual(classify_direct(p), "Grok")
        # sessionize without lsof → fallback pid:start
        sz = Sessionizer(start_unix_fn=lambda pid: 42, lsof_path_overrides={5: []})
        sessions = sessionize_all([p], sessionizer=sz, skip_lsof=False)
        # empty lsof paths → no uuid
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].session_id.startswith("pid:5:start:"))

    def test_c_grok_04_fallback_pid_start(self):
        p = make_proc(7, 1, "/usr/local/bin/grok")
        sz = Sessionizer(start_unix_fn=lambda pid: 99, lsof_path_overrides={7: []})
        sessions = sessionize_all([p], sessionizer=sz)
        self.assertEqual(sessions[0].session_id, "pid:7:start:99")

    def test_c_grok_sessionize_with_uuid(self):
        p = make_proc(9, 1, "/opt/homebrew/bin/grok", pcpu=10.0, rss_kb=1000)
        path = (
            "/Users/u/.grok/sessions/%2FUsers%2Fu%2Fproj/"
            "019fae27-d955-7e71-b0ca-45fe8250e2ee/events.jsonl"
        )
        sz = Sessionizer(
            start_unix_fn=lambda pid: 0,
            lsof_path_overrides={9: [path]},
            lsof_cwd_overrides={9: "/Users/u/proj"},
            mtime_overrides={path: 1.0},
        )
        sessions = sessionize_all([p], sessionizer=sz)
        self.assertEqual(sessions[0].session_id, "019fae27-d955-7e71-b0ca-45fe8250e2ee")
        self.assertIn("proj", sessions[0].label)
        self.assertIn("019fae27", sessions[0].label)


class TestTreeAndWorkers(unittest.TestCase):
    """C-TREE-01, C-TREE-02"""

    def test_c_tree_01_multi_root_separate_sessions(self):
        g1 = make_proc(10, 1, "/opt/bin/grok", pcpu=1.0, rss_kb=100)
        g2 = make_proc(20, 1, "/opt/bin/grok", pcpu=2.0, rss_kb=200)
        path1 = "/x/.grok/sessions/a/019faaaa-aaaa-7e71-b0ca-45fe8250e2ee/events.jsonl"
        path2 = "/x/.grok/sessions/b/019fbbbb-bbbb-7e71-b0ca-45fe8250e2ee/events.jsonl"
        sz = Sessionizer(
            start_unix_fn=lambda pid: pid,
            lsof_path_overrides={10: [path1], 20: [path2]},
            mtime_overrides={path1: 1.0, path2: 1.0},
        )
        sessions = sessionize_all([g1, g2], sessionizer=sz)
        self.assertEqual(len(sessions), 2)
        ids = {s.session_id for s in sessions}
        self.assertEqual(
            ids,
            {
                "019faaaa-aaaa-7e71-b0ca-45fe8250e2ee",
                "019fbbbb-bbbb-7e71-b0ca-45fe8250e2ee",
            },
        )

    def test_c_tree_02_worker_inherits_not_root(self):
        root = make_proc(10, 1, "/opt/bin/grok", pcpu=5.0, rss_kb=500)
        worker = make_proc(
            11,
            10,
            "python worker.py GROK_AGENT=1",
            pcpu=3.0,
            rss_kb=300,
        )
        path = "/x/.grok/sessions/e/019fae27-d955-7e71-b0ca-45fe8250e2ee/events.jsonl"
        sz = Sessionizer(
            start_unix_fn=lambda pid: 1,
            lsof_path_overrides={10: [path], 11: []},
            mtime_overrides={path: 1.0},
        )
        sessions = sessionize_all([root, worker], sessionizer=sz)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].nproc, 2)
        self.assertAlmostEqual(sessions[0].pcpu, 8.0)
        self.assertEqual(sessions[0].rss_kb, 800)
        # worker alone is not session root
        worker.app = "Grok"
        worker.direct_app = "Grok"
        self.assertFalse(is_session_root(worker))


class TestClaude(unittest.TestCase):
    """C-CLAUDE-01, C-CLAUDE-02"""

    def test_c_claude_01_session_key(self):
        p = make_proc(30, 1, "/opt/homebrew/bin/claude", pcpu=1.0, rss_kb=50)
        sz = Sessionizer(
            start_unix_fn=lambda pid: 12345,
            lsof_cwd_overrides={30: "/Users/u/Claude Projects/sample_project"},
        )
        sessions = sessionize_all([p], sessionizer=sz, skip_lsof=True)
        # skip_lsof still uses overrides via _cwd? skip_lsof skips paths for Grok
        # and for Claude when skip_lsof True, we don't call _cwd. Fix: don't skip
        sessions = sessionize_all([p], sessionizer=sz, skip_lsof=False)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "pid:30:start:12345")
        self.assertEqual(sessions[0].label, "sample_project")

    def test_c_claude_02_reject_truncated_pwd(self):
        home = "/Users/u"
        self.assertFalse(is_trustworthy_pwd(f"{home}/Grok", home=home))
        self.assertFalse(is_trustworthy_pwd(f"{home}/Claude", home=home))
        self.assertTrue(
            is_trustworthy_pwd(f"{home}/Claude Projects/sample_project", home=home)
        )
        cwd = resolve_cwd(
            1,
            f"claude PWD={home}/Grok",
            lsof_cwd=None,
            session_open_paths=None,
            home=home,
        )
        # untrustworthy PWD alone → None (or untrusted returned only if no better)
        # resolve_cwd returns pwd only if trustworthy; else None
        self.assertIsNone(cwd)


class TestSelfFilter(unittest.TestCase):
    """C-SELF"""

    def test_c_self_launchd_parent_does_not_wipe_tree(self):
        """: collector under launchd has ppid=1; must not expand-from-init."""
        from unittest import mock
        from local_ai_monitor.self_filter import filter_self

        # pid 1 launchd; pid 99 collector; pid 50 grok under launchd
        launchd = make_proc(1, 0, "/sbin/launchd")
        collector = make_proc(99, 1, "python3 -m local_ai_monitor collect")
        grok = make_proc(50, 1, "/opt/homebrew/bin/grok", pcpu=5.0, rss_kb=1000)
        with mock.patch("local_ai_monitor.self_filter.os.getpid", return_value=99):
            with mock.patch("local_ai_monitor.self_filter.os.getppid", return_value=1):
                kept = filter_self([launchd, collector, grok])
        pids = {p.pid for p in kept}
        self.assertIn(50, pids)  # grok survives
        self.assertNotIn(99, pids)  # collector excluded
        # launchd (pid 1) may remain or not; must not remove grok via expand

    def test_c_self_local_ai_monitor(self):
        p = make_proc(1, 0, "/opt/homebrew/bin/python3 -m local_ai_monitor once")
        self.assertTrue(is_self_pattern(p))

    def test_c_self_not_local_ai_monitor_env_pollution(self):
        """LOCAL_AI_MONITOR= in env must not mark Grok as self (C-SELF false positive)."""
        p = make_proc(
            34002,
            1,
            "grok TERM=xterm LOCAL_AI_MONITOR=/Users/u/.local/bin/local-ai-monitor LANG=en_US.UTF-8",
            pcpu=10.0,
            rss_kb=1000,
        )
        self.assertFalse(is_self_pattern(p))
        self.assertEqual(classify_direct(p), "Grok")

    def test_c_self_glances_tmux(self):
        self.assertTrue(
            is_self_pattern(make_proc(1, 0, "/opt/homebrew/bin/glances -t 2"))
        )
        self.assertTrue(is_self_pattern(make_proc(2, 0, "tmux: server")))
        self.assertTrue(is_self_pattern(make_proc(3, 0, "/usr/bin/tmux new -s x")))

    def test_c_self_excluded_from_sessionize(self):
        grok = make_proc(10, 1, "/opt/bin/grok", pcpu=1.0, rss_kb=100)
        mon = make_proc(99, 1, "python3 -m local_ai_monitor live", pcpu=50.0, rss_kb=9999)
        path = "/x/.grok/sessions/e/019fae27-d955-7e71-b0ca-45fe8250e2ee/events.jsonl"
        sz = Sessionizer(
            start_unix_fn=lambda pid: 1,
            lsof_path_overrides={10: [path]},
            mtime_overrides={path: 1.0},
        )
        sessions = sessionize_all([grok, mon], sessionizer=sz)
        all_pids = set()
        for s in sessions:
            all_pids |= s.pids
        self.assertNotIn(99, all_pids)
        self.assertIn(10, all_pids)


class TestNoGrokProjectsFalsePositive(unittest.TestCase):
    def test_folder_name_not_grok(self):
        p = make_proc(
            1,
            0,
            "python3 script.py PWD=/Users/alice/Projects/foo",
        )
        self.assertIsNone(classify_direct(p))


class TestOpenClawBuzzMerge(unittest.TestCase):
    def test_openclaw_gateway(self):
        p = make_proc(
            1,
            0,
            "/opt/homebrew/bin/node /opt/homebrew/lib/node_modules/openclaw/dist/index.js "
            "gateway --port 18789 OPENCLAW_SERVICE_MARKER=openclaw "
            "XPC_SERVICE_NAME=ai.openclaw.gateway",
            pcpu=0.1,
            rss_kb=80000,
        )
        self.assertEqual(classify_direct(p), "OpenClaw")
        sessions = sessionize_all(
            [p], sessionizer=Sessionizer(start_unix_fn=lambda p: 0), skip_lsof=True
        )
        self.assertEqual(sessions[0].session_id, "svc:gateway")
        self.assertEqual(sessions[0].label, "gateway :18789")

    def test_buzz_same_channel_different_roles_are_separate(self):
        """Channel listed once per agent/role — tunnel ≠ runtime."""
        a = make_proc(
            1,
            0,
            "node x XPC_SERVICE_NAME=com.buzz.sample-app-tunnel",
            pcpu=1.0,
            rss_kb=100,
        )
        b = make_proc(
            2,
            0,
            "node /Users/u/.buzz/PROJECTS/buzz-sample-app/server.js",
            pcpu=2.0,
            rss_kb=200,
        )
        sessions = sessionize_all(
            [a, b], sessionizer=Sessionizer(start_unix_fn=lambda p: 0), skip_lsof=True
        )
        self.assertEqual(len(sessions), 2)
        ids = {s.session_id for s in sessions}
        self.assertTrue(any("tunnel" in i for i in ids))
        self.assertTrue(all(i.startswith("buzz:sample-app|") for i in ids))

    def test_buzz_merge_same_channel_and_agent(self):
        a = make_proc(
            1,
            0,
            "node w1 "
            "PWD=/Users/u/.buzz/PROJECTS/buzz-sample-app "
            "BUZZ_ACP_SESSION_TITLE=Operator "
            "BUZZ_ACP_AGENT_COMMAND=/bin/grok",
            pcpu=1.0,
            rss_kb=100,
        )
        b = make_proc(
            2,
            0,
            "node w2 "
            "PWD=/Users/u/.buzz/PROJECTS/buzz-sample-app "
            "BUZZ_ACP_SESSION_TITLE=Operator "
            "BUZZ_ACP_AGENT_COMMAND=/bin/grok",
            pcpu=2.0,
            rss_kb=200,
        )
        sessions = sessionize_all(
            [a, b], sessionizer=Sessionizer(start_unix_fn=lambda p: 0), skip_lsof=True
        )
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "buzz:sample-app|operator/grok")
        self.assertEqual(sessions[0].nproc, 2)
        self.assertAlmostEqual(sessions[0].pcpu, 3.0)


if __name__ == "__main__":
    unittest.main()
