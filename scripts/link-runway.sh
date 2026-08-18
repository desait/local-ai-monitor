#!/usr/bin/env bash
# Put `runway` on PATH. Does not replace local-ai-monitor, LaunchAgents, or ~/.local/lib.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${LOCAL_AI_MONITOR_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN"
for name in runway local-ai-monitor; do
  src="$ROOT/scripts/$name"
  if [[ ! -x "$src" ]]; then
    echo "error: missing $src" >&2
    exit 1
  fi
  ln -sfn "$src" "$BIN/$name"
  echo "linked: $BIN/$name -> $src"
done
echo "This does not replace LaunchAgents or ~/.local/lib."

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo "Add this to your shell profile, then open a new Terminal:"
    echo "  export PATH=\"$BIN:\$PATH\""
    ;;
esac

echo "Try:  runway"
echo "      runway json"
