#!/usr/bin/env bash
set -euo pipefail

: "${ERP_DB_USER:?ERP_DB_USER is required}"
: "${ERP_DB_PASSWORD:?ERP_DB_PASSWORD is required}"

psql -X -q -v ON_ERROR_STOP=1 \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v reader="$ERP_DB_USER" \
    -v password="$ERP_DB_PASSWORD" <<'SQL'
BEGIN;
CREATE ROLE :"reader"
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    PASSWORD :'password';

GRANT USAGE ON SCHEMA erp TO :"reader";
GRANT SELECT ON ALL TABLES IN SCHEMA erp TO :"reader";
ALTER ROLE :"reader" SET default_transaction_read_only = on;
COMMIT;
SQL
