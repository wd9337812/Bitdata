#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/binance-strategy}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is required. Please install docker-compose-plugin." >&2
  exit 1
fi

sudo mkdir -p "$APP_DIR"
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude 'data/config.json' \
  "$REPO_DIR"/ "$APP_DIR"/

cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
fi
mkdir -p data
docker compose up -d --build

echo "Dashboard: http://YOUR_VPS_IP:8080"
