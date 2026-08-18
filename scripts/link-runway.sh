#!/usr/bin/env bash
# Put `runway` on PATH. Does not replace local-ai-monitor, LaunchAgents, or ~/.local/lib.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${LOCAL_AI_MONITOR_BIN:-$HOME/.local/bin}"
SRC="$ROOT/scripts/runway"

if [[ ! -x "$SRC" ]]; then
  echo "error: missing $SRC" >&2
  exit 1
fi

mkdir -p "$BIN"
ln -sfn "$SRC" "$BIN/runway"
echo "linked: $BIN/runway -> $SRC"
echo "This does not replace local-ai-monitor or the menu bar."

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo "Add this to your shell profile, then open a new Terminal:"
    echo "  export PATH=\"$BIN:\$PATH\""
    ;;
esac

echo "Try:  runway"
echo "      runway json"
