import os

from fastapi import Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

TABLES = ("vendors", "sku_master", "purchase_orders", "po_line_items")


def create_pool():
    if not os.getenv("ERP_DB_PASSWORD"):
        raise RuntimeError("ERP_DB_PASSWORD is required")

    return ConnectionPool(
        open=False,
        min_size=1,
        max_size=10,
        timeout=5,
        check=ConnectionPool.check_connection,
        kwargs={
            "host": os.getenv("ERP_DB_HOST", "postgres"),
            "port": int(os.getenv("ERP_DB_PORT", "5432")),
            "dbname": os.getenv("ERP_DB_NAME", "invoice_auditor"),
            "user": os.getenv("ERP_DB_USER", "erp_reader"),
            "password": os.environ["ERP_DB_PASSWORD"],
            "connect_timeout": 5,
            "options": "-c statement_timeout=5000",
            "autocommit": True,
            "row_factory": dict_row,
        },
    )

def validate_reader(connection):
    role = connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
        "FROM pg_roles WHERE rolname = current_user"
    ).fetchone()

    if not role or any(role.values()):
        raise RuntimeError("ERP requires an unprivileged database reader")

    for table in TABLES:
        rights = connection.execute(
            "SELECT has_table_privilege(current_user, %s, 'SELECT') AS readable, "
            "has_table_privilege(current_user, %s, "
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS writable",
            (f"erp.{table}", f"erp.{table}"),
        ).fetchone()

        if not rights["readable"] or rights["writable"]:
            raise RuntimeError("ERP tables require SELECT-only permissions")

    if connection.execute(
        "SELECT has_schema_privilege(current_user, 'erp', 'CREATE') AS allowed"
    ).fetchone()["allowed"]:
        raise RuntimeError("ERP reader cannot have schema creation privileges")


def get_connection(request: Request):
    with request.app.state.pool.connection() as connection, connection.transaction():
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        yield connection