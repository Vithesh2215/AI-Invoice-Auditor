
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..db import audit_repo
from .vectorstore import Chunk

if TYPE_CHECKING:
    from ..runtime import Runtime

logger = logging.getLogger(__name__)


def _splitter(runtime: "Runtime") -> RecursiveCharacterTextSplitter:
    rules = runtime.rules
    return RecursiveCharacterTextSplitter(
        chunk_size=int(rules.rag_value("chunk_size", 900)),
        chunk_overlap=int(rules.rag_value("chunk_overlap", 120)),
        separators=["\n\n", "\n", ". ", " ", ""],
        length_function=len,
    )


def render_report_text(invoice: dict[str, Any], discrepancies: list[dict[str, Any]]) -> str:
    """A prose rendering of the audit outcome.

    A question such as "why was this invoice flagged?" is answered by the
    report, not by the invoice, so the report is indexed as its own source_type
    and has to read as sentences rather than as a table.
    """
    lines = [
        f"Audit report for invoice {invoice.get('invoice_number') or 'with no invoice number'} "
        f"from vendor {invoice.get('vendor_name') or 'unknown'}.",
        f"Purchase order referenced: {invoice.get('po_number') or 'none'}.",
        f"Invoice date: {invoice.get('invoice_date') or 'not stated'}. "
        f"Currency: {invoice.get('currency') or 'not stated'}. "
        f"Total amount: {invoice.get('total_amount') if invoice.get('total_amount') is not None else 'not stated'}.",
        f"Validation status: {invoice.get('validation_status') or 'unknown'}. "
        f"Recommendation: {invoice.get('recommendation') or 'none'}.",
        f"Source language: {invoice.get('source_language') or 'unknown'}, "
        f"translation confidence {invoice.get('translation_confidence')}.",
    ]

    if invoice.get("report_summary"):
        lines.append(f"Auditor summary: {invoice['report_summary']}")

    if discrepancies:
        lines.append(f"{len(discrepancies)} discrepancies were recorded:")
        for item in discrepancies:
            deviation = item.get("deviation_pct")
            deviation_text = f" a deviation of {deviation}%," if deviation is not None else ""
            lines.append(
                f"- {item.get('severity')} severity on field '{item.get('field_name')}'"
                + (f" at line {item['line_number']}" if item.get("line_number") else "")
                + f": the invoice shows {item.get('invoice_value')} against "
                f"{item.get('erp_value')} in the ERP system,{deviation_text} "
                f"{item.get('message') or ''}".rstrip()
            )
    else:
        lines.append("No discrepancies were recorded for this invoice.")

    return "\n".join(lines)


def build_chunks(
    runtime: "Runtime", invoice: dict[str, Any], discrepancies: list[dict[str, Any]]
) -> list[Chunk]:
    splitter = _splitter(runtime)
    invoice_id = str(invoice["invoice_id"])
    vendor = invoice.get("vendor_name")
    language = invoice.get("source_language")
    invoice_number = invoice.get("invoice_number")
    po_number = invoice.get("po_number")

    chunks: list[Chunk] = []

    invoice_text = invoice.get("translated_text") or invoice.get("raw_extracted_text") or ""
    for index, piece in enumerate(splitter.split_text(invoice_text)):
        if not piece.strip():
            continue
        chunks.append(
            Chunk(
                invoice_id=invoice_id,
                chunk_index=index,
                chunk_text=piece,
                source_type="invoice",
                vendor=vendor,
                language=language,
                invoice_number=invoice_number,
                po_number=po_number,
                extra_payload={
                    "recommendation": invoice.get("recommendation"),
                    "total_amount": _as_float(invoice.get("total_amount")),
                    "currency": invoice.get("currency"),
                },
            )
        )

    report_text = render_report_text(invoice, discrepancies)
    for index, piece in enumerate(splitter.split_text(report_text)):
        if not piece.strip():
            continue
        chunks.append(
            Chunk(
                invoice_id=invoice_id,
                chunk_index=index,
                chunk_text=piece,
                source_type="report",
                vendor=vendor,
                language=language,
                invoice_number=invoice_number,
                po_number=po_number,
                extra_payload={
                    "recommendation": invoice.get("recommendation"),
                    "discrepancy_count": len(discrepancies),
                },
            )
        )

    return chunks


def index_invoice(runtime: "Runtime", invoice_id: UUID) -> int:
    invoice = audit_repo.get_invoice(invoice_id)
    if invoice is None:
        return 0

    discrepancies = audit_repo.json_safe(audit_repo.get_discrepancies(invoice_id))
    chunks = build_chunks(runtime, audit_repo.json_safe(invoice), discrepancies)
    if not chunks:
        return 0

    runtime.vectors.delete_invoice(str(invoice_id))

    vectors = runtime.embedder.embed_documents([chunk.chunk_text for chunk in chunks])
    written = runtime.vectors.upsert(chunks, vectors)

    audit_repo.update_invoice(invoice_id, indexed_at=datetime.now(timezone.utc))
    logger.info("invoice indexed", extra={"invoice_id": str(invoice_id), "chunks": written})
    return written


def rebuild_index(runtime: "Runtime", *, drop_collection: bool = True) -> dict[str, Any]:
    """Repopulate Qdrant from PostgreSQL.

    This is the recovery path after `docker compose down -v` removes the
    Qdrant volume while the PostgreSQL volume survives.
    """
    if drop_collection:
        runtime.vectors.drop()
        runtime.vectors.ensure_collection()

    rows = audit_repo.invoices_for_indexing()
    total_chunks = 0
    for row in rows:
        try:
            total_chunks += index_invoice(runtime, row["invoice_id"])
        except Exception:
            logger.exception(
                "failed to index invoice", extra={"invoice_id": str(row["invoice_id"])}
            )

    result = {
        "invoices": len(rows),
        "chunks": total_chunks,
        "collection": runtime.vectors.collection,
        "points": runtime.vectors.count(),
    }
    logger.info("rag index rebuilt", extra=result)
    return result


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
