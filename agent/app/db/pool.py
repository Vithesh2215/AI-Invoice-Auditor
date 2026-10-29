
from __future__ import annotations

import logging

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..settings import Settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def build_pool(settings: Settings) -> ConnectionPool:
    return ConnectionPool(
        open=False,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        timeout=10,
        check=ConnectionPool.check_connection,
        kwargs={
            **settings.db_connection_kwargs(),
            "autocommit": True,
            "row_factory": dict_row,
        },
    )


def open_pool(settings: Settings) -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool

    pool = build_pool(settings)
    try:
        pool.open(wait=True, timeout=30)
        with pool.connection() as conn:
            conn.execute("SELECT 1 FROM audit.invoice_audit LIMIT 0")
    except Exception as exc:
        pool.close()
        logger.error("database startup check failed", extra={"error": type(exc).__name__})
        raise RuntimeError(
            "Cannot reach PostgreSQL or the audit schema is missing. "
            "Check AGENT_DB_* settings and that 03_audit_schema.sql ran on first start."
        ) from None

    _pool = pool
    logger.info("database pool open", extra={"host": settings.agent_db_host})
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool is not open")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
