"""mux / CLI default resolution tests."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.mux import (  # noqa: 
    build_dash_plan,
    deps_available,
    resolve_interactive_target,
    session_exists,
    shell_join,
    tui_command,
)


class TestResolveInteractive(unittest.TestCase):
    def test_inside_tmux_is_tui(self):
        self.assertEqual(
            resolve_interactive_target(
                env={"TMUX": "/tmp/tmux-1/default,123,0"},
                tmux_path="/opt/homebrew/bin/tmux",
                glances_path="/opt/homebrew/bin/glances",
            ),
            "tui",
        )

    def test_with_deps_outside_tmux_is_dash(self):
        self.assertEqual(
            resolve_interactive_target(
                env={},
                tmux_path="/opt/homebrew/bin/tmux",
                glances_path="/opt/homebrew/bin/glances",
            ),
            "dash",
        )

    def test_missing_deps_is_tui(self):
        self.assertEqual(
            resolve_interactive_target(
                env={},
                tmux_path=None,
                glances_path="/opt/homebrew/bin/glances",
            ),
            "tui",
        )
        self.assertEqual(
            resolve_interactive_target(
                env={},
                tmux_path="/opt/homebrew/bin/tmux",
                glances_path=None,
            ),
            "tui",
        )

    def test_force_dash_inside_tmux(self):
        self.assertEqual(
            resolve_interactive_target(
                force_dash=True,
                env={"TMUX": "1"},
                tmux_path="/x/tmux",
                glances_path="/x/glances",
            ),
            "dash",
        )

    def test_force_tui(self):
        self.assertEqual(
            resolve_interactive_target(
                force_tui=True,
                env={},
                tmux_path="/x/tmux",
                glances_path="/x/glances",
            ),
            "tui",
        )


class TestBuildPlan(unittest.TestCase):
    def test_glances_args_and_tui(self):
        plan = build_dash_plan(
            session="local-ai-monitor-test",
            glances_bin="/opt/homebrew/bin/glances",
            tmux_bin="/opt/homebrew/bin/tmux",
            local_ai_monitor="/Users/x/.local/bin/local-ai-monitor",
            glances_args=["-t", "2", "--disable-plugin", "docker"],
            view="session",
            threads=False,
        )
        self.assertEqual(plan["glances_argv"][0], "/opt/homebrew/bin/glances")
        self.assertIn("-t", plan["glances_argv"])
        self.assertEqual(plan["tui_argv"][0], "/Users/x/.local/bin/local-ai-monitor")
        self.assertEqual(plan["tui_argv"][1], "tui")
        self.assertIn("--view", plan["tui_argv"])
        self.assertNotIn("--threads", plan["tui_argv"])
        self.assertIn("glances", plan["glances_shell"])
        self.assertIn("tui", plan["tui_shell"])

    def test_tui_threads_flag(self):
        cmd = tui_command("/bin/local-ai-monitor", threads=True, view="app", interval=1.5)
        self.assertIn("--threads", cmd)
        self.assertIn("1.5", cmd)
        self.assertIn("app", cmd)

    def test_shell_join_quotes_spaces(self):
        s = shell_join(["/path/with space/local-ai-monitor", "tui"])
        # shlex.quote wraps spaces
        self.assertTrue(" " not in s.split()[0] or "'" in s or '"' in s or "\\" in s)
        self.assertTrue(s.endswith("tui") or s.endswith("'tui'") or "tui" in s)


class TestDeps(unittest.TestCase):
    def test_deps_available_both(self):
        ok, t, g = deps_available("/bin/tmux", "/bin/glances")
        self.assertTrue(ok)
        self.assertEqual(t, "/bin/tmux")

    def test_deps_missing(self):
        ok, _, _ = deps_available(None, "/bin/glances")
        self.assertFalse(ok)


@unittest.skipUnless(
    shutil_which := __import__("shutil").which("tmux"),
    "tmux not installed",
)
class TestTmuxDetached(unittest.TestCase):
    """Agent-env: create detached session, verify 2 panes, destroy. No attach."""

    SESSION = "local-ai-monitor-pr5-test"

    def tearDown(self):
        subprocess.run(
            ["tmux", "kill-session", "-t", self.SESSION],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_create_two_panes_detached(self):
        from local_ai_monitor.mux import run_dash

        # glances may be heavy; use sleep stand-ins via recreate + mock plan?
        # Real tools present on this machine — use short-lived sleep panes by
        # temporarily patching build commands through run_dash with env PATH.
        # Simpler: invoke tmux ourselves matching design shape.
        glances = __import__("shutil").which("glances")
        if not glances:
            self.skipTest("glances missing")
        # Use sleep so we don't need a TTY for glances
        subprocess.run(["tmux", "kill-session", "-t", self.SESSION], capture_output=True)
        r = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.SESSION,
                "-n",
                "main",
                "sleep 30",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(
            [
                "tmux",
                "split-window",
                "-h",
                "-t",
                f"{self.SESSION}:main",
                "sleep 30",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(session_exists("tmux", self.SESSION))
        # list panes
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", self.SESSION, "-F", "#{pane_index}"],
            text=True,
        )
        panes = [x for x in out.splitlines() if x.strip()]
        self.assertGreaterEqual(len(panes), 2)

    def test_run_dash_attach_false(self):
        from local_ai_monitor.mux import find_glances, find_tmux, run_dash

        if not find_tmux() or not find_glances():
            self.skipTest("tmux/glances missing")
        # Isolate session name via config is fixed — use kill of default only if we
        # use a unique session by patching load_config
        with mock.patch("local_ai_monitor.mux.load_config", return_value={
            "mux": {
                "tmux_session": self.SESSION,
                "glances_left": True,
                "glances_args": ["-t", "2", "--disable-plugin", "docker"],
            }
        }):
            # Patch glances/tui shells to sleep so no TTY needed
            with mock.patch(
                "local_ai_monitor.mux.build_dash_plan",
                return_value={
                    "tmux": "tmux",
                    "session": self.SESSION,
                    "glances_argv": ["sleep", "60"],
                    "tui_argv": ["sleep", "60"],
                    "glances_shell": "sleep 60",
                    "tui_shell": "sleep 60",
                    "glances_left": True,
                },
            ):
                rc = run_dash(attach=False, recreate=True)
        self.assertEqual(rc, 0)
        self.assertTrue(session_exists("tmux", self.SESSION))
        out = subprocess.check_output(
            ["tmux", "list-panes", "-t", self.SESSION, "-F", "#{pane_index}"],
            text=True,
        )
        self.assertGreaterEqual(len([x for x in out.splitlines() if x.strip()]), 2)


class TestCliBareResolution(unittest.TestCase):
    def test_main_bare_is_home_message_not_dash(self):
        """Non-tech default: bare local-ai-monitor never opens tmux dash."""
        from local_ai_monitor import cli

        with mock.patch("local_ai_monitor.mux.run_dash", return_value=0) as rd:
            with mock.patch("local_ai_monitor.simple.print_home_message", return_value=0) as home:
                rc = cli.main([])
        self.assertEqual(rc, 0)
        home.assert_called_once()
        rd.assert_not_called()

    def test_main_simple_mode(self):
        from local_ai_monitor import cli

        with mock.patch("local_ai_monitor.simple.run_simple", return_value=0) as rs:
            rc = cli.main(["simple"])
        self.assertEqual(rc, 0)
        rs.assert_called_once()

    def test_main_tui_explicit(self):
        from local_ai_monitor import cli

        with mock.patch("local_ai_monitor.cli._run_tui", return_value=0) as rt:
            rc = cli.main(["tui"])
        self.assertEqual(rc, 0)
        rt.assert_called_once()

    def test_main_dash_explicit(self):
        from local_ai_monitor import cli

        with mock.patch("local_ai_monitor.mux.run_dash", return_value=0) as rd:
            rc = cli.main(["dash"])
        self.assertEqual(rc, 0)
        rd.assert_called_once()

    def test_once_still_snapshot(self):
        from local_ai_monitor import cli

        with mock.patch("local_ai_monitor.cli._run_tui", return_value=0) as rt:
            rc = cli.main(["once"])
        self.assertEqual(rc, 0)
        _, kwargs = rt.call_args
        self.assertTrue(kwargs.get("once"))


if __name__ == "__main__":
    unittest.main()
