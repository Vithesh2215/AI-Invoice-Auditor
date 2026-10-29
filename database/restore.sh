#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <path-to-dump-file>" >&2
    exit 1
fi
DUMP_FILE="$1"
if [ ! -f "$DUMP_FILE" ]; then
    echo "dump file not found: $DUMP_FILE" >&2
    exit 1
fi

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

: "${POSTGRES_USER:?POSTGRES_USER is required (set it in .env)}"
: "${POSTGRES_DB:?POSTGRES_DB is required (set it in .env)}"

echo "Restoring $DUMP_FILE into database '$POSTGRES_DB' ..."
docker compose exec -T postgres \
    pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --clean --if-exists --no-owner --no-privileges \
    < "$DUMP_FILE"

echo "Restore complete. If Qdrant was also lost, rebuild it with:"
echo "  curl -X POST localhost:8000/admin/reindex"
