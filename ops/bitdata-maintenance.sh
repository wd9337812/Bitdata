#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${BITDATA_APP_DIR:-/opt/bitdata}"
BACKUP_RETENTION_DAYS="${BITDATA_BACKUP_RETENTION_DAYS:-30}"
COMPRESS_AFTER_MINUTES="${BITDATA_COMPRESS_AFTER_MINUTES:-1440}"
DOCKER_PRUNE_HOURS="${BITDATA_DOCKER_PRUNE_HOURS:-168}"

APP_DIR="$(realpath -e "$APP_DIR")"
DATA_DIR="$APP_DIR/data"
BACKUP_DIR="$APP_DIR/backups"
ACTIVE_DB="$DATA_DIR/bitdata.db"

if [[ ! -d "$DATA_DIR" || ! -d "$BACKUP_DIR" || ! -f "$ACTIVE_DB" ]]; then
  echo "Bitdata maintenance refused: expected workspace was not found under $APP_DIR" >&2
  exit 1
fi

compress_snapshot() {
  local snapshot="$1"
  local resolved
  resolved="$(realpath -e "$snapshot")"
  if [[ "$resolved" == "$ACTIVE_DB" ]]; then
    echo "Refusing to compress active database: $resolved" >&2
    return 1
  fi
  if command -v ionice >/dev/null 2>&1; then
    ionice -c3 nice -n 19 gzip -f -- "$resolved"
  else
    nice -n 19 gzip -f -- "$resolved"
  fi
}

while IFS= read -r -d '' snapshot; do
  compress_snapshot "$snapshot"
done < <(find "$BACKUP_DIR" -type f -name '*.db' -mmin "+$COMPRESS_AFTER_MINUTES" -print0)

while IFS= read -r -d '' snapshot; do
  compress_snapshot "$snapshot"
done < <(find "$DATA_DIR" -maxdepth 1 -type f -name 'bitdata-pre-*.db' -mmin "+$COMPRESS_AFTER_MINUTES" -print0)

find "$BACKUP_DIR" -type f \( -name '*.db.gz' -o -name '*.tgz' \) \
  -mtime "+$BACKUP_RETENTION_DAYS" -delete

if command -v docker >/dev/null 2>&1; then
  docker image prune -f --filter "until=${DOCKER_PRUNE_HOURS}h" >/dev/null
  docker builder prune -f --filter "until=${DOCKER_PRUNE_HOURS}h" >/dev/null
fi

echo "Bitdata maintenance completed at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
df -h "$APP_DIR"
du -sh "$DATA_DIR" "$BACKUP_DIR"
