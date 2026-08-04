#!/usr/bin/env bash
# Build Local AI Monitor Menu.app (LSUIElement MenuBarExtra) into menubar/dist/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/Sources/LocalAIMonitorMenu/main.swift"
DIST="${LOCAL_AI_MONITOR_MENUBAR_DIST:-$ROOT/dist}"
APP="$DIST/Local AI Monitor Menu.app"
BIN="$APP/Contents/MacOS/local-ai-monitor-menubar"
RES="$APP/Contents/Resources"
# Liquid Glass APIs need macOS Tahoe 26+
MIN_MACOS="${LOCAL_AI_MONITOR_MIN_MACOS:-26.0}"

if [[ ! -f "$SRC" ]]; then
  echo "error: missing $SRC" >&2
  exit 1
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "error: swiftc not found" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

# Prefer arm64 native; fall back to host default
ARCH_FLAGS=()
if [[ "$(uname -m)" == "arm64" ]]; then
  ARCH_FLAGS=(-target "arm64-apple-macosx${MIN_MACOS}")
else
  ARCH_FLAGS=(-target "x86_64-apple-macosx${MIN_MACOS}")
fi

echo "building: $BIN"
swiftc -O \
  "${ARCH_FLAGS[@]}" \
  -parse-as-library \
  -o "$BIN" \
  "$SRC"

# Frameworks for SwiftUI/AppKit linked automatically by swiftc on Apple platforms

cp "$ROOT/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/scripts/open-dash.command" "$RES/open-dash.command"
chmod +x "$RES/open-dash.command"
if [[ -f "$ROOT/scripts/open-simple.command" ]]; then
  cp "$ROOT/scripts/open-simple.command" "$RES/open-simple.command"
  chmod +x "$RES/open-simple.command"
fi
chmod +x "$BIN"

# PkgInfo
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Ad-hoc sign (optional; helps Gatekeeper local runs)
if command -v codesign >/dev/null 2>&1; then
  codesign -s - --force --deep "$APP" 2>/dev/null || true
fi

# Clear quarantine if present
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
fi

echo "built: $APP"
ls -la "$BIN"
file "$BIN"
