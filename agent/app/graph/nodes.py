
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..db import audit_repo
from ..extraction import ExtractionError, checksum_file, detect_format, extract
from ..extraction.parser import parse_amount, parse_date, parse_invoice
from ..models import (
    SEVERITY_ORDER,
    Discrepancy,
    InvoiceFields,
    LineItem,
    Recommendation,
    Severity,
    ValidationStatus,
)
from ..services.cache import cache_key
from ..services.erp_client import ERPUnavailable
from ..services.indexing import index_invoice
from ..services.llm import LLMUnavailable
from .state import InvoiceState

if TYPE_CHECKING:
    from ..runtime import Runtime

logger = logging.getLogger(__name__)

ENGLISH = "en"
_PROMPT_FENCE_RE = re.compile(
    r"^[ \t]*-{2,}[ \t]*(?:BEGIN|END)[ \t]+INVOICE[ \t]+TEXT[ \t]*-{2,}[ \t]*$\n?",
    re.IGNORECASE | re.MULTILINE,
)
HEADER_CORRECTABLE = {
    "invoice_number", "vendor_name", "vendor_id", "po_number",
    "invoice_date", "currency", "subtotal_amount", "tax_amount", "total_amount",
}


class InvoiceNodes:
    def __init__(self, runtime: "Runtime") -> None:
        self.rt = runtime

    def monitor(self, state: InvoiceState) -> dict[str, Any]:
        """Detect, checksum, de-duplicate and route."""
        started = time.perf_counter()
        path = Path(state["file_path"])

        if not path.is_file():
            return {"status": "failed", "error": f"file no longer exists: {path}"}

        checksum = checksum_file(path)
        source_format = detect_format(path)
        size_bytes = path.stat().st_size

        existing = audit_repo.find_by_checksum(checksum)
        force = bool(state.get("force_reprocess"))

        if existing and not force and existing["processing_status"] == "completed":
            audit_repo.record_event(
                stage="monitor",
                status="skipped",
                invoice_id=existing["invoice_id"],
                file_name=path.name,
                checksum=checksum,
                detail="checksum already present in the audit trail",
            )
            return {
                "invoice_id": str(existing["invoice_id"]),
                "checksum": checksum,
                "status": "skipped",
                "skipped_reason": (
                    f"already audited as {existing['file_name']} "
                    f"({existing['recommendation'] or 'no recommendation'})"
                ),
            }

        thread_id = state.get("thread_id") or f"invoice:{checksum[:16]}"
        invoice_id = audit_repo.register_file(
            file_name=path.name,
            file_path=str(path),
            checksum=checksum,
            size_bytes=size_bytes,
            source_format=source_format,
            thread_id=thread_id,
        )

        if existing or force:
            audit_repo.reset_for_reprocess(
                invoice_id, file_path=str(path), thread_id=thread_id
            )

        audit_repo.record_event(
            stage="monitor",
            status="ok",
            invoice_id=invoice_id,
            file_name=path.name,
            checksum=checksum,
            detail=f"routed as {source_format}",
            duration_ms=_ms(started),
        )
        logger.info(
            "invoice detected",
            extra={"file": path.name, "format": source_format, "invoice_id": str(invoice_id)},
        )

        return {
            "invoice_id": str(invoice_id),
            "file_name": path.name,
            "checksum": checksum,
            "file_size_bytes": size_bytes,
            "source_format": source_format,
            "thread_id": thread_id,
            "status": "processing",
        }

    def extractor(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        invoice_id = UUID(state["invoice_id"])
        path = Path(state["file_path"])

        try:
            result = extract(path, self.rt.settings)
        except (ExtractionError, Exception) as exc:
            message = f"{type(exc).__name__}: {exc}"
            audit_repo.mark_failed(invoice_id, message)
            audit_repo.record_event(
                stage="extract", status="error", invoice_id=invoice_id, detail=message
            )
            logger.error("extraction failed", extra={"file": path.name, "error": message})
            return {"status": "failed", "error": message}

        fields = parse_invoice(
            result.raw_text, currency_symbol_map=self.rt.rules.currency_symbol_map
        )
        fields = self._apply_corrections(fields, state)

        audit_repo.update_invoice(
            invoice_id,
            raw_extracted_text=result.raw_text,
            extraction_method=result.extraction_method,
            source_format=result.source_format,
        )
        audit_repo.record_event(
            stage="extract",
            status="ok",
            invoice_id=invoice_id,
            detail=f"{result.extraction_method}, {len(result.raw_text)} chars",
            duration_ms=_ms(started),
        )

        return {
            "raw_text": result.raw_text,
            "extraction_method": result.extraction_method,
            "source_format": result.source_format,
            "page_count": result.page_count,
            "ocr_used": result.ocr_used,
            "fields": fields.model_dump(mode="json"),
            "warnings": result.warnings,
        }

    def translator(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        invoice_id = UUID(state["invoice_id"])
        raw_text = state.get("raw_text") or ""

        language = _detect_language(raw_text)

        if language == ENGLISH or not raw_text.strip():
            update = {
                "source_language": language,
                "translated_text": raw_text,
                "translation_confidence": "1.0",
                "translation_engine": "none (already English)",
                "translation_cached": False,
            }
            self._persist_translation(invoice_id, update)
            audit_repo.record_event(
                stage="translate", status="ok", invoice_id=invoice_id,
                detail="English source, no translation required", duration_ms=_ms(started),
            )
            return update

        key = cache_key("translation", self.rt.llm.model_for("translation"), raw_text)
        cached = self.rt.cache.get_json(key)
        if cached:
            update = {**cached, "translation_cached": True}
            self._persist_translation(invoice_id, update)
            audit_repo.record_event(
                stage="translate", status="ok", invoice_id=invoice_id,
                detail="served from the translation cache", duration_ms=_ms(started),
            )
            return update

        persona = self.rt.prompts.persona("translator")
        prompt = self.rt.prompts.render(
            "translate",
            detected_language=language,
            source_format=state.get("source_format", "unknown"),
            extraction_method=state.get("extraction_method", "unknown"),
            text=raw_text[:20000],
        )

        try:
            payload = self.rt.llm.complete_json(persona, prompt)
        except LLMUnavailable as exc:
            logger.warning("translation unavailable", extra={"error": str(exc)[:160]})
            update = {
                "source_language": language,
                "translated_text": raw_text,
                "translation_confidence": "0.0",
                "translation_engine": f"unavailable ({type(exc).__name__})",
                "translation_cached": False,
                "warnings": [f"translation failed: {exc}"],
            }
            self._persist_translation(invoice_id, update)
            audit_repo.record_event(
                stage="translate", status="error", invoice_id=invoice_id, detail=str(exc)[:500]
            )
            return update

        update = {
            "source_language": str(payload.get("source_language") or language)[:8],
            "translated_text": _strip_prompt_delimiters(
                str(payload.get("translated_text") or raw_text)
            ),
            "translation_confidence": str(_clamp01(payload.get("confidence"), default="0.5")),
            "translation_engine": self.rt.llm.model_for("translation"),
            "translation_cached": False,
        }
        self.rt.cache.set_json(
            key,
            {k: v for k, v in update.items() if k != "translation_cached"},
            self.rt.settings.translation_cache_ttl_seconds,
        )
        self._persist_translation(invoice_id, update)
        audit_repo.record_event(
            stage="translate", status="ok", invoice_id=invoice_id,
            detail=f"{update['source_language']} -> en at confidence "
                   f"{update['translation_confidence']}",
            duration_ms=_ms(started),
        )
        return update

    def data_validator(self, state: InvoiceState) -> dict[str, Any]:
        from ..validation import run_data_checks

        started = time.perf_counter()
        invoice_id = UUID(state["invoice_id"])
        fields = InvoiceFields.model_validate(state["fields"])

        checks, findings = run_data_checks(fields, self.rt.rules)

        assessment: dict[str, Any] = {}
        persona = self.rt.prompts.persona("data_validator")
        try:
            assessment = self.rt.llm.complete_json(
                persona,
                self.rt.prompts.render(
                    "validate_data",
                    file_name=state.get("file_name", ""),
                    fields_json=audit_repo.dumps(
                        fields.model_dump(mode="json", exclude={"line_items"}), indent=2
                    ),
                    line_count=len(fields.line_items),
                    line_items_json=audit_repo.dumps(
                        [i.model_dump(mode="json") for i in fields.line_items], indent=2
                    ),
                    computed_checks_json=audit_repo.dumps(checks.as_dict(), indent=2),
                    rules_json=audit_repo.dumps(
                        {
                            "required_header_fields": self.rt.rules.required_header_fields,
                            "required_line_fields": self.rt.rules.required_line_fields,
                            "accepted_currencies": sorted(self.rt.rules.accepted_currencies),
                            "arithmetic": self.rt.rules.section("arithmetic"),
                        },
                        indent=2,
                    ),
                ),
            )
            findings = _apply_llm_severities(
                findings, assessment.get("findings"), key="field_name"
            )
        except LLMUnavailable as exc:
            logger.warning("data validator LLM unavailable", extra={"error": str(exc)[:160]})
            assessment = {"assessment": f"LLM grading unavailable ({type(exc).__name__}); "
                                        "rule-based severities retained."}

        audit_repo.replace_line_items(invoice_id, fields.line_items)
        audit_repo.update_invoice(
            invoice_id,
            invoice_number=fields.invoice_number,
            vendor_name=fields.vendor_name,
            vendor_id=fields.vendor_id,
            po_number=fields.po_number,
            invoice_date=fields.invoice_date,
            currency=(fields.currency or None),
            subtotal_amount=fields.subtotal_amount,
            tax_amount=fields.tax_amount,
            total_amount=fields.total_amount,
            extracted_raw={
                **(fields.extras or {}),
                "page_count": state.get("page_count"),
                "ocr_used": state.get("ocr_used"),
                "data_assessment": assessment,
            },
        )
        audit_repo.record_event(
            stage="validate_data", status="ok", invoice_id=invoice_id,
            detail=f"{len(findings)} data findings", duration_ms=_ms(started),
        )

        return {
            "data_checks": checks.as_dict(),
            "data_findings": [f.model_dump(mode="json") for f in findings],
            "data_assessment": assessment,
        }

    def business_validator(self, state: InvoiceState) -> dict[str, Any]:
        from ..validation import compare_with_erp

        started = time.perf_counter()
        invoice_id = UUID(state["invoice_id"])
        fields = InvoiceFields.model_validate(state["fields"])

        try:
            vendor = self.rt.erp.resolve_vendor(fields.vendor_name, fields.vendor_id)
            purchase_order = self.rt.erp.get_purchase_order(fields.po_number or "")
            sku_lookup = {
                item.item_code.upper(): self.rt.erp.get_sku(item.item_code)
                for item in fields.line_items
                if item.item_code
            }
        except ERPUnavailable as exc:
            message = str(exc)
            audit_repo.mark_failed(invoice_id, message)
            audit_repo.record_event(
                stage="validate_business", status="error", invoice_id=invoice_id, detail=message
            )
            logger.error("ERP lookup failed", extra={"error": message})
            return {"status": "failed", "error": message}

        comparison, findings = compare_with_erp(
            fields,
            purchase_order=purchase_order,
            vendor=vendor,
            sku_lookup=sku_lookup,
            rules=self.rt.rules,
        )

        assessment: dict[str, Any] = {}
        persona = self.rt.prompts.persona("business_validator")
        try:
            assessment = self.rt.llm.complete_json(
                persona,
                self.rt.prompts.render(
                    "validate_business",
                    invoice_number=fields.invoice_number or "unknown",
                    vendor_name=fields.vendor_name or "unknown",
                    po_number=fields.po_number or "none",
                    invoice_header_json=audit_repo.dumps(
                        fields.model_dump(mode="json", exclude={"line_items"}), indent=2
                    ),
                    invoice_lines_json=audit_repo.dumps(
                        [i.model_dump(mode="json") for i in fields.line_items], indent=2
                    ),
                    po_json=audit_repo.dumps(purchase_order, indent=2),
                    vendor_json=audit_repo.dumps(vendor, indent=2),
                    discrepancies_json=audit_repo.dumps(
                        [f.model_dump(mode="json") for f in findings], indent=2
                    ),
                    tolerances_json=audit_repo.dumps(
                        self.rt.rules.section("tolerances"), indent=2
                    ),
                ),
            )
            findings = _apply_llm_severities(
                findings, assessment.get("severities"), key="field_name"
            )
        except LLMUnavailable as exc:
            logger.warning("business validator LLM unavailable", extra={"error": str(exc)[:160]})
            assessment = {"assessment": f"LLM grading unavailable ({type(exc).__name__}); "
                                        "rule-based severities retained."}

        data_findings = [
            Discrepancy.model_validate(item) for item in state.get("data_findings") or []
        ]
        all_findings = data_findings + findings
        severities = [f.severity for f in all_findings]

        translation_confidence = _to_decimal(state.get("translation_confidence"))
        recommendation, reason = self.rt.rules.recommend(
            severities=severities, translation_confidence=translation_confidence
        )

        if not all_findings:
            validation_status = ValidationStatus.PASSED
        elif recommendation == Recommendation.REJECT:
            validation_status = ValidationStatus.FAILED
        else:
            validation_status = ValidationStatus.PARTIAL

        confidence = _confidence_score(all_findings, translation_confidence)

        run_id = audit_repo.replace_discrepancies(invoice_id, all_findings)

        outcome: dict[str, Any] = {
            "validation_status": validation_status.value,
            "recommendation": recommendation.value,
            "confidence_score": confidence,
        }
        resolved_vendor_id = (vendor or {}).get("vendor_id") or fields.vendor_id
        if resolved_vendor_id:
            outcome["vendor_id"] = resolved_vendor_id

        audit_repo.update_invoice(invoice_id, **outcome)
        audit_repo.record_event(
            stage="validate_business", status="ok", invoice_id=invoice_id,
            detail=f"{len(findings)} business findings; recommendation {recommendation.value}",
            duration_ms=_ms(started),
        )
        logger.info(
            "invoice validated",
            extra={
                "invoice_id": state["invoice_id"],
                "recommendation": recommendation.value,
                "discrepancies": len(all_findings),
                "run_id": str(run_id),
            },
        )

        return {
            "purchase_order": purchase_order,
            "vendor": vendor,
            "business_comparison": comparison.as_dict(),
            "business_findings": [f.model_dump(mode="json") for f in findings],
            "business_assessment": assessment,
            "discrepancies": [f.model_dump(mode="json") for f in all_findings],
            "validation_status": validation_status.value,
            "recommendation": recommendation.value,
            "recommendation_reason": reason,
            "confidence_score": float(confidence),
        }

    def reporter(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        invoice_id = UUID(state["invoice_id"])

        invoice_row = audit_repo.json_safe(audit_repo.get_invoice(invoice_id)) or {}
        line_items = audit_repo.json_safe(audit_repo.get_line_items(invoice_id))
        discrepancies = audit_repo.json_safe(audit_repo.get_discrepancies(invoice_id))

        narrative: dict[str, Any] = {}
        persona = self.rt.prompts.persona("reporter")
        try:
            narrative = self.rt.llm.complete_json(
                persona,
                self.rt.prompts.render(
                    "write_report",
                    invoice_header_json=audit_repo.dumps(
                        {k: v for k, v in invoice_row.items()
                         if k not in {"raw_extracted_text", "translated_text", "extracted_raw"}},
                        indent=2,
                    ),
                    validation_status=state.get("validation_status", "unknown"),
                    recommendation=state.get("recommendation", "review"),
                    recommendation_reason=state.get("recommendation_reason", ""),
                    source_language=state.get("source_language", "unknown"),
                    translation_confidence=state.get("translation_confidence", "unknown"),
                    discrepancy_count=len(discrepancies),
                    discrepancies_json=audit_repo.dumps(discrepancies, indent=2),
                    po_json=audit_repo.dumps(state.get("purchase_order"), indent=2),
                ),
            )
        except LLMUnavailable as exc:
            logger.warning("reporter LLM unavailable", extra={"error": str(exc)[:160]})
            narrative = {
                "summary": _fallback_summary(invoice_row, discrepancies, state),
                "key_findings": [d.get("message") for d in discrepancies[:5] if d.get("message")],
                "suggested_action": "Review the discrepancy table below.",
            }

        invoice_row["processing_status"] = "completed"

        payload = self.rt.reports.build_payload(
            invoice=invoice_row,
            line_items=line_items,
            discrepancies=discrepancies,
            purchase_order=state.get("purchase_order"),
            data_checks=state.get("data_checks") or {},
            business_comparison=state.get("business_comparison") or {},
            narrative=narrative,
            raw_text=invoice_row.get("raw_extracted_text") or "",
        )

        stem = f"{invoice_row.get('invoice_number') or Path(state['file_name']).stem}_{str(invoice_id)[:8]}"
        json_path, html_path = self.rt.reports.write(
            payload, raw_text=invoice_row.get("raw_extracted_text") or "", stem=stem
        )
        audit_repo.update_invoice(
            invoice_id,
            report_summary=str(narrative.get("summary") or "")[:8000] or None,
            report_json_path=json_path,
            report_html_path=html_path,
        )
        audit_repo.record_event(
            stage="report", status="ok", invoice_id=invoice_id,
            detail=f"wrote {Path(json_path).name} and {Path(html_path).name}",
            duration_ms=_ms(started),
        )

        return {
            "narrative": narrative,
            "report_json_path": json_path,
            "report_html_path": html_path,
            "report_payload": payload,
        }

    def indexer(self, state: InvoiceState) -> dict[str, Any]:
        started = time.perf_counter()
        invoice_id = UUID(state["invoice_id"])

        audit_repo.update_invoice(invoice_id, processing_status="completed")

        try:
            chunks = index_invoice(self.rt, invoice_id)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.error("indexing failed", extra={"invoice_id": str(invoice_id), "error": message})
            audit_repo.record_event(
                stage="index", status="error", invoice_id=invoice_id, detail=message
            )
            return {
                "indexed_chunks": 0,
                "status": "completed",
                "warnings": [f"indexing failed: {message}"],
            }

        audit_repo.record_event(
            stage="index", status="ok", invoice_id=invoice_id,
            detail=f"{chunks} chunks written to Qdrant", duration_ms=_ms(started),
        )
        return {"indexed_chunks": chunks, "status": "completed"}

    def _persist_translation(self, invoice_id: UUID, update: dict[str, Any]) -> None:
        audit_repo.update_invoice(
            invoice_id,
            source_language=update["source_language"],
            translated_text=update["translated_text"],
            translation_confidence=_to_decimal(update["translation_confidence"]),
            translation_engine=update["translation_engine"][:64],
        )

    def _apply_corrections(self, fields: InvoiceFields, state: InvoiceState) -> InvoiceFields:
        """Overlay the auditor's corrections before re-validating.

        The corrections themselves live in audit.human_feedback; the original
        extraction on audit.invoice_audit is what they are measured against and
        is never destroyed.
        """
        header = state.get("corrections") or {}
        lines = state.get("line_corrections") or {}
        if not header and not lines:
            return fields

        data = fields.model_dump()

        for name, value in header.items():
            if name not in HEADER_CORRECTABLE:
                logger.warning("ignoring correction for unknown field", extra={"field": name})
                continue
            data[name] = _coerce_field(name, value)

        if lines:
            by_number = {item["line_number"]: item for item in data.get("line_items", [])}
            for key, value in lines.items():
                line_text, _, field_name = key.partition("|")
                try:
                    item = by_number[int(line_text)]
                except (KeyError, ValueError):
                    continue
                if field_name in {"item_code", "description"}:
                    item[field_name] = value
                elif field_name in {"quantity", "unit_price", "line_total"}:
                    item[field_name] = parse_amount(value)

        corrected = InvoiceFields.model_validate(data)
        corrected.extras = {**(corrected.extras or {}), "human_corrections_applied": True}
        return corrected




def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _strip_prompt_delimiters(text: str) -> str:
    """Drop the prompt's BEGIN/END fences if the model echoed them back.

    They are scaffolding, not invoice content, and left in place they are
    persisted, rendered into reports and embedded as chunk text in Qdrant.
    """
    cleaned = _PROMPT_FENCE_RE.sub("", text or "")
    return cleaned.strip() or text


def _detect_language(text: str) -> str:
    sample = (text or "").strip()
    if len(sample) < 20:
        return ENGLISH
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(sample[:4000])[:8]
    except Exception:
        logger.warning("language detection failed; assuming English")
        return ENGLISH


def _clamp01(value: Any, default: str = "0.5") -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal(default)
    return min(max(number, Decimal("0")), Decimal("1")).quantize(Decimal("0.001"))


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _coerce_field(name: str, value: str) -> Any:
    if name == "invoice_date":
        return parse_date(value)
    if name.endswith("_amount"):
        return parse_amount(value)
    if name == "currency":
        return (value or "").strip().upper()[:3] or None
    return (value or "").strip() or None


def _apply_llm_severities(
    findings: list[Discrepancy], graded: Any, key: str = "field_name"
) -> list[Discrepancy]:
    """Let the model raise a severity, never lower one.

    The rules engine floor is what the configuration promises; a model is
    allowed to say a finding is worse than the band suggests, but not to talk
    a confirmed discrepancy down.
    """
    if not isinstance(graded, list):
        return findings

    overrides: dict[tuple[str, int | None], Severity] = {}
    for entry in graded:
        if not isinstance(entry, dict):
            continue
        try:
            severity = Severity(str(entry.get("severity", "")).lower())
        except ValueError:
            continue
        line = entry.get("line_number")
        overrides[(str(entry.get(key)), int(line) if line is not None else None)] = severity

    for finding in findings:
        proposed = overrides.get((finding.field_name, finding.line_number)) or overrides.get(
            (finding.field_name, None)
        )
        if proposed and SEVERITY_ORDER[proposed] > SEVERITY_ORDER[finding.severity]:
            finding.severity = proposed
    return findings


def _confidence_score(
    findings: list[Discrepancy], translation_confidence: Decimal | None
) -> Decimal:
    """How much the system trusts its own conclusion, not whether it approved."""
    penalty = Decimal("0")
    weights = {
        Severity.LOW: Decimal("0.02"),
        Severity.MEDIUM: Decimal("0.06"),
        Severity.HIGH: Decimal("0.12"),
        Severity.CRITICAL: Decimal("0.20"),
    }
    for finding in findings:
        penalty += weights.get(finding.severity, Decimal("0.05"))

    base = translation_confidence if translation_confidence is not None else Decimal("0.9")
    score = max(Decimal("0"), min(Decimal("1"), base - penalty))
    return score.quantize(Decimal("0.001"))


def _fallback_summary(
    invoice: dict[str, Any], discrepancies: list[dict[str, Any]], state: InvoiceState
) -> str:
    return (
        f"Invoice {invoice.get('invoice_number') or invoice.get('file_name')} from "
        f"{invoice.get('vendor_name') or 'an unidentified vendor'} totalling "
        f"{invoice.get('total_amount')} {invoice.get('currency') or ''} was checked against "
        f"purchase order {invoice.get('po_number') or 'none'}. "
        f"{len(discrepancies)} discrepancies were recorded. "
        f"The recommendation is to {state.get('recommendation', 'review')} because "
        f"{state.get('recommendation_reason', 'the rules engine said so')}."
    ).strip()
