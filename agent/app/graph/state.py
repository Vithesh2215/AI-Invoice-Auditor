
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _merge_lists(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    return (left or []) + (right or [])


class InvoiceState(TypedDict, total=False):
    file_path: str
    file_name: str
    checksum: str
    file_size_bytes: int
    source_format: str
    invoice_id: str
    thread_id: str
    force_reprocess: bool
    corrections: dict[str, str]
    line_corrections: dict[str, str]

    raw_text: str
    extraction_method: str
    page_count: int
    ocr_used: bool

    source_language: str
    translated_text: str
    translation_confidence: str
    translation_engine: str
    translation_cached: bool

    fields: dict[str, Any]

    data_checks: dict[str, Any]
    data_findings: list[dict[str, Any]]
    data_assessment: dict[str, Any]

    purchase_order: dict[str, Any] | None
    vendor: dict[str, Any] | None
    business_comparison: dict[str, Any]
    business_findings: list[dict[str, Any]]
    business_assessment: dict[str, Any]

    discrepancies: list[dict[str, Any]]
    validation_status: str
    recommendation: str
    recommendation_reason: str
    confidence_score: float

    narrative: dict[str, Any]
    report_json_path: str
    report_html_path: str
    report_payload: dict[str, Any]

    indexed_chunks: int

    status: str
    skipped_reason: str
    error: str
    warnings: Annotated[list[str], _merge_lists]
