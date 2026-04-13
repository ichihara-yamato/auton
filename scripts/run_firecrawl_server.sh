#!/usr/bin/env bash
set -euo pipefail

FIRECRAWL_DIR="${FIRECRAWL_DIR:-./vendor/firecrawl}"
FIRECRAWL_REPO="${FIRECRAWL_REPO:-https://github.com/firecrawl/firecrawl.git}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker コマンドが見つかりません" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker デーモンに接続できません。Docker/OrbStack を起動してください" >&2
  exit 1
fi

if [ ! -d "$FIRECRAWL_DIR/.git" ]; then
  mkdir -p "$(dirname "$FIRECRAWL_DIR")"
  git clone --depth 1 "$FIRECRAWL_REPO" "$FIRECRAWL_DIR"
fi

COMPOSE_FILE="$FIRECRAWL_DIR/docker-compose.yaml"
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "docker-compose.yaml が見つかりません: $COMPOSE_FILE" >&2
  exit 1
fi

# 競合回避: 以前の self-host コンテナが残っている場合を掃除
docker compose -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true

stale_ids="$(docker ps -aq --filter "name=^/firecrawl-")"
if [ -n "$stale_ids" ]; then
  # shellcheck disable=SC2086
  docker rm -f $stale_ids >/dev/null 2>&1 || true
fi

docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

echo "Firecrawl self-host を起動しました"
