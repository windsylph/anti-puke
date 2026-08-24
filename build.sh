#!/usr/bin/env bash
# Build both native components and stage them into bin/, where main.py expects
# them. Run this before copying the plugin to ~/homebrew/plugins.
set -euo pipefail
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "$SCRIPT_DIR"

./sensor-service/build.sh
./overlay/build.sh

mkdir -p bin
cp sensor-service/out/motiondots-sensord bin/
cp overlay/out/motiondots-overlay bin/
chmod +x bin/motiondots-sensord bin/motiondots-overlay

echo
echo "Staged into $SCRIPT_DIR/bin:"
ls -l bin/
