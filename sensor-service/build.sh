#!/usr/bin/env bash
# Build the sensor service. Output: out/motiondots-sensord
set -euo pipefail
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "$SCRIPT_DIR"

cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"

mkdir -p out
cp build/motiondots-sensord out/
echo "Built: $SCRIPT_DIR/out/motiondots-sensord"
