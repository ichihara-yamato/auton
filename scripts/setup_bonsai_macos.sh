#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="${VENV_DIR:-.venv}"
MODEL_DIR="${MODEL_DIR:-./models}"
MODEL_FILE="${MODEL_FILE:-bonsai-8b-v0.1.gguf}"
HF_REPO="${HF_REPO:-prism-ml/Bonsai-8B-gguf}"
HF_INCLUDE="${HF_INCLUDE:-Bonsai-8B.gguf}"

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium

CMAKE_ARGS="${CMAKE_ARGS:--DGGML_METAL=on}" \
  pip install "llama-cpp-python[server]" "huggingface_hub[cli]"

mkdir -p "$MODEL_DIR"
hf download "$HF_REPO" "$HF_INCLUDE" --local-dir "$MODEL_DIR"
cp "$MODEL_DIR/$HF_INCLUDE" "$MODEL_DIR/$MODEL_FILE"

echo "Setup complete."
echo "Start the server with:"
echo "  bash scripts/run_bonsai_server.sh"
echo "Then test it with:"
echo "  source $VENV_DIR/bin/activate && python scripts/test_bonsai_server.py"
