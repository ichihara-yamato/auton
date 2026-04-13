#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-./vendor/llama.cpp}"
REPO_URL="${REPO_URL:-https://github.com/PrismML-Eng/llama.cpp.git}"
BUILD_DIR="${BUILD_DIR:-./build/prism-llama-cpp}"
UPDATE_REPO="${UPDATE_REPO:-0}"

mkdir -p "$(dirname "$REPO_DIR")"

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "$REPO_URL" "$REPO_DIR"
elif [ "$UPDATE_REPO" = "1" ]; then
  git -C "$REPO_DIR" pull --ff-only
fi

mkdir -p "$BUILD_DIR"
cmake -S "$REPO_DIR" -B "$BUILD_DIR"
cmake --build "$BUILD_DIR" --target llama-server -j

echo "Build complete: $BUILD_DIR/bin/llama-server"
