from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .db import audit_repo
from .extraction import checksum_file
from .graph.workflow import run_invoice
from .models import ProcessingOutcome

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)


class InvoiceWatcher:
    def __init__(self, runtime: "Runtime") -> None:
        self.rt = runtime
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._semaphore = asyncio.Semaphore(runtime.settings.watch_max_concurrency)
        self._in_flight: set[str] = set()
        self.last_scan_at: float | None = None
        self.total_processed = 0

    def start(self) -> None:
        if not self.rt.settings.watch_enabled:
            logger.info("folder watcher disabled by configuration")
            return
        self._task = asyncio.create_task(self._loop(), name="invoice-watcher")
        logger.info(
            "folder watcher started",
            extra={
                "directory": str(self.rt.settings.incoming_dir),
                "interval_s": self.rt.settings.watch_interval_seconds,
            },
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(2)
        while not self._stopping.is_set():
            try:
                await self.scan()
            except Exception:
                logger.exception("watcher scan failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.rt.settings.watch_interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    def discover(self) -> list[Path]:
        directory = self.rt.settings.incoming_dir
        if not directory.is_dir():
            logger.warning("incoming directory is missing", extra={"path": str(directory)})
            return []

        extensions = set(self.rt.settings.supported_extensions)
        now = time.time()
        stability = self.rt.settings.watch_stability_seconds

        candidates: list[Path] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in extensions:
                continue
            try:
                if now - path.stat().st_mtime < stability:
                    continue
            except OSError:
                continue
            candidates.append(path)
        return candidates

    async def scan(self, *, force: bool = False, limit: int | None = None) -> dict[str, Any]:
        paths = self.discover()
        if limit:
            paths = paths[:limit]

        results: list[ProcessingOutcome] = []
        tasks = [self._process(path, force=force) for path in paths]
        for outcome in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(outcome, BaseException):
                logger.error("invoice task raised", exc_info=outcome)
                continue
            if outcome is not None:
                results.append(outcome)

        self.last_scan_at = time.time()

        summary = {
            "scanned": len(paths),
            "processed": sum(1 for r in results if r.status == "completed"),
            "skipped": sum(1 for r in results if r.status == "skipped"),
            "failed": sum(1 for r in results if r.status == "failed"),
            "results": results,
        }
        if summary["processed"] or summary["failed"]:
            logger.info("scan complete", extra={k: v for k, v in summary.items() if k != "results"})
        return summary

    async def _process(self, path: Path, *, force: bool) -> ProcessingOutcome | None:
        key = str(path)
        if key in self._in_flight:
            return None
        self._in_flight.add(key)

        try:
            async with self._semaphore:
                if not force:
                    skip = await asyncio.to_thread(self._already_done, path)
                    if skip is not None:
                        return skip
                return await asyncio.to_thread(self.process_now, path, force)
        finally:
            self._in_flight.discard(key)

    def _already_done(self, path: Path) -> ProcessingOutcome | None:
        """Return a skip outcome (and log it) if this checksum is already completed.

        Every detection cycle, including ones that short-circuit here, must
        leave a `processing_events` row so "record every detected file" holds
        at the watcher level, not just inside the graph.
        """
        try:
            checksum = checksum_file(path)
            existing = audit_repo.find_by_checksum(checksum)
        except Exception:
            logger.exception("checksum/lookup failed before dedupe check", extra={"file": path.name})
            return None
        if not (existing and existing["processing_status"] == "completed"):
            return None
        detail = "checksum already present in the audit trail"
        audit_repo.record_event(
            stage="monitor",
            status="skipped",
            invoice_id=existing.get("invoice_id"),
            file_name=path.name,
            checksum=checksum,
            detail=detail,
        )
        return ProcessingOutcome(
            invoice_id=existing.get("invoice_id"),
            file_name=path.name,
            status="skipped",
            detail=detail,
        )

    def process_now(self, path: Path, force: bool = False) -> ProcessingOutcome:
        """Run one invoice through the graph. Blocking; call in a worker thread."""
        started = time.perf_counter()
        try:
            final = run_invoice(self.rt, str(path), force_reprocess=force)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("workflow crashed", extra={"file": path.name})
            audit_repo.record_event(
                stage="workflow", status="error", file_name=path.name, detail=message
            )
            return ProcessingOutcome(file_name=path.name, status="failed", detail=message)

        status = final.get("status", "completed")
        if status == "completed":
            self.total_processed += 1

        logger.info(
            "invoice finished",
            extra={
                "file": path.name,
                "status": status,
                "recommendation": final.get("recommendation"),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

        return ProcessingOutcome(
            invoice_id=final.get("invoice_id"),
            file_name=path.name,
            status=status,
            recommendation=final.get("recommendation"),
            validation_status=final.get("validation_status"),
            discrepancy_count=len(final.get("discrepancies") or []),
            detail=final.get("error") or final.get("skipped_reason"),
        )
