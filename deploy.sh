#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/wd9337812/Bitdata.git}"
BRANCH="${BRANCH:-codex/two-stage-live-system}"
APP_DIR="${APP_DIR:-/opt/bitdata}"
APP_PORT="${APP_PORT:-8080}"
ACTION="${1:-install}"

need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_packages() {
  if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! command -v rsync >/dev/null 2>&1; then
    need_sudo apt-get update
    need_sudo apt-get install -y git curl rsync ca-certificates
  fi
}

install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
  fi
  if ! docker compose version >/dev/null 2>&1; then
    need_sudo apt-get update
    need_sudo apt-get install -y docker-compose-plugin
  fi
}

sync_repo() {
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch origin "$BRANCH"
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
  else
    need_sudo mkdir -p "$APP_DIR"
    tmp_dir="$(mktemp -d)"
    git clone --branch "$BRANCH" "$REPO_URL" "$tmp_dir"
    need_sudo rsync -a "$tmp_dir"/ "$APP_DIR"/
    rm -rf "$tmp_dir"
    if [ "$(id -u)" -ne 0 ]; then
      need_sudo chown -R "$(id -u):$(id -g)" "$APP_DIR"
    fi
  fi
}

ensure_env() {
  cd "$APP_DIR"
  mkdir -p data
  if [ ! -f .env ]; then
    cp .env.example .env
    sed -i "s/^APP_PORT=.*/APP_PORT=$APP_PORT/" .env
    echo "Created $APP_DIR/.env. Edit BASIC_AUTH_USER, BASIC_AUTH_PASSWORD and Binance API before live trading."
  fi
}

compose_up() {
  cd "$APP_DIR"
  docker compose up -d --build
}

install_maintenance_timer() {
  if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
    echo "systemd unavailable; skipped Bitdata maintenance timer installation."
    return
  fi
  need_sudo install -m 0755 "$APP_DIR/ops/bitdata-maintenance.sh" /usr/local/sbin/bitdata-maintenance
  need_sudo install -m 0644 "$APP_DIR/ops/bitdata-maintenance.service" /etc/systemd/system/bitdata-maintenance.service
  need_sudo install -m 0644 "$APP_DIR/ops/bitdata-maintenance.timer" /etc/systemd/system/bitdata-maintenance.timer
  printf 'BITDATA_APP_DIR=%s\n' "$APP_DIR" | need_sudo tee /etc/default/bitdata-maintenance >/dev/null
  need_sudo systemctl daemon-reload
  need_sudo systemctl enable --now bitdata-maintenance.timer
}

open_firewall_hint() {
  if command -v ufw >/dev/null 2>&1; then
    echo "If UFW is enabled, run: sudo ufw allow ${APP_PORT}/tcp"
  fi
}

case "$ACTION" in
  install)
    install_packages
    install_docker
    sync_repo
    ensure_env
    compose_up
    install_maintenance_timer
    open_firewall_hint
    echo "Bitdata deployed."
    echo "Dashboard: http://YOUR_VPS_IP:${APP_PORT}"
    echo "Config: $APP_DIR/.env and $APP_DIR/data/config.json"
    ;;
  update)
    sync_repo
    ensure_env
    compose_up
    install_maintenance_timer
    echo "Bitdata updated."
    ;;
  restart)
    cd "$APP_DIR"
    docker compose restart
    ;;
  stop)
    cd "$APP_DIR"
    docker compose down
    ;;
  logs)
    cd "$APP_DIR"
    docker compose logs -f --tail=200
    ;;
  status)
    cd "$APP_DIR"
    docker compose ps
    ;;
  *)
    echo "Usage: $0 {install|update|restart|stop|logs|status}" >&2
    exit 1
    ;;
esac
