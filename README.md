# Auton

Streamlit + Playwright + Bonsai 8B による、自然言語指示ベースのローカル Web テストツールです。
各ステップのスクリーンショットを `artifacts/` 配下へ保存できます。

## 3分クイックスタート


手順:

```bash
git clone https://github.com/ichihara-yamato/auton.git
cd auton

# --- ここでPython仮想環境とhuggingface_hub CLIのセットアップ（初回のみ） ---
# 1. Python仮想環境の作成（任意・推奨）
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# 2. pipがなければインストール
python3 -m ensurepip --upgrade || true

# 3. huggingface_hub CLI (hf) のインストール
pip install --upgrade huggingface_hub

# 4. hfコマンドが使えるか確認
hf --help

bash scripts/bootstrap_docker.sh
```

ブラウザで `http://localhost:8501` が開いたら完了です。

LLM API を外部で使う場合:

```bash
AUTON_LLM_BASE_URL="https://YOUR_OPENAI_COMPAT_ENDPOINT/v1" \
AUTON_LLM_MODEL="YOUR_MODEL_NAME" \
bash scripts/bootstrap_docker.sh
```

トラブル時の最短確認:

```bash
docker compose ps
curl -sS -H "Authorization: Bearer local" http://localhost:8000/v1/models | head
curl -sS -X POST http://localhost:3002/v1/scrape -H "Content-Type: application/json" -d '{"url":"https://example.com","formats":["markdown"],"onlyMainContent":false}' | head
```

## Files

- `app.py`: Streamlit UI、自律実行エンジン、スクリーンショット保存
- `requirements.txt`: UI 実行に必要な依存
- `scripts/run_bonsai_server.sh`: Bonsai 8B を OpenAI 互換 API として起動する補助スクリプト
- `scripts/test_bonsai_server.py`: OpenAI クライアントからの疎通確認
- `scripts/bootstrap_docker.sh`: 公開向けのワンコマンド起動（依存確認 + Docker UI 起動）
- `docker-compose.yml`: Streamlit UI コンテナ定義
- `Dockerfile`: Streamlit UI コンテナビルド定義

## GitHub 公開向けの方針

- リポジトリには「ソース + スクリプト + 設定」のみを含める
- `models/`, `build/`, `artifacts/`, `.venv/` は含めない（`.gitignore` 済み）
- 受け手は「1コマンドで起動」できる入口を用意する


## 最短起動（推奨）

```bash
bash scripts/bootstrap_docker.sh
```

このコマンドが行うこと:

1. Docker が使えるかチェック
2. Firecrawl (`http://localhost:3002`) を確認し、必要なら起動
3. LLM API (`http://localhost:8000/v1`) を確認し、条件が揃っていれば Prism llama server を起動
4. Docker で Streamlit UI をビルド・起動し、`http://localhost:8501` を開く

LLM を別サーバーで運用する場合は、起動前に環境変数を指定してください。

```bash
AUTON_LLM_BASE_URL="https://YOUR_OPENAI_COMPAT_ENDPOINT/v1" \
AUTON_LLM_MODEL="YOUR_MODEL_NAME" \
bash scripts/bootstrap_docker.sh
```

---

## 停止方法・再起動方法

### Docker で起動した場合

停止:

```bash
docker compose down
```

再起動（バックグラウンドで起動）:

```bash
docker compose up -d
```

### ローカル実行（python/streamlit）で起動した場合

停止:

- Streamlit を起動したターミナルで `Ctrl+C` を押す

再起動:

```bash
streamlit run app.py
```

---

## 1. Python 仮想環境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

## 2. UI と Playwright 依存の導入

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 3. Bonsai 8B サーバーのセットアップ

前提: `macOS / Apple Silicon`

### 重要

`prism-ml/Bonsai-8B-gguf` は `GGUF Q1_0_g128` 形式です。  
この形式は、現時点では stock の `llama-cpp-python` では読み込めず、`ggml type 41` で失敗します。

そのため実運用では、PrismML の `llama.cpp` fork が必要です。  
`app.py` は OpenAI 互換 API を叩くだけなので、バックエンドが `llama-cpp-python` でなくても `http://localhost:8000/v1` を提供できればそのまま使えます。

### llama-cpp-python の導入

```bash
source .venv/bin/activate
CMAKE_ARGS="-DGGML_METAL=on" pip install "llama-cpp-python[server]" "huggingface_hub[cli]"
```

NVIDIA GPU の Linux/Windows なら:

```bash
CMAKE_ARGS="-DGGML_CUDA=on" pip install "llama-cpp-python[server]" "huggingface_hub[cli]"
```

CPU only なら:

```bash
CMAKE_ARGS="-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS" pip install "llama-cpp-python[server]" "huggingface_hub[cli]"
```

### Bonsai 8B GGUF の取得

```bash
mkdir -p models
hf download prism-ml/Bonsai-8B-gguf "Bonsai-8B.gguf" --local-dir ./models
cp ./models/Bonsai-8B.gguf ./models/bonsai-8b-v0.1.gguf
```

## 4. PrismML llama.cpp fork のビルド

```bash
bash scripts/build_prism_llama_cpp_macos.sh
```

## 5. OpenAI 互換サーバーの起動

```bash
bash scripts/run_prism_llama_server.sh
```

または:

```bash
bash scripts/run_bonsai_server.sh
```

`scripts/run_bonsai_server.sh` は stock `llama-cpp-python` 用です。  
Bonsai 8B では失敗するため、実際には `scripts/run_prism_llama_server.sh` を使ってください。

## 6. 動作確認

```bash
python scripts/test_bonsai_server.py
```

## 7. Streamlit UI の起動

```bash
streamlit run app.py
```

UI 起動後:

1. サイドバーにログイン URL / ID / パスワードを入力
2. チャット入力に自然言語のテスト内容を入力
3. 必要ならスクリーンショット保存を有効化
4. 右側ログエリアで LLM の判断と Playwright 実行ログを確認
5. 実行後は `artifacts/` 配下にスクリーンショットが保存される

## 8. 一括セットアップ例

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
CMAKE_ARGS="-DGGML_METAL=on" pip install "llama-cpp-python[server]" "huggingface_hub[cli]"
mkdir -p models
hf download prism-ml/Bonsai-8B-gguf "Bonsai-8B.gguf" --local-dir ./models
cp ./models/Bonsai-8B.gguf ./models/bonsai-8b-v0.1.gguf
bash scripts/build_prism_llama_cpp_macos.sh
```
