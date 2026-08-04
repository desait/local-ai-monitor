# Local AI Monitor

Local AI Monitor shows which AI tools are using CPU and memory on your Mac.

It runs locally, lives in your menu bar, and helps you spot when your Mac is getting low on working room.

## Install

Copy and paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/desait/local-ai-monitor/main/scripts/bootstrap.sh | bash
```

You will see Local AI Monitor in your Mac menu bar.

Requirements: macOS, Python 3.10+, and Xcode Command Line Tools.

## What It Does

- Shows local AI tool activity from the menu bar.
- Highlights CPU, memory, swap, and available headroom.
- Recommends cleanup actions when your Mac is under pressure.
- Asks before closing or reclaiming work.

## What It Does Not Do

- It does not send your process data to a cloud service.
- It does not automatically kill active AI sessions.
- It does not replace Activity Monitor.

## Common Commands

```bash
~/.local/bin/local-ai-monitor status
~/.local/bin/local-ai-monitor uninstall
```

## For Developers

```bash
git clone https://github.com/desait/local-ai-monitor.git
cd local-ai-monitor
python3 -m unittest discover -s tests -q
make -C native all
bash menubar/scripts/build.sh
```

Config lives in `~/.config/local-ai-monitor/`.
Runtime state lives in `~/.local/state/local-ai-monitor/`.

## License

Local AI Monitor is open source under the [MIT License](LICENSE).
