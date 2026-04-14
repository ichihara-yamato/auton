#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p artifacts/service-logs .auton-pids

echo "[1/4] Docker状態を確認中..."
if ! command -v docker >/dev/null 2>&1; then
  echo "docker コマンドが見つかりません。Docker Desktop / OrbStack をインストールしてください。" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Dockerデーモンに接続できません。Docker Desktop / OrbStack を起動してください。" >&2
  exit 1
fi

echo "[2/4] Firecrawlを確認/起動中..."
if ! curl -fsS -X POST "http://localhost:3002/v1/scrape" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","formats":["markdown"],"onlyMainContent":false}' >/dev/null 2>&1; then
  bash ./scripts/run_firecrawl_server.sh
fi

# --- 追加: モデル・サーバーバイナリ自動セットアップ ---
bash ./scripts/auton_auto_setup.sh

echo "[3/4] LLM APIを確認/起動中..."
if ! curl -fsS -H "Authorization: Bearer local" "http://localhost:8000/v1/models" >/dev/null 2>&1; then
  if [[ -x ./build/prism-llama-cpp/bin/llama-server && -f ./models/bonsai-8b-v0.1.gguf ]]; then
    nohup bash ./scripts/run_prism_llama_server.sh > ./artifacts/service-logs/prism-llama.log 2>&1 &
    echo $! > ./.auton-pids/prism-llama.pid
    echo "Prism llama server をバックグラウンドで起動しました。"
  else
    cat <<'MSG'
LLM API (http://localhost:8000/v1) が未起動です。
次のどちらかを実行してください:
  A) 既存のBonsai構成を使う: bash scripts/build_prism_llama_cpp_macos.sh && bash scripts/run_prism_llama_server.sh
  B) OpenAI互換APIを別途用意し、AUTON_LLM_BASE_URL を設定する
MSG
  fi
fi

echo "[4/4] Docker UIをビルド/起動中..."
docker compose up -d --build auton-ui

echo "起動完了: http://localhost:8501"
if command -v open >/dev/null 2>&1; then
  open "http://localhost:8501" || true
fi
