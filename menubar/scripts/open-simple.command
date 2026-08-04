#!/bin/zsh
# Open friendly AI list in Terminal (not tmux / not Glances).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec local-ai-monitor simple
