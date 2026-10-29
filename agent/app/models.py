from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: dict[str, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class Recommendation(str, Enum):
    APPROVE = "approve"
    REVIEW = "review"
    REJECT = "reject"


class ValidationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"




class LineItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    line_number: int
    item_code: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    currency: str | None = None
    tax_rate: Decimal | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class InvoiceFields(BaseModel):
    invoice_number: str | None = None
    invoice_date: date | None = None
    vendor_name: str | None = None
    vendor_id: str | None = None
    po_number: str | None = None
    currency: str | None = None
    subtotal_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class ExtractionResult(BaseModel):
    raw_text: str
    extraction_method: str
    source_format: str
    page_count: int = 1
    ocr_used: bool = False
    warnings: list[str] = Field(default_factory=list)


class TranslationResult(BaseModel):
    translated_text: str
    source_language: str
    confidence: Decimal
    engine: str
    cached: bool = False
    notes: str | None = None


class Discrepancy(BaseModel):
    source: str = "business"
    field_name: str
    line_number: int | None = None
    invoice_value: str | None = None
    erp_value: str | None = None
    deviation_pct: Decimal | None = None
    tolerance_pct: Decimal | None = None
    severity: Severity = Severity.MEDIUM
    message: str | None = None




class FeedbackIn(BaseModel):
    field_name: str = Field(min_length=1, max_length=64)
    corrected_value: str = Field(max_length=4000)
    line_number: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=2000)
    corrected_by: str = Field(default="auditor", max_length=128)
    revalidate: bool = True


class ScanRequest(BaseModel):
    force: bool = Field(
        default=False,
        description="Reprocess files whose checksum is already in the audit trail.",
    )
    limit: int | None = Field(default=None, ge=1, le=200)


class RagQueryIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    invoice_id: UUID | None = None
    vendor: str | None = Field(default=None, max_length=255)
    source_type: str | None = Field(default=None, pattern="^(invoice|report)$")
    top_k: int | None = Field(default=None, ge=1, le=25)
    asked_by: str | None = Field(default=None, max_length=128)


class RetrievedChunk(BaseModel):
    chunk_id: str
    invoice_id: str | None = None
    invoice_number: str | None = None
    source_type: str | None = None
    vendor: str | None = None
    language: str | None = None
    score: float
    chunk_text: str


class TriadScores(BaseModel):
    context_relevance: float = 0.0
    groundedness: float = 0.0
    answer_relevance: float = 0.0
    reasoning: str | None = None
    missing_information: str | None = None

    @property
    def overall(self) -> float:
        return round(
            (self.context_relevance + self.groundedness + self.answer_relevance) / 3, 4
        )


class RagAnswer(BaseModel):
    question: str
    answer: str
    route: str
    accepted: bool
    attempts: int
    scores: TriadScores | None = None
    overall_score: float | None = None
    citations: list[RetrievedChunk] = Field(default_factory=list)
    cache_hit: bool = False
    latency_ms: int = 0
    note: str | None = None


class InvoiceSummary(BaseModel):
    invoice_id: UUID
    file_name: str
    invoice_number: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    po_number: str | None = None
    invoice_date: date | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    source_language: str | None = None
    translation_confidence: Decimal | None = None
    extraction_method: str | None = None
    processing_status: str
    validation_status: str | None = None
    recommendation: str | None = None
    confidence_score: Decimal | None = None
    discrepancy_count: int = 0
    critical_count: int = 0
    line_item_count: int = 0
    feedback_count: int = 0
    indexed_at: datetime | None = None
    processed_at: datetime
    error_message: str | None = None


class ProcessingOutcome(BaseModel):
    invoice_id: UUID | None = None
    file_name: str
    status: str
    recommendation: str | None = None
    validation_status: str | None = None
    discrepancy_count: int = 0
    detail: str | None = None


class ScanResponse(BaseModel):
    scanned: int
    processed: int
    skipped: int
    failed: int
    results: list[ProcessingOutcome] = Field(default_factory=list)


class DependencyHealth(BaseModel):
    name: str
    healthy: bool
    required: bool = True
    detail: str | None = None
    latency_ms: int | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    dependencies: list[DependencyHealth]
