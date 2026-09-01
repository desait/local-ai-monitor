# AGENTS.md

## Cursor Cloud specific instructions

Runway (`local-ai-monitor`) is a **macOS** menu-bar + CLI tool that answers "does this Mac
have room for more AI work?". The Cloud VM is **Linux**, so keep these constraints in mind.

### What runs on Linux vs. what does not
- Runs on Linux: the Python package, the full unit-test suite, byte-compile checks, and the
  three CLIs (`runway`, `local-ai-monitor`, `local-ai-rm`) — the "product brain"
  (classify / policy / forecast / compose / render).
- Does **not** run on Linux (macOS-only, expected to fail/skip): live process sampling
  (`ps eww -axo …` → `error: must set personality to get -x option`), memory physics
  (`vm_stat`/`sysctl`), the native C plane in `native/` (`make -C native all`, `libproc`/`mach`),
  and the Swift menu-bar app (`menubar/scripts/build.sh`, needs `swiftc` + macOS 26). Do not try
  to build the native or Swift targets here.

### Dependencies
- Pure Python **stdlib**, no third-party runtime deps. Requires Python ≥ 3.10 (VM has 3.12).
- The update script runs `pip install -e .`, which installs console scripts to `~/.local/bin`
  (**not on PATH**). Invoke them by full path (`~/.local/bin/runway`) or, more reliably, run the
  module directly: `python3 -m local_ai_monitor …` (equivalent to `local-ai-monitor`;
  `python3 -m local_ai_monitor runway …` == `runway`; `… resource …` == `local-ai-rm`).

### Lint / test / run
- Lint: no linter is configured in the repo. Static check = `python3 -m compileall -q local_ai_monitor tests`.
- Test: `python3 -m unittest discover -s tests -q` (README/CONTRIBUTING).
- Run (dev): `python3 -m local_ai_monitor runway status` (also `watch`, `suggest`, `json`).

### Known test gotchas (pre-existing, not environment problems)
- `tests/test_rollup.py::test_c_roll_02` and `::test_c_roll_04` **fail whenever the current date is
  more than 14 days after `2026-07-29`**. They insert history at that hardcoded date, and
  `rollup_and_clear` calls `HistoryStore.prune(retention_days=14)`, which deletes the just-inserted
  rows before the assertion. This is time-bomb test brittleness, not a code or setup bug. Expect
  209/211 pass, 2 fail, 5 skip on the current VM date.
- The 5 skips are for absent optional fixtures/tools (`tmux`/`glances`, prebuilt Swift app, sample
  session JSONL) and are expected on Linux.

### Exercising the decision engine on Linux
Since sampling is macOS-only, seed the observe plane the same way the macOS samplers would, then the
real engine runs unchanged. Point `LOCAL_AI_MONITOR_STATE` at a scratch dir and write:
- `live.json` via `local_ai_monitor.store.LiveStore(state).write([SessionStats(...)], collector_pid=…, sample_interval_s=10.0)`.
- `physics.json` (fresh mtime, `< 20s` old) matching the native C-sensor schema
  (`ok`, `page_size`, `free_pages`, `free_mb`, `memsize_bytes`, …); `sample_physics()` prefers it via
  `_try_native_physics_json()`. Lower `free_pages` drives Open → Watch → Protect.
Then `LOCAL_AI_MONITOR_STATE=<dir> python3 -m local_ai_monitor runway status|json` (or `resource status`).
