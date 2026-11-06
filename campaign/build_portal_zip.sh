#!/usr/bin/env bash
# Package the stable game build for browser-game portals (Newgrounds, Game Jolt, CrazyGames Basic Launch).
# Usage: campaign/build_portal_zip.sh /path/to/ringshift-game   ->  campaign/assets/ringshift-web.zip
set -euo pipefail
GAME_DIR="${1:?path to a checkout of ringshift-game}"
OUT="$(cd "$(dirname "$0")" && pwd)/assets/ringshift-web.zip"
GAME_FILE="$(ls "$GAME_DIR"/ringshift\ v*.html | sort -V | tail -1)"
STAGE="$(mktemp -d)"
cp "$GAME_FILE" "$STAGE/index.html"
for extra in privacy.html terms.html; do [ -f "$GAME_DIR/$extra" ] && cp "$GAME_DIR/$extra" "$STAGE/"; done
rm -f "$OUT"; (cd "$STAGE" && zip -q -9 "$OUT" ./*)
echo "Built $OUT from $(basename "$GAME_FILE") (game commit $(git -C "$GAME_DIR" rev-parse --short HEAD))"
unzip -l "$OUT"
