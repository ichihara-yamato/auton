#!/usr/bin/env bash
set -euo pipefail

# モデル自動ダウンロード
MODEL_DIR="./models"
MODEL_FILE="bonsai-8b-v0.1.gguf"
HF_MODEL="prism-ml/Bonsai-8B-gguf"
HF_FILE="Bonsai-8B.gguf"

if [ ! -f "$MODEL_DIR/$MODEL_FILE" ]; then
  echo "[Auto-setup] モデルファイルがありません。Hugging Faceからダウンロードします..."
  mkdir -p "$MODEL_DIR"
  if ! command -v hf >/dev/null 2>&1; then
    echo "[Auto-setup] huggingface_hub CLI (hf) が見つかりません。pip install huggingface_hub でインストールしてください。" >&2
    exit 1
  fi
  hf download "$HF_MODEL" "$HF_FILE" --local-dir "$MODEL_DIR"
  cp "$MODEL_DIR/$HF_FILE" "$MODEL_DIR/$MODEL_FILE"
  echo "[Auto-setup] モデルダウンロード完了: $MODEL_DIR/$MODEL_FILE"
fi

# サーバーバイナリ自動ビルド
if [ ! -x "./build/prism-llama-cpp/bin/llama-server" ]; then
  echo "[Auto-setup] サーバーバイナリがありません。ビルドします..."
  bash ./scripts/build_prism_llama_cpp_macos.sh
fi
