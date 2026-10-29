
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .db import close_pool, open_pool
from .models import DependencyHealth
from .services.cache import Cache
from .services.embeddings import Embedder, build_embedder
from .services.erp_client import ERPClient
from .services.llm import LLMClient
from .services.prompts import PromptRegistry, load_prompts
from .services.reports import ReportWriter
from .services.rules import Rules, load_rules
from .services.vectorstore import VectorStore
from .settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    rules: Rules
    prompts: PromptRegistry
    cache: Cache
    llm: LLMClient
    embedder: Embedder
    vectors: VectorStore
    erp: ERPClient
    reports: ReportWriter

    invoice_graph: Any = None
    rag_graph: Any = None

    _llm_probe: tuple[float, str, str | None] | None = None

    def close(self) -> None:
        for closer in (self.erp.close, self.vectors.close, self.cache.close, close_pool):
            try:
                closer()
            except Exception:
                logger.warning("error during shutdown", exc_info=True)

    def health(self) -> list[DependencyHealth]:
        return [
            self._probe("postgres", self._check_postgres),
            self._probe("mock-erp", self._check_erp),
            self._probe("qdrant", self._check_qdrant),
            self._probe("redis", self._check_redis),
            self._probe("bedrock", self._check_llm, required=False),
        ]

    def _probe(self, name: str, check, required: bool = True) -> DependencyHealth:
        started = time.perf_counter()
        try:
            detail: Any = check()
            healthy = True
        except Exception as exc:
            detail = f"{type(exc).__name__}: {str(exc)[:160]}"
            healthy = False
        return DependencyHealth(
            name=name,
            healthy=healthy,
            required=required,
            detail=str(detail)[:240],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _check_postgres(self) -> str:
        from .db.pool import get_pool

        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS invoices FROM audit.invoice_audit"
            ).fetchone()
        return f"{row['invoices']} invoices in the audit trail"

    def _check_erp(self) -> str:
        self.erp.health()
        return f"reachable at {self.erp.base_url}"

    def _check_qdrant(self) -> str:
        info = self.vectors.verify()
        return f"collection '{info['collection']}' holds {info['points']} points"

    def _check_redis(self) -> str:
        if not self.cache.ping():
            raise RuntimeError("PING failed")
        stats = self.cache.stats()
        return f"{stats.get('keys', 0)} cached keys"

    def _check_llm(self) -> str:
        if not self.settings.llm_enabled:
            return "disabled by configuration"

        now = time.monotonic()
        if self._llm_probe is not None and now < self._llm_probe[0]:
            _, detail, error = self._llm_probe
            if error:
                raise RuntimeError(error)
            return detail

        expires = now + self.settings.llm_health_probe_interval_seconds
        try:
            result = self.llm.verify()
        except Exception as exc:
            error = f"{self.llm.model_for('reasoning')} unusable: {type(exc).__name__}"
            self._llm_probe = (expires, "", error)
            raise RuntimeError(error) from None

        detail = (
            f"{result['model']} answered in {result['latency_ms']}ms"
            if result.get("enabled")
            else str(result.get("detail", "disabled"))
        )
        self._llm_probe = (expires, detail, None)
        return detail


def build_runtime(settings: Settings) -> Runtime:
    """Open every connection, verifying as we go.

    Ordering matters: the cheap local checks run first so that a configuration
    mistake surfaces before we spend time loading an embedding model.
    """
    rules = load_rules(settings.rules_path)
    prompts = load_prompts(settings.personas_path, settings.prompts_path)

    open_pool(settings)

    cache = Cache(settings)
    try:
        cache.verify()
    except Exception as exc:
        raise RuntimeError(
            f"cannot reach Redis at {settings.redis_url} ({type(exc).__name__}). "
            "Check the REDIS_URL service name and that the redis service is healthy."
        ) from None

    llm = LLMClient(settings, cache)

    try:
        embedder = build_embedder(settings)
    except Exception as exc:
        raise RuntimeError(
            f"cannot initialize the '{settings.embedding_provider}' embedder "
            f"({type(exc).__name__}: {exc}). With the default provider "
            "('bedrock'), this usually means AWS credentials are missing or "
            "the account lacks InvokeModel on "
            f"'{settings.bedrock_embedding_model}'; set "
            "EMBEDDING_PROVIDER=fastembed (and add fastembed back to "
            "agent/requirements.txt) to run without Bedrock access."
        ) from None

    vectors = VectorStore(settings, embedder.dimension)
    try:
        vectors.ensure_collection()
    except Exception as exc:
        raise RuntimeError(
            f"cannot prepare the Qdrant collection at {settings.qdrant_url} "
            f"({type(exc).__name__}: {exc})"
        ) from None

    erp = ERPClient(settings)
    try:
        erp.health()
    except Exception as exc:
        raise RuntimeError(
            f"cannot reach the ERP service at {settings.erp_base_url} ({exc}). "
            "Remember that containers address it as mock-erp:8000, not localhost:8001."
        ) from None

    reports = ReportWriter(settings.reports_dir)
    reports.ensure_dir()

    runtime = Runtime(
        settings=settings,
        rules=rules,
        prompts=prompts,
        cache=cache,
        llm=llm,
        embedder=embedder,
        vectors=vectors,
        erp=erp,
        reports=reports,
    )

    from .graph.rag_workflow import build_rag_graph
    from .graph.workflow import build_invoice_graph

    runtime.invoice_graph = build_invoice_graph(runtime)
    runtime.rag_graph = build_rag_graph(runtime)

    logger.info(
        "runtime ready",
        extra={
            "embedding_provider": embedder.name,
            "embedding_dim": embedder.dimension,
            "llm_model": settings.llm_reasoning_model,
            "erp": settings.erp_base_url,
        },
    )
    return runtime
