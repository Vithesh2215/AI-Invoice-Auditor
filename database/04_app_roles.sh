#!/usr/bin/env bash
set -euo pipefail

: "${AGENT_DB_USER:?AGENT_DB_USER is required}"
: "${AGENT_DB_PASSWORD:?AGENT_DB_PASSWORD is required}"
: "${UI_DB_USER:?UI_DB_USER is required}"
: "${UI_DB_PASSWORD:?UI_DB_PASSWORD is required}"

psql -X -q -v ON_ERROR_STOP=1 \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v agent="$AGENT_DB_USER" \
    -v agent_password="$AGENT_DB_PASSWORD" \
    -v ui="$UI_DB_USER" \
    -v ui_password="$UI_DB_PASSWORD" <<'SQL'
BEGIN;

-- ---------------------------------------------------------------- agent role
CREATE ROLE :"agent"
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'agent_password';

GRANT CONNECT ON DATABASE :"DBNAME" TO :"agent";

GRANT USAGE ON SCHEMA audit TO :"agent";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA audit TO :"agent";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA audit TO :"agent";
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"agent";
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
    GRANT USAGE, SELECT ON SEQUENCES TO :"agent";

-- The LangGraph checkpointer creates its own tables on first connect, so the
-- agent must be able to create objects inside this schema and nowhere else.
ALTER SCHEMA langgraph OWNER TO :"agent";
GRANT USAGE, CREATE ON SCHEMA langgraph TO :"agent";

-- Explicit, even though PUBLIC has no privileges on a named schema by default.
REVOKE ALL ON SCHEMA erp FROM :"agent";
REVOKE ALL ON ALL TABLES IN SCHEMA erp FROM :"agent";

-- ------------------------------------------------------------------- ui role
CREATE ROLE :"ui"
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'ui_password';

GRANT CONNECT ON DATABASE :"DBNAME" TO :"ui";

GRANT USAGE ON SCHEMA audit TO :"ui";
GRANT SELECT ON ALL TABLES IN SCHEMA audit TO :"ui";
ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT ON TABLES TO :"ui";

REVOKE ALL ON SCHEMA erp FROM :"ui";
REVOKE ALL ON ALL TABLES IN SCHEMA erp FROM :"ui";
REVOKE ALL ON SCHEMA langgraph FROM :"ui";

-- Any write from the dashboard must go through the agent API, so make the
-- connection itself read-only.
ALTER ROLE :"ui" SET default_transaction_read_only = on;
ALTER ROLE :"ui" SET statement_timeout = '15s';

COMMIT;
SQL

echo "04_app_roles.sh: created roles '$AGENT_DB_USER' (audit rw) and '$UI_DB_USER' (audit ro)"
