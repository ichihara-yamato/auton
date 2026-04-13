#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="${VENV_DIR:-.venv}"
MODEL_PATH="${MODEL_PATH:-./models/bonsai-8b-v0.1.gguf}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
N_CTX="${N_CTX:-8192}"
N_GPU_LAYERS="${N_GPU_LAYERS:--1}"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip

if [ "$(uname -s)" = "Darwin" ]; then
  CMAKE_ARGS="${CMAKE_ARGS:--DGGML_METAL=on}" \
    pip install "llama-cpp-python[server]" "huggingface_hub[cli]"
else
  pip install "llama-cpp-python[server]" "huggingface_hub[cli]"
fi

mkdir -p "$(dirname "$MODEL_PATH")"

python -m llama_cpp.server \
  --model "$MODEL_PATH" \
  --host "$HOST" \
  --port "$PORT" \
  --n_ctx "$N_CTX" \
  --n_gpu_layers "$N_GPU_LAYERS"
