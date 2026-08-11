"""menubar title format + plist contract tests."""

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
    LEGACY_MENUBAR_LABELS,
    MENUBAR_LABEL,
    bootout_legacy_menubars,
    format_menubar_title,
    parse_plist_bytes,
    render_menubar_plist,
    validate_menubar_plist,
)


class TestTitleFormat(unittest.TestCase):
    def test_mb_under_1g(self):
        # 320000 kb ≈ 312.5 MB → 312M
        t = format_menubar_title(40.2, 320_000)
        self.assertTrue(t.startswith("AI 40%"), t)
        self.assertIn("·", t)
        self.assertTrue(t.endswith("M"), t)

    def test_gb(self):
        # 2 GB in kb
        t = format_menubar_title(12.4, 2 * 1024 * 1024)
        self.assertIn("AI 12%", t)
        self.assertIn("G", t)

    def test_stale_not_here(self):
        # format only — stale is Swift-side
        self.assertEqual(format_menubar_title(0, 0), "AI 0% · 0M")


class TestMenubarPlist(unittest.TestCase):
    def test_render_absolute_exec(self):
        home = "/Users/testuser"
        exe = f"{home}/.local/lib/local-ai-monitor/menubar/dist/Local AI Monitor Menu.app/Contents/MacOS/local-ai-monitor-menubar"
        state = f"{home}/.local/state/local-ai-monitor"
        xml = render_menubar_plist(
            exec_path=exe,
            state=state,
            home=home,
            template=os.path.join(
                _SRC, "share", "com.user.local-ai-monitor.menubar.plist.template"
            ),
        )
        data = parse_plist_bytes(xml)
        errs = validate_menubar_plist(data, expect_exec=exe)
        self.assertEqual(errs, [], errs)
        self.assertEqual(data["Label"], MENUBAR_LABEL)
        self.assertEqual(data["ProgramArguments"][0], exe)
        self.assertTrue(data["KeepAlive"])
        self.assertTrue(os.path.isabs(data["ProgramArguments"][0]))

    def test_info_plist_lsuielement(self):
        info = os.path.join(_SRC, "menubar", "Info.plist")
        self.assertTrue(os.path.isfile(info))
        with open(info, "rb") as f:
            data = plistlib.load(f)
        self.assertTrue(data.get("LSUIElement"))
        self.assertEqual(data.get("CFBundleIdentifier"), "com.user.local-ai-monitor.menubar")
        self.assertEqual(data.get("CFBundleExecutable"), "local-ai-monitor-menubar")

    def test_open_dash_command_exists(self):
        p = os.path.join(_SRC, "menubar", "scripts", "open-dash.command")
        self.assertTrue(os.path.isfile(p))
        with open(p, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertIn("local-ai-monitor dash", body)

    def test_app_bundle_built_or_buildable(self):
        app = os.path.join(_SRC, "menubar", "dist", "Local AI Monitor Menu.app")
        exe = os.path.join(app, "Contents", "MacOS", "local-ai-monitor-menubar")
        if not os.path.isfile(exe):
            self.skipTest("app not prebuilt; run menubar/scripts/build.sh")
        self.assertTrue(os.access(exe, os.X_OK))
        # LSUIElement in installed Info
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertTrue(info.get("LSUIElement"))

    def test_bootout_legacy_menubars_targets_known_predecessors(self):
        with mock.patch("local_ai_monitor.install_svc.subprocess.run") as run, mock.patch(
            "local_ai_monitor.install_svc.os.path.isfile", return_value=False
        ):
            touched = bootout_legacy_menubars()
        self.assertEqual(touched, [])
        calls = [" ".join(str(p) for p in c.args[0]) for c in run.call_args_list]
        for label in LEGACY_MENUBAR_LABELS:
            self.assertTrue(any(f"/{label}" in c for c in calls), calls)
            self.assertTrue(any(f"disable gui/" in c and label in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
