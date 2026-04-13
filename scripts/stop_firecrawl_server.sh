#!/usr/bin/env bash
set -euo pipefail

FIRECRAWL_DIR="${FIRECRAWL_DIR:-./vendor/firecrawl}"
COMPOSE_FILE="$FIRECRAWL_DIR/docker-compose.yaml"

if command -v docker >/dev/null 2>&1 && [ -f "$COMPOSE_FILE" ]; then
  docker compose -f "$COMPOSE_FILE" down || true
fi

echo "Firecrawl self-host を停止しました"
