#!/usr/bin/env bash
# Install Local AI Monitor package → ~/.local/lib/local-ai-monitor and launchers → ~/.local/bin.
# Portable: no hardcoded developer machine paths.
set -euo pipefail

# Resolve source tree (this script lives in <repo>/scripts/install.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${LOCAL_AI_MONITOR_SRC:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB="${LOCAL_AI_MONITOR_LIB:-${HOME}/.local/lib/local-ai-monitor}"
BIN="${LOCAL_AI_MONITOR_BIN:-${HOME}/.local/bin}"

if [[ ! -d "$SOURCE/local_ai_monitor" ]]; then
  echo "error: source package not found at $SOURCE/local_ai_monitor" >&2
  echo "set LOCAL_AI_MONITOR_SRC to the local-ai-monitor repository root" >&2
  exit 1
fi

resolve_python3() {
  local c
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [[ -x "$c" ]]; then
      realpath "$c" 2>/dev/null || echo "$c"
      return
    fi
  done
  c="$(command -v python3 || true)"
  if [[ -n "$c" && -x "$c" ]]; then
    realpath "$c" 2>/dev/null || echo "$c"
    return
  fi
  echo "/usr/bin/python3"
}

PYTHON="$(resolve_python3)"

mkdir -p "$LIB" "$BIN"

rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$SOURCE/local_ai_monitor/" "$LIB/local_ai_monitor/"

if [[ -d "$SOURCE/share" ]]; then
  rsync -a --delete "$SOURCE/share/" "$LIB/share/"
fi

if [[ -d "$SOURCE/menubar" ]]; then
  mkdir -p "$LIB/menubar"
  rsync -a --delete \
    --exclude '.build' \
    --exclude 'DerivedData' \
    "$SOURCE/menubar/" "$LIB/menubar/"
fi

# Native plane (best-effort)
if [[ -f "$SOURCE/native/Makefile" ]] && command -v make >/dev/null 2>&1; then
  make -C "$SOURCE/native" all 2>/dev/null || make -C "$SOURCE/native" dist/local-ai-monitord 2>/dev/null || true
  if [[ -x "$SOURCE/native/dist/local-ai-monitord" ]]; then
    cp -f "$SOURCE/native/dist/local-ai-monitord" "$BIN/local-ai-monitord"
    chmod +x "$BIN/local-ai-monitord"
  fi
  for b in local-ai-monitor-sensor local-ai-monitor-appscan local-ai-monitor-litebar; do
    if [[ -x "$SOURCE/native/dist/$b" ]]; then
      cp -f "$SOURCE/native/dist/$b" "$BIN/$b"
      chmod +x "$BIN/$b"
    fi
  done
fi

# Thin launchers
cat > "$BIN/local-ai-monitor" <<EOF
#!/usr/bin/env bash
set -euo pipefail
LIB="${LIB}"
SOURCE="${SOURCE}"
PYTHON="${PYTHON}"
if [[ "\${LOCAL_AI_MONITOR_DEV:-0}" == "1" ]]; then
  export LOCAL_AI_MONITOR_SRC="\${SOURCE}"
  export PYTHONPATH="\${SOURCE}\${PYTHONPATH:+:\$PYTHONPATH}"
else
  export LOCAL_AI_MONITOR_SRC="\${SOURCE}"
  export PYTHONPATH="\${LIB}\${PYTHONPATH:+:\$PYTHONPATH}"
fi
exec "\$PYTHON" -m local_ai_monitor "\$@"
EOF
chmod +x "$BIN/local-ai-monitor"

cat > "$BIN/local-ai-rm" <<EOF
#!/usr/bin/env bash
set -euo pipefail
LIB="${LIB}"
SOURCE="${SOURCE}"
PYTHON="${PYTHON}"
if [[ "\${LOCAL_AI_MONITOR_DEV:-0}" == "1" ]]; then
  export LOCAL_AI_MONITOR_SRC="\${SOURCE}"
  export PYTHONPATH="\${SOURCE}\${PYTHONPATH:+:\$PYTHONPATH}"
else
  export LOCAL_AI_MONITOR_SRC="\${SOURCE}"
  export PYTHONPATH="\${LIB}\${PYTHONPATH:+:\$PYTHONPATH}"
fi
exec "\$PYTHON" -m local_ai_monitor resource "\$@"
EOF
chmod +x "$BIN/local-ai-rm"

echo "installed: $BIN/local-ai-monitor"
echo "           $BIN/local-ai-rm"
echo "  package: $LIB/local_ai_monitor"
echo "  python:  $PYTHON"
echo "  next:    $BIN/local-ai-monitor install    # start menu bar + background monitor"
echo "  dev:     LOCAL_AI_MONITOR_DEV=1 uses live source at $SOURCE"
