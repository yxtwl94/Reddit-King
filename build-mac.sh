#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export UV_CACHE_DIR="$ROOT/.uv-cache"

echo "[1/3] Installing locked build and runtime dependencies..."
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c "import tkinter; print('Tkinter OK')"
uv sync --frozen --python "$PYTHON_BIN"

echo "[2/3] Building dist/Reddit-King.app..."
uv run pyinstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name "Reddit-King" \
  --distpath "$ROOT/dist" \
  --workpath "$ROOT/build" \
  --specpath "$ROOT/build" \
  --collect-all bs4 \
  --collect-all certifi \
  "$ROOT/launcher.py"

APP="$ROOT/dist/Reddit-King.app"
if [[ ! -d "$APP" ]]; then
  echo "[ERROR] Build finished but Reddit-King.app was not found."
  exit 1
fi

echo "[3/3] Creating ZIP and DMG..."
ditto -c -k --sequesterRsrc --keepParent \
  "$APP" "$ROOT/dist/Reddit-King-macOS.zip"

STAGING="$ROOT/build/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/Reddit-King.app"
ln -s /Applications "$STAGING/Applications"
hdiutil create \
  -volname "Reddit King" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  "$ROOT/dist/Reddit-King.dmg"
rm -rf "$STAGING"

echo "[OK] Built:"
echo "  $APP"
echo "  $ROOT/dist/Reddit-King-macOS.zip"
echo "  $ROOT/dist/Reddit-King.dmg"
