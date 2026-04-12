#!/bin/bash
# Build boox-bridge server into a standalone macOS executable.
# Usage: cd server && ./build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure venv
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt pyinstaller

# Build
pyinstaller \
    --name "boox-bridge-server" \
    --onefile \
    --console \
    --noconfirm \
    --clean \
    server.py

echo ""
echo "Built: dist/boox-bridge-server"
echo "Arch:  $(file dist/boox-bridge-server)"
