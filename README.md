# Runway

Runway tells you whether this Mac has room for more AI work — without Terminal, tmux, or Activity Monitor.

It lives in the menu bar. It never closes a chat or session you are still using.

The install paths and background jobs stay `local-ai-monitor` so a copy already running on your laptop is not replaced by this tree.

## Install

Copy and paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/desait/local-ai-monitor/main/scripts/bootstrap.sh | bash
```

You will see **Runway** in your Mac menu bar.

Requirements: macOS, Python 3.10+, and Xcode Command Line Tools.

## What it answers

1. **Can I start more work?** Open, Watch, Hold, or Protect.
2. **What is using this Mac?** Tool names you recognize, not PIDs.
3. **What should I do next?** One step, in English.
4. **Will you close my work?** No. Active work is never closed for you.

## What it does not do

- It does not send your process data to a cloud service.
- It does not automatically kill active AI sessions.
- It does not replace Activity Monitor.
- It does not change a LaunchAgent already running from an earlier install.

## Everyday commands

From a checkout of this branch, without replacing the installed copy:

```bash
./scripts/runway
./scripts/runway refresh
./scripts/runway json
./scripts/link-runway.sh   # optional: put `runway` and `local-ai-monitor` on PATH
```

`LOCAL_AI_MONITOR_DEV=1` only changes an already-installed launcher. It does not create the `runway` command.

After linking, or after `scripts/install.sh`:

```bash
runway
runway watch
runway suggest
~/.local/bin/local-ai-monitor status
~/.local/bin/local-ai-monitor uninstall
```

`local-ai-monitor` still works. `runway` is the same product with the everyday name.

## For developers

```bash
git clone https://github.com/desait/local-ai-monitor.git
cd local-ai-monitor
python3 -m unittest discover -s tests -q
make -C native all
bash menubar/scripts/build.sh
```

Config lives in `~/.config/local-ai-monitor/`.
Runtime state lives in `~/.local/state/local-ai-monitor/`.

The product brain is `local_ai_monitor/runway/`. Physics, policy, and collect stay the observe plane; Runway only composes them.

## License

Runway (Local AI Monitor) is open source under the [MIT License](LICENSE).
