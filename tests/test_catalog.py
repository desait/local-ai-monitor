#!/usr/bin/env python3
"""Catalog discovery, prefs, Codex classification, session roots."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from local_ai_monitor.catalog import (  # noqa: 
    KNOWN_TOOLS,
    classify_with_catalog,
    discover_installed,
    load_tool_prefs,
    save_tool_prefs,
    set_hidden,
    tool_def,
    visible_tool_ids,
)
from local_ai_monitor.classify import classify_direct  # noqa: 
from local_ai_monitor.sessionize import (  # noqa: 
    Sessionizer,
    is_session_root,
    make_proc,
    sessionize_all,
)


class TestCatalogRegistry(unittest.TestCase):
    def test_codex_in_known(self):
        ids = {t.id for t in KNOWN_TOOLS}
        self.assertIn("Codex", ids)
        self.assertIn("OpenAI CLI", ids)
        self.assertIn("Cursor", ids)
        self.assertIn("Grok", ids)

    def test_discover_includes_codex_when_bin_present(self):
        # This machine has ~/.local/bin/codex (install footprint)
        codex = tool_def("Codex")
        self.assertIsNotNone(codex)
        found = discover_installed()
        # If codex binary exists, must appear; if not, skip soft
        home_bin = os.path.expanduser("~/.local/bin/codex")
        if os.path.isfile(home_bin) or os.path.islink(home_bin):
            self.assertIn("Codex", found)


class TestClassifyCodex(unittest.TestCase):
    def test_codex_binary(self):
        p = make_proc(1, 0, "/Users/u/.local/bin/codex --help")
        self.assertEqual(classify_direct(p), "Codex")

    def test_codex_standalone_path(self):
        p = make_proc(
            1,
            0,
            "/Users/u/.codex/packages/standalone/current/bin/codex serve",
        )
        self.assertEqual(classify_direct(p), "Codex")

    def test_codex_not_folder_noise(self):
        # PATH noise like codex.system must not match
        p = make_proc(
            1,
            0,
            "/usr/bin/python3 -m http.server "
            "PATH=/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/bin",
        )
        self.assertNotEqual(classify_direct(p), "Codex")

    def test_buzz_wins_over_embedded_codex(self):
        p = make_proc(
            1,
            0,
            "node /Users/u/Library/Application Support/Buzz/node-tools/"
            "lib/node_modules/@openai/codex/bin.js "
            "PWD=/Users/u/.buzz/PROJECTS/buzz-foo "
            "BUZZ_MANAGED_AGENT=xyz.block.buzz.app "
            "XPC_SERVICE_NAME=com.buzz.foo",
        )
        self.assertEqual(classify_direct(p), "Buzz")

    def test_grok_projects_folder_not_grok(self):
        p = make_proc(
            1,
            0,
            "/usr/bin/python3 script.py PWD=/Users/u/Projects/foo",
        )
        self.assertNotEqual(classify_direct(p), "Grok")


class TestToolPrefs(unittest.TestCase):
    def test_hide_show_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools.json")
            prefs = set_hidden("Codex", True, path=path)
            self.assertIn("Codex", prefs.hidden)
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.assertIn("Codex", raw["hidden"])
            prefs2 = set_hidden("Codex", False, path=path)
            self.assertNotIn("Codex", prefs2.hidden)
            vis = visible_tool_ids(
                running=["Codex", "Grok"],
                installed=["Codex", "Grok", "Buzz"],
                prefs=load_tool_prefs(path),
            )
            self.assertIn("Codex", vis)
            set_hidden("Buzz", True, path=path)
            vis2 = visible_tool_ids(
                running=["Codex", "Grok", "Buzz"],
                installed=["Codex", "Grok", "Buzz"],
                prefs=load_tool_prefs(path),
            )
            self.assertNotIn("Buzz", vis2)
            self.assertIn("Codex", vis2)

    def test_idle_off_hides_quiet_installed(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools.json")
            from local_ai_monitor.catalog import ToolPrefs

            prefs = ToolPrefs(show_idle_installed=False)
            save_tool_prefs(prefs, path)
            vis = visible_tool_ids(
                running=["Grok"],
                installed=["Codex", "Grok"],
                prefs=load_tool_prefs(path),
            )
            self.assertEqual(vis, ["Grok"])


class TestCodexSessionRoot(unittest.TestCase):
    def test_codex_is_session_root_and_emits_row(self):
        p = make_proc(42, 1, "/Users/u/.local/bin/codex", pcpu=2.0, rss_kb=5000)
        p.direct_app = classify_direct(p)
        p.app = p.direct_app
        self.assertEqual(p.app, "Codex")
        self.assertTrue(is_session_root(p))
        sz = Sessionizer(start_unix_fn=lambda pid: 99, lsof_path_overrides={42: []})
        sessions = sessionize_all([p], sessionizer=sz, skip_lsof=True)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].app, "Codex")
        self.assertEqual(sessions[0].session_id, "pid:42:start:99")


class TestClassifyCatalogHelper(unittest.TestCase):
    def test_openai_cli_binary(self):
        self.assertEqual(
            classify_with_catalog(
                "/opt/homebrew/bin/openai api models.list",
                "openai",
                "/opt/homebrew/bin/openai",
            ),
            "OpenAI CLI",
        )

    def test_cursor_app(self):
        self.assertEqual(
            classify_with_catalog(
                "/Applications/Cursor.app/Contents/MacOS/Cursor",
                "Cursor",
                "/Applications/Cursor.app/Contents/MacOS/Cursor",
            ),
            "Cursor",
        )


if __name__ == "__main__":
    unittest.main()
