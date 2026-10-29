from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .db import audit_repo
from .logging_config import configure_logging
from .models import (
    FeedbackIn,
    HealthResponse,
    ProcessingOutcome,
    RagAnswer,
    RagQueryIn,
    ScanRequest,
    ScanResponse,
)
from .runtime import Runtime, build_runtime
from .settings import Settings, get_settings
from .watcher import InvoiceWatcher

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_scan_lock = asyncio.Lock()



@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info("starting %s v%s", settings.app_name, __version__)
    runtime = build_runtime(settings)
    watcher = InvoiceWatcher(runtime)

    app.state.settings = settings
    app.state.runtime = runtime
    app.state.watcher = watcher

    watcher.start()
    try:
        yield
    finally:
        await watcher.stop()
        from .graph.workflow import close_checkpointer

        close_checkpointer()
        runtime.close()
        logger.info("shutdown complete")


app = FastAPI(
    title="AI Invoice Auditor - Agent",
    version=__version__,
    description=(
        "Agentic invoice audit pipeline: monitor, extract, translate, validate "
        "against the ERP, report and index for retrieval-augmented question "
        "answering."
    ),
    lifespan=lifespan,
)


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(503, "the agent is still starting up")
    return runtime


def get_watcher(request: Request) -> InvoiceWatcher:
    watcher = getattr(request.app.state, "watcher", None)
    if watcher is None:
        raise HTTPException(503, "the agent is still starting up")
    return watcher


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


RT = Depends(get_runtime)
WATCHER = Depends(get_watcher)
CONFIG = Depends(get_app_settings)


def _require_invoice(invoice_id: UUID) -> dict[str, Any]:
    invoice = audit_repo.get_invoice(invoice_id)
    if invoice is None:
        raise HTTPException(404, "no invoice with that id has been audited")
    return invoice




@app.get("/", tags=["meta"])
def root(settings: Settings = CONFIG) -> dict[str, Any]:
    return {
        "service": settings.app_name,
        "version": __version__,
        "environment": settings.app_env,
        "docs": "/docs",
    }


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health(runtime: Runtime = RT):
    """Probe every dependency.

    A failing optional dependency (Bedrock) degrades the pipeline but does not
    make the service unhealthy, because the workflow is written to carry on
    without a model and push the invoice towards manual review instead.
    """
    dependencies = runtime.health()
    degraded = [d for d in dependencies if not d.healthy and d.required]

    payload = HealthResponse(
        status="unhealthy" if degraded else "healthy",
        version=__version__,
        dependencies=dependencies,
    )
    if degraded:
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    return payload


@app.get("/stats", tags=["meta"])
def stats(runtime: Runtime = RT, watcher: InvoiceWatcher = WATCHER) -> dict[str, Any]:
    return {
        "portfolio": audit_repo.json_safe(audit_repo.portfolio_totals()),
        "by_recommendation": audit_repo.json_safe(audit_repo.counts_by_recommendation()),
        "top_discrepancy_fields": audit_repo.json_safe(audit_repo.top_discrepancy_fields()),
        "vectors": {
            "collection": runtime.vectors.collection,
            "points": runtime.vectors.count(),
            "embedder": runtime.embedder.name,
            "dimension": runtime.embedder.dimension,
        },
        "cache": runtime.cache.stats(),
        "llm": {
            "enabled": runtime.settings.llm_enabled,
            "calls": runtime.llm.calls,
            "cache_hits": runtime.llm.cache_hits,
            "failures": runtime.llm.failures,
        },
        "watcher": {
            "enabled": runtime.settings.watch_enabled,
            "interval_seconds": runtime.settings.watch_interval_seconds,
            "incoming_dir": str(runtime.settings.incoming_dir),
            "last_scan_at": watcher.last_scan_at,
            "total_processed": watcher.total_processed,
            "files_in_directory": len(watcher.discover()),
        },
    }


@app.get("/config/rules", tags=["meta"])
def effective_rules(runtime: Runtime = RT) -> dict[str, Any]:
    """The rules actually in force, after defaults have been merged in."""
    return runtime.rules.raw




@app.post("/scan", response_model=ScanResponse, tags=["ingestion"])
async def scan(request: ScanRequest, watcher: InvoiceWatcher = WATCHER) -> ScanResponse:
    async with _scan_lock:
        summary = await watcher.scan(force=request.force, limit=request.limit)
    return ScanResponse(**summary)



@app.post("/invoices/upload", response_model=ProcessingOutcome, tags=["ingestion"])
async def upload_invoice(
    file: UploadFile = File(...),
    process_now: bool = Query(True, description="Audit immediately instead of waiting for the next scan."),
    settings: Settings = CONFIG,
    watcher: InvoiceWatcher = WATCHER,
) -> ProcessingOutcome:
    """Accept an invoice over HTTP and drop it into the watched directory."""
    import asyncio

    original = Path(file.filename or "upload").name
    suffix = Path(original).suffix.lower()
    if suffix not in set(settings.supported_extensions):
        raise HTTPException(
            415,
            f"'{suffix or 'no extension'}' is not a supported invoice format; "
            f"expected one of {', '.join(settings.supported_extensions)}",
        )

    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"invoice exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
    if not payload:
        raise HTTPException(400, "the uploaded file is empty")

    settings.incoming_dir.mkdir(parents=True, exist_ok=True)
    destination = _unique_path(settings.incoming_dir, original)
    destination.write_bytes(payload)

    logger.info(
        "invoice uploaded",
        extra={"file": destination.name, "bytes": len(payload)},
    )

    if not process_now:
        return ProcessingOutcome(
            file_name=destination.name,
            status="pending",
            detail="saved to the incoming directory; the watcher will pick it up",
        )

    return await asyncio.to_thread(watcher.process_now, destination, False)


@app.post("/invoices/{invoice_id}/reprocess", response_model=ProcessingOutcome, tags=["ingestion"])
async def reprocess_invoice(invoice_id: UUID, runtime: Runtime = RT) -> ProcessingOutcome:
    """Run the workflow again, overlaying any corrections recorded since."""
    return await _revalidate(runtime, invoice_id)




@app.get("/invoices", tags=["invoices"])
def list_invoices(
    recommendation: str | None = Query(None, pattern="^(approve|review|reject)$"),
    processing_status: str | None = Query(
        None, pattern="^(pending|processing|completed|failed|skipped)$"
    ),
    vendor_id: str | None = Query(None, max_length=64),
    search: str | None = Query(None, max_length=255),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    rows = audit_repo.list_invoices(
        recommendation=recommendation,
        processing_status=processing_status,
        vendor_id=vendor_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return audit_repo.json_safe(rows)


@app.get("/invoices/{invoice_id}", tags=["invoices"])
def get_invoice(invoice_id: UUID) -> dict[str, Any]:
    invoice = _require_invoice(invoice_id)
    return audit_repo.json_safe(
        {
            "invoice": {
                k: v for k, v in invoice.items() if k != "raw_extracted_text"
            },
            "raw_extracted_text": invoice.get("raw_extracted_text"),
            "line_items": audit_repo.get_line_items(invoice_id),
            "discrepancies": audit_repo.get_discrepancies(invoice_id),
            "feedback": audit_repo.get_feedback(invoice_id),
            "events": audit_repo.get_events(invoice_id, limit=50),
        }
    )


@app.get("/invoices/{invoice_id}/report", tags=["invoices"])
def get_report_json(invoice_id: UUID) -> dict[str, Any]:
    import json

    invoice = _require_invoice(invoice_id)
    path = invoice.get("report_json_path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no report has been generated for this invoice yet")
    return json.loads(Path(path).read_text(encoding="utf-8"))


@app.get("/invoices/{invoice_id}/report.html", response_class=HTMLResponse, tags=["invoices"])
def get_report_html(invoice_id: UUID):
    invoice = _require_invoice(invoice_id)
    path = invoice.get("report_html_path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "no report has been generated for this invoice yet")
    return FileResponse(path, media_type="text/html")




@app.post("/invoices/{invoice_id}/feedback", tags=["feedback"])
async def submit_feedback(
    invoice_id: UUID, feedback: FeedbackIn, runtime: Runtime = RT
) -> dict[str, Any]:
    """Record an auditor's correction, then optionally re-validate.

    The correction is an INSERT into audit.human_feedback. The original
    extraction on audit.invoice_audit is what it is measured against and is
    never overwritten; the trigger on that table enforces it.
    """
    invoice = _require_invoice(invoice_id)

    original = _original_value(invoice, invoice_id, feedback)
    feedback_id = audit_repo.insert_feedback(
        invoice_id=invoice_id,
        field_name=feedback.field_name,
        original_value=original,
        corrected_value=feedback.corrected_value,
        corrected_by=feedback.corrected_by,
        line_number=feedback.line_number,
        note=feedback.note,
    )
    audit_repo.record_event(
        stage="feedback",
        status="ok",
        invoice_id=invoice_id,
        detail=f"{feedback.corrected_by} corrected '{feedback.field_name}'",
    )

    result: dict[str, Any] = {
        "feedback_id": feedback_id,
        "field_name": feedback.field_name,
        "original_value": original,
        "corrected_value": feedback.corrected_value,
        "revalidated": False,
    }

    if feedback.revalidate:
        outcome = await _revalidate(runtime, invoice_id)
        audit_repo.mark_feedback_revalidated(invoice_id)
        result["revalidated"] = True
        result["outcome"] = outcome.model_dump(mode="json")

    return result


@app.get("/invoices/{invoice_id}/feedback", tags=["feedback"])
def list_feedback(invoice_id: UUID) -> list[dict[str, Any]]:
    _require_invoice(invoice_id)
    return audit_repo.json_safe(audit_repo.get_feedback(invoice_id))




@app.post("/rag/query", response_model=RagAnswer, tags=["rag"])
async def rag_query(query: RagQueryIn, runtime: Runtime = RT) -> RagAnswer:
    import asyncio

    from .graph.rag_workflow import answer_question

    return await asyncio.to_thread(
        answer_question,
        runtime,
        question=query.question,
        invoice_id=query.invoice_id,
        vendor=query.vendor,
        source_type=query.source_type,
        top_k=query.top_k,
        asked_by=query.asked_by,
    )


@app.get("/rag/history", tags=["rag"])
def rag_history(limit: int = Query(25, ge=1, le=200)) -> list[dict[str, Any]]:
    return audit_repo.json_safe(audit_repo.recent_rag_queries(limit))




@app.post("/admin/reindex", tags=["admin"])
async def reindex(
    drop_collection: bool = Query(
        True, description="Recreate the collection before repopulating it."
    ),
    runtime: Runtime = RT,
) -> dict[str, Any]:
    """Rebuild Qdrant from PostgreSQL.

    This is the recovery path after the Qdrant volume is lost. No document is
    reopened and no OCR is repeated: every chunk is built from text already
    persisted in audit.invoice_audit.
    """
    import asyncio

    from .services.indexing import rebuild_index

    return await asyncio.to_thread(rebuild_index, runtime, drop_collection=drop_collection)




async def _revalidate(runtime: Runtime, invoice_id: UUID) -> ProcessingOutcome:
    """Re-run the graph for an existing invoice with its corrections applied."""
    import asyncio

    from .graph.workflow import run_invoice

    invoice = _require_invoice(invoice_id)
    path = Path(invoice["file_path"])
    if not path.is_file():
        raise HTTPException(
            410,
            f"the original document is no longer at {path}; "
            "re-upload it before re-validating",
        )

    corrections = audit_repo.latest_corrections(invoice_id)
    line_corrections = {
        f"{line_number}|{field}": value
        for (line_number, field), value in audit_repo.latest_line_corrections(
            invoice_id
        ).items()
    }

    final = await asyncio.to_thread(
        run_invoice,
        runtime,
        str(path),
        force_reprocess=True,
        corrections=corrections,
        line_corrections=line_corrections,
    )

    return ProcessingOutcome(
        invoice_id=final.get("invoice_id") or str(invoice_id),
        file_name=path.name,
        status=final.get("status", "completed"),
        recommendation=final.get("recommendation"),
        validation_status=final.get("validation_status"),
        discrepancy_count=len(final.get("discrepancies") or []),
        detail=final.get("error") or final.get("skipped_reason"),
    )


def _original_value(
    invoice: dict[str, Any], invoice_id: UUID, feedback: FeedbackIn
) -> str | None:
    """What the system extracted, so the correction has something to measure."""
    if feedback.line_number is None:
        value = invoice.get(feedback.field_name)
        return None if value is None else str(value)

    for item in audit_repo.get_line_items(invoice_id):
        if item["line_number"] == feedback.line_number:
            value = item.get(feedback.field_name)
            return None if value is None else str(value)
    return None


def _unique_path(directory: Path, file_name: str) -> Path:
    """Never silently overwrite a file already waiting in the drop folder."""
    candidate = directory / file_name
    if not candidate.exists():
        return candidate

    stem, suffix = Path(file_name).stem, Path(file_name).suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(409, "too many files with that name are already queued")
