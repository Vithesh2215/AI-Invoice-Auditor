
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Sequence
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ..models import Discrepancy, LineItem
from .pool import get_pool

logger = logging.getLogger(__name__)

UPDATABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "source_format",
        "extraction_method",
        "raw_extracted_text",
        "translated_text",
        "source_language",
        "translation_confidence",
        "translation_engine",
        "invoice_number",
        "vendor_name",
        "vendor_id",
        "po_number",
        "invoice_date",
        "currency",
        "subtotal_amount",
        "tax_amount",
        "total_amount",
        "processing_status",
        "validation_status",
        "recommendation",
        "confidence_score",
        "report_summary",
        "error_message",
        "extracted_raw",
        "report_json_path",
        "report_html_path",
        "thread_id",
        "indexed_at",
    }
)

_OVERVIEW_COLUMNS = """
    invoice_id, file_name, invoice_number, vendor_id, vendor_name, po_number,
    invoice_date, currency, total_amount, source_language, translation_confidence,
    extraction_method, processing_status, validation_status, recommendation,
    confidence_score, discrepancy_count, critical_count, line_item_count,
    feedback_count, indexed_at, processed_at, error_message
"""




def find_by_checksum(checksum: str) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT invoice_id, file_name, processing_status, recommendation "
            "FROM audit.invoice_audit WHERE file_checksum = %s",
            (checksum,),
        ).fetchone()


def register_file(
    *,
    file_name: str,
    file_path: str,
    checksum: str,
    size_bytes: int,
    source_format: str,
    thread_id: str,
) -> UUID:
    """Claim a file for processing, or return the existing claim.

    The unique constraint on file_checksum is what makes this safe when two
    watcher ticks race on the same file.
    """
    invoice_id = uuid4()
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO audit.invoice_audit
                (invoice_id, file_name, file_path, file_checksum, file_size_bytes,
                 source_format, processing_status, thread_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'processing', %s)
            ON CONFLICT (file_checksum) DO NOTHING
            RETURNING invoice_id
            """,
            (invoice_id, file_name, file_path, checksum, size_bytes, source_format, thread_id),
        ).fetchone()

        if row is not None:
            return row["invoice_id"]

        existing = conn.execute(
            "SELECT invoice_id FROM audit.invoice_audit WHERE file_checksum = %s",
            (checksum,),
        ).fetchone()
        return existing["invoice_id"]


def reset_for_reprocess(invoice_id: UUID, *, file_path: str, thread_id: str) -> None:
    """Clear the derived results of a previous run, keeping the identity row."""
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM audit.invoice_line_items WHERE invoice_id = %s", (invoice_id,)
        )
        conn.execute(
            "DELETE FROM audit.discrepancies WHERE invoice_id = %s", (invoice_id,)
        )
        conn.execute(
            """
            UPDATE audit.invoice_audit
               SET processing_status = 'processing',
                   validation_status = NULL,
                   recommendation    = NULL,
                   error_message     = NULL,
                   file_path         = %s,
                   thread_id         = %s,
                   updated_at        = now()
             WHERE invoice_id = %s
            """,
            (file_path, thread_id, invoice_id),
        )




def update_invoice(invoice_id: UUID, **values: Any) -> None:
    unknown = set(values) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"refusing to update non-whitelisted columns: {sorted(unknown)}")
    if not values:
        return

    params: list[Any] = []
    assignments: list[str] = []
    for column, value in values.items():
        assignments.append(f"{column} = %s")
        params.append(Jsonb(value) if column == "extracted_raw" else value)
    params.append(invoice_id)

    with get_pool().connection() as conn:
        conn.execute(
            f"UPDATE audit.invoice_audit SET {', '.join(assignments)}, updated_at = now() "
            "WHERE invoice_id = %s",
            params,
        )


def mark_failed(invoice_id: UUID, message: str) -> None:
    update_invoice(
        invoice_id,
        processing_status="failed",
        validation_status="failed",
        recommendation="review",
        error_message=message[:4000],
    )


def replace_line_items(invoice_id: UUID, items: Sequence[LineItem]) -> None:
    with get_pool().connection() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM audit.invoice_line_items WHERE invoice_id = %s", (invoice_id,)
        )
        if not items:
            return
        conn.cursor().executemany(
            """
            INSERT INTO audit.invoice_line_items
                (invoice_id, line_number, item_code, description, quantity,
                 unit_price, line_total, currency, tax_rate, extras)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    invoice_id,
                    item.line_number,
                    item.item_code,
                    item.description,
                    item.quantity,
                    item.unit_price,
                    item.line_total,
                    item.currency,
                    item.tax_rate,
                    Jsonb(item.extras or {}),
                )
                for item in items
            ],
        )


def replace_discrepancies(
    invoice_id: UUID, discrepancies: Sequence[Discrepancy], run_id: UUID | None = None
) -> UUID:
    run_id = run_id or uuid4()
    with get_pool().connection() as conn, conn.transaction():
        conn.execute("DELETE FROM audit.discrepancies WHERE invoice_id = %s", (invoice_id,))
        if discrepancies:
            conn.cursor().executemany(
                """
                INSERT INTO audit.discrepancies
                    (invoice_id, run_id, source, line_number, field_name,
                     invoice_value, erp_value, deviation_pct, tolerance_pct,
                     severity, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        invoice_id,
                        run_id,
                        d.source,
                        d.line_number,
                        d.field_name,
                        d.invoice_value,
                        d.erp_value,
                        d.deviation_pct,
                        d.tolerance_pct,
                        d.severity.value if hasattr(d.severity, "value") else d.severity,
                        d.message,
                    )
                    for d in discrepancies
                ],
            )
    return run_id


def record_event(
    *,
    stage: str,
    status: str,
    invoice_id: UUID | None = None,
    file_name: str | None = None,
    checksum: str | None = None,
    detail: str | None = None,
    duration_ms: int | None = None,
) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO audit.processing_events
                    (invoice_id, file_name, checksum, stage, status, detail, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (invoice_id, file_name, checksum, stage, status,
                 (detail or "")[:4000] or None, duration_ms),
            )
    except Exception:
        logger.warning("failed to record processing event", exc_info=True)




def list_invoices(
    *,
    recommendation: str | None = None,
    vendor_id: str | None = None,
    processing_status: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if recommendation:
        clauses.append("recommendation = %s")
        params.append(recommendation)
    if vendor_id:
        clauses.append("vendor_id = %s")
        params.append(vendor_id)
    if processing_status:
        clauses.append("processing_status = %s")
        params.append(processing_status)
    if search:
        clauses.append(
            "(invoice_number ILIKE %s OR vendor_name ILIKE %s "
            "OR po_number ILIKE %s OR file_name ILIKE %s)"
        )
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([pattern] * 4)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([min(limit, 500), max(offset, 0)])

    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT {_OVERVIEW_COLUMNS} FROM audit.invoice_overview {where} "
            "ORDER BY processed_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()


def get_invoice(invoice_id: UUID) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM audit.invoice_audit WHERE invoice_id = %s", (invoice_id,)
        ).fetchone()


def get_invoice_overview(invoice_id: UUID) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        return conn.execute(
            f"SELECT {_OVERVIEW_COLUMNS} FROM audit.invoice_overview WHERE invoice_id = %s",
            (invoice_id,),
        ).fetchone()


def get_line_items(invoice_id: UUID) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT line_number, item_code, description, quantity, unit_price, "
            "line_total, currency, tax_rate, extras FROM audit.invoice_line_items "
            "WHERE invoice_id = %s ORDER BY line_number",
            (invoice_id,),
        ).fetchall()


def get_discrepancies(invoice_id: UUID) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, source, line_number, field_name, invoice_value, erp_value, "
            "deviation_pct, tolerance_pct, severity, message, detected_at "
            "FROM audit.discrepancies WHERE invoice_id = %s "
            "ORDER BY array_position("
            "  ARRAY['critical','high','medium','low']::text[], severity), line_number NULLS FIRST, id",
            (invoice_id,),
        ).fetchall()


def get_feedback(invoice_id: UUID) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT id, line_number, field_name, original_value, corrected_value, note, "
            "corrected_by, corrected_at, revalidated, revalidated_at "
            "FROM audit.human_feedback WHERE invoice_id = %s ORDER BY corrected_at DESC",
            (invoice_id,),
        ).fetchall()


def get_events(invoice_id: UUID, limit: int = 100) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT stage, status, detail, duration_ms, created_at "
            "FROM audit.processing_events WHERE invoice_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (invoice_id, min(limit, 500)),
        ).fetchall()




def insert_feedback(
    *,
    invoice_id: UUID,
    field_name: str,
    original_value: str | None,
    corrected_value: str,
    corrected_by: str,
    line_number: int | None = None,
    note: str | None = None,
) -> int:
    """Append a correction. Never an UPDATE of the extracted value."""
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO audit.human_feedback
                (invoice_id, line_number, field_name, original_value,
                 corrected_value, note, corrected_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (invoice_id, line_number, field_name, original_value,
             corrected_value, note, corrected_by),
        ).fetchone()
        return row["id"]


def mark_feedback_revalidated(invoice_id: UUID) -> int:
    """Flag outstanding corrections as having been through validation again.

    Only the `revalidated` flag moves; the append-only trigger rejects any
    attempt to change what was actually corrected.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            "UPDATE audit.human_feedback SET revalidated = TRUE, revalidated_at = now() "
            "WHERE invoice_id = %s AND revalidated = FALSE RETURNING id",
            (invoice_id,),
        ).fetchall()
        return len(rows)


def latest_corrections(invoice_id: UUID) -> dict[str, str]:
    """Most recent correction per header field, applied on re-validation."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (field_name) field_name, corrected_value
            FROM audit.human_feedback
            WHERE invoice_id = %s AND line_number IS NULL
            ORDER BY field_name, corrected_at DESC
            """,
            (invoice_id,),
        ).fetchall()
    return {row["field_name"]: row["corrected_value"] for row in rows}


def latest_line_corrections(invoice_id: UUID) -> dict[tuple[int, str], str]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (line_number, field_name)
                   line_number, field_name, corrected_value
            FROM audit.human_feedback
            WHERE invoice_id = %s AND line_number IS NOT NULL
            ORDER BY line_number, field_name, corrected_at DESC
            """,
            (invoice_id,),
        ).fetchall()
    return {(row["line_number"], row["field_name"]): row["corrected_value"] for row in rows}




def invoices_for_indexing(only_unindexed: bool = False) -> list[dict[str, Any]]:
    """Source rows for the Qdrant index.

    The index is derived from text already in PostgreSQL, so a rebuild never
    re-reads a PDF and never repeats OCR.
    """
    clause = "AND indexed_at IS NULL" if only_unindexed else ""
    with get_pool().connection() as conn:
        return conn.execute(
            f"""
            SELECT invoice_id, invoice_number, vendor_id, vendor_name, po_number,
                   invoice_date, currency, total_amount, source_language,
                   raw_extracted_text, translated_text, report_summary,
                   validation_status, recommendation
            FROM audit.invoice_audit
            WHERE processing_status = 'completed'
              AND (raw_extracted_text IS NOT NULL OR translated_text IS NOT NULL)
              {clause}
            ORDER BY processed_at
            """
        ).fetchall()


def discrepancy_text(invoice_id: UUID) -> list[dict[str, Any]]:
    return get_discrepancies(invoice_id)




def log_rag_query(
    *,
    question: str,
    answer: str | None,
    route: str,
    invoice_id: UUID | None,
    attempts: int,
    scores: dict[str, float] | None,
    overall_score: float | None,
    accepted: bool,
    chunk_ids: Iterable[str],
    cache_hit: bool,
    latency_ms: int,
    asked_by: str | None,
) -> None:
    scores = scores or {}
    try:
        with get_pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO audit.rag_queries
                    (question, answer, route, invoice_id, attempts, answer_relevance,
                     groundedness, context_relevance, overall_score, accepted,
                     retrieved_chunk_ids, cache_hit, latency_ms, asked_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    question,
                    answer,
                    route,
                    invoice_id,
                    attempts,
                    scores.get("answer_relevance"),
                    scores.get("groundedness"),
                    scores.get("context_relevance"),
                    overall_score,
                    accepted,
                    Jsonb(list(chunk_ids)),
                    cache_hit,
                    latency_ms,
                    asked_by,
                ),
            )
    except Exception:
        logger.warning("failed to log rag query", exc_info=True)


def recent_rag_queries(limit: int = 25) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT question, answer, route, attempts, overall_score, accepted, "
            "cache_hit, latency_ms, asked_at FROM audit.rag_queries "
            "ORDER BY asked_at DESC LIMIT %s",
            (min(limit, 200),),
        ).fetchall()




def counts_by_recommendation() -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT COALESCE(recommendation, 'not_assessed') AS recommendation, "
            "count(*) AS invoices, COALESCE(sum(total_amount), 0) AS total_value "
            "FROM audit.invoice_audit GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()


def counts_by_vendor() -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            """
            SELECT COALESCE(i.vendor_name, 'unknown') AS vendor_name,
                   i.vendor_id,
                   count(*) AS invoices,
                   COALESCE(sum(i.total_amount), 0) AS total_value,
                   count(*) FILTER (WHERE i.recommendation = 'reject') AS rejected,
                   count(*) FILTER (WHERE i.recommendation = 'review') AS in_review
            FROM audit.invoice_audit i
            GROUP BY 1, 2 ORDER BY invoices DESC LIMIT 50
            """
        ).fetchall()


def top_discrepancy_fields() -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT field_name, severity, count(*) AS occurrences "
            "FROM audit.discrepancies GROUP BY 1, 2 ORDER BY occurrences DESC LIMIT 25"
        ).fetchall()


def portfolio_totals() -> dict[str, Any]:
    with get_pool().connection() as conn:
        return conn.execute(
            """
            SELECT count(*) AS invoices,
                   count(*) FILTER (WHERE processing_status = 'completed') AS completed,
                   count(*) FILTER (WHERE processing_status = 'failed') AS failed,
                   count(*) FILTER (WHERE indexed_at IS NOT NULL) AS indexed,
                   count(DISTINCT vendor_id) AS vendors,
                   COALESCE(sum(total_amount), 0) AS total_value,
                   (SELECT count(*) FROM audit.discrepancies) AS discrepancies,
                   (SELECT count(*) FROM audit.human_feedback) AS corrections
            FROM audit.invoice_audit
            """
        ).fetchone()


def invoices_for_vendor(vendor_term: str) -> list[dict[str, Any]]:
    pattern = "%" + vendor_term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT invoice_number, vendor_name, po_number, invoice_date, currency, "
            "total_amount, recommendation, validation_status FROM audit.invoice_audit "
            "WHERE vendor_name ILIKE %s OR vendor_id ILIKE %s "
            "ORDER BY invoice_date DESC NULLS LAST LIMIT 50",
            (pattern, pattern),
        ).fetchall()


def invoice_by_number(invoice_number: str) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM audit.invoice_overview WHERE invoice_number = %s "
            "ORDER BY processed_at DESC LIMIT 1",
            (invoice_number,),
        ).fetchone()


def json_safe(value: Any) -> Any:
    """Make psycopg rows JSON serialisable without losing decimal precision."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(json_safe(value), **kwargs)
