#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-./vendor/llama.cpp}"
BUILD_DIR="${BUILD_DIR:-./build/prism-llama-cpp}"
MODEL_PATH="${MODEL_PATH:-./models/bonsai-8b-v0.1.gguf}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
N_CTX="${N_CTX:-8192}"
N_GPU_LAYERS="${N_GPU_LAYERS:-99}"
THREADS="${THREADS:-8}"

"$BUILD_DIR/bin/llama-server" \
  -m "$MODEL_PATH" \
  --host "$HOST" \
  --port "$PORT" \
  -c "$N_CTX" \
  -ngl "$N_GPU_LAYERS" \
  -t "$THREADS"
