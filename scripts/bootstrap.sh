#!/usr/bin/env bash
# Download Local AI Monitor from GitHub and install it locally.
set -euo pipefail

REPO="${LOCAL_AI_MONITOR_REPO:-desait/local-ai-monitor}"
REF="${LOCAL_AI_MONITOR_REF:-main}"
DEST="${LOCAL_AI_MONITOR_SOURCE_DIR:-$HOME/.local/src/local-ai-monitor}"
TMP="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

archive="$TMP/source.tar.gz"
url="https://api.github.com/repos/${REPO}/tarball/${REF}"

if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  curl -fL -H "Authorization: Bearer ${GITHUB_TOKEN}" "$url" -o "$archive"
else
  curl -fL "$url" -o "$archive"
fi
mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
mkdir -p "$DEST"
tar -xzf "$archive" -C "$TMP"
src="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d ! -path "$DEST" | head -n 1)"
rsync -a "$src/" "$DEST/"

bash "$DEST/scripts/install.sh"
"$HOME/.local/bin/local-ai-monitor" install

echo "source: $DEST"
echo "status: $HOME/.local/bin/local-ai-monitor status"
