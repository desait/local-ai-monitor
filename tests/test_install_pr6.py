"""install/plist contract tests (absolute python, )."""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.install_svc import (  # noqa: 
    CCM_LABEL,
    COLLECT_LABEL,
    MENUBAR_LABEL,
    RESOURCE_LABEL,
    cmd_uninstall,
    parse_plist_bytes,
    render_collect_plist,
    resolve_python3,
    template_path,
    validate_collect_plist,
)


class TestResolvePython(unittest.TestCase):
    def test_absolute(self):
        py = resolve_python3()
        self.assertTrue(os.path.isabs(py), py)
        self.assertNotIn("env", os.path.basename(py))


class TestPlistRender(unittest.TestCase):
    def test_template_exists(self):
        self.assertTrue(os.path.isfile(template_path(_SRC)))

    def test_render_absolute_python_and_paths(self):
        home = "/Users/testuser"
        state = f"{home}/.local/state/local-ai-monitor"
        lib = f"{home}/.local/lib/local-ai-monitor"
        py = "/opt/homebrew/bin/python3"
        xml = render_collect_plist(
            python=py,
            pythonpath=lib,
            state=state,
            throttle=10,
            template=template_path(_SRC),
        )
        self.assertIn(py, xml)
        self.assertIn(lib, xml)
        self.assertIn(state, xml)
        self.assertNotIn("__PYTHON__", xml)
        self.assertNotIn("#!/usr/bin/env", xml)

        data = parse_plist_bytes(xml)
        errs = validate_collect_plist(data)
        self.assertEqual(errs, [], errs)
        self.assertEqual(data["Label"], COLLECT_LABEL)
        self.assertEqual(data["ProgramArguments"][0], py)
        self.assertEqual(data["ProgramArguments"][1:], ["-m", "local_ai_monitor", "collect"])
        self.assertTrue(data["KeepAlive"])
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["ThrottleInterval"], 10)
        self.assertEqual(data["EnvironmentVariables"]["LC_ALL"], "C")
        self.assertEqual(data["EnvironmentVariables"]["PYTHONPATH"], lib)
        self.assertTrue(data["WorkingDirectory"].endswith("local-ai-monitor"))

    def test_validate_rejects_relative_python(self):
        xml = render_collect_plist(
            python="python3",
            pythonpath="/tmp/lib",
            state="/tmp/state",
            template=template_path(_SRC),
        )
        data = parse_plist_bytes(xml)
        errs = validate_collect_plist(data)
        self.assertTrue(any("absolute" in e for e in errs), errs)

    def test_write_plist_roundtrip(self):
        from local_ai_monitor.install_svc import write_collect_agent_plist

        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "com.user.local-ai-monitor.collect.plist")
            path = write_collect_agent_plist(
                python="/opt/homebrew/bin/python3",
                pythonpath="/Users/x/.local/lib/local-ai-monitor",
                state=td,
                throttle=10,
                dest=dest,
            )
            self.assertEqual(path, dest)
            with open(dest, "rb") as f:
                data = plistlib.load(f)
            self.assertEqual(validate_collect_plist(data), [])


class TestCmdInstallNoBootstrap(unittest.TestCase):
    def test_install_no_bootstrap_no_package_writes_plist(self):
        from local_ai_monitor.install_svc import cmd_install

        with tempfile.TemporaryDirectory() as td:
            dest_dir = os.path.join(td, "LaunchAgents")
            os.makedirs(dest_dir)
            dest = os.path.join(dest_dir, f"{COLLECT_LABEL}.plist")

            # Patch LAUNCH_AGENTS and write path via write_collect_agent_plist dest
            with mock.patch("local_ai_monitor.install_svc.LAUNCH_AGENTS", dest_dir):
                with mock.patch("local_ai_monitor.install_svc.LIB_DIR", td):
                    with mock.patch(
                        "local_ai_monitor.install_svc.ensure_state_dir",
                        return_value=os.path.join(td, "state"),
                    ):
                        with mock.patch(
                            "local_ai_monitor.install_svc.write_default_config",
                            return_value="",
                        ):
                            rc = cmd_install(
                                [
                                    "--no-package",
                                    "--no-bootstrap",
                                    "--source",
                                    _SRC,
                                ]
                            )
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(dest))
            with open(dest, "rb") as f:
                data = plistlib.load(f)
            self.assertEqual(data["ProgramArguments"][1:], ["-m", "local_ai_monitor", "collect"])
            self.assertTrue(os.path.isabs(data["ProgramArguments"][0]))

    def test_install_succeeds_when_swift_menubar_build_fails(self):
        from local_ai_monitor.install_svc import cmd_install

        with tempfile.TemporaryDirectory() as td:
            dest_dir = os.path.join(td, "LaunchAgents")
            os.makedirs(dest_dir)
            dest = os.path.join(dest_dir, f"{COLLECT_LABEL}.plist")

            with mock.patch("local_ai_monitor.install_svc.LAUNCH_AGENTS", dest_dir):
                with mock.patch("local_ai_monitor.install_svc.LIB_DIR", td):
                    with mock.patch(
                        "local_ai_monitor.install_svc.ensure_state_dir",
                        return_value=os.path.join(td, "state"),
                    ):
                        with mock.patch(
                            "local_ai_monitor.install_svc.write_default_config",
                            return_value="",
                        ):
                            with mock.patch(
                                "local_ai_monitor.install_svc.build_menubar_app",
                                return_value=(1, "", "missing Swift SDK"),
                            ):
                                rc = cmd_install(
                                    [
                                        "--no-package",
                                        "--no-bootstrap",
                                        "--source",
                                        _SRC,
                                    ]
                                )
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(dest))


class TestCmdUninstall(unittest.TestCase):
    def test_removes_all_installed_launchagent_plists(self):
        labels = (COLLECT_LABEL, MENUBAR_LABEL, RESOURCE_LABEL, CCM_LABEL)
        with tempfile.TemporaryDirectory() as td:
            for label in labels:
                with open(os.path.join(td, f"{label}.plist"), "w", encoding="utf-8") as f:
                    f.write("plist")

            with mock.patch("local_ai_monitor.install_svc.LAUNCH_AGENTS", td):
                with mock.patch("local_ai_monitor.install_svc.subprocess.run"):
                    rc = cmd_uninstall([])

            self.assertEqual(rc, 0)
            for label in labels:
                self.assertFalse(os.path.exists(os.path.join(td, f"{label}.plist")))


if __name__ == "__main__":
    unittest.main()
