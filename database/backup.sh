#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

: "${POSTGRES_USER:?POSTGRES_USER is required (set it in .env)}"
: "${POSTGRES_DB:?POSTGRES_DB is required (set it in .env)}"

BACKUP_DIR="database/backups"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="$BACKUP_DIR/${POSTGRES_DB}_${STAMP}.dump"

echo "Backing up database '$POSTGRES_DB' to $OUT_FILE ..."
docker compose exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --format=custom --no-owner --no-privileges \
    > "$OUT_FILE"

echo "Done: $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"
echo "Restore with: ./database/restore.sh \"$OUT_FILE\""
