#!/bin/bash
# Build Boox Display Digitizer server into a macOS .app bundle + DMG.
# Usage: cd server && ./build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Boox Display Digitizer"
BUNDLE_ID="com.boox.display-digitizer"
ARCH="$(uname -m)"

# Ensure venv
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt pyinstaller

# Build .app bundle
pyinstaller \
    --name "$APP_NAME" \
    --windowed \
    --onedir \
    --noconfirm \
    --clean \
    --osx-bundle-identifier "$BUNDLE_ID" \
    server.py

# Create DMG with Applications symlink (drag-to-install)
DMG_DIR="dmg-stage"
rm -rf "$DMG_DIR"
mkdir -p "$DMG_DIR"
cp -R "dist/$APP_NAME.app" "$DMG_DIR/"
ln -s /Applications "$DMG_DIR/Applications"

DMG_NAME="BooxDisplayDigitizer-${ARCH}.dmg"
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_DIR" \
    -ov -format UDZO \
    "dist/$DMG_NAME"

echo ""
echo "Built: dist/$DMG_NAME"
echo "Arch:  $ARCH"
