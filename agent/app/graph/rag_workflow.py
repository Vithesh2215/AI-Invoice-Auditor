
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Annotated, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from ..db import audit_repo
from ..models import RetrievedChunk, TriadScores
from ..services.cache import cache_key
from ..services.llm import LLMUnavailable

if TYPE_CHECKING:
    from ..runtime import Runtime

logger = logging.getLogger(__name__)

REFUSAL = "I cannot answer that from the indexed invoices."


def _keep_last(_: Any, right: Any) -> Any:
    return right


class RagState(TypedDict, total=False):
    question: str
    query: str
    asked_by: str | None

    invoice_id: str | None
    vendor: str | None
    source_type: str | None
    top_k: int

    route: str

    retrieved: list[dict[str, Any]]
    context: str
    citations: list[dict[str, Any]]

    answer: str
    scores: dict[str, Any]
    overall_score: float
    accepted: bool
    attempts: Annotated[int, _keep_last]
    note: str



class RagNodes:
    def __init__(self, runtime: "Runtime") -> None:
        self.rt = runtime

    def retrieval(self, state: RagState) -> dict[str, Any]:
        rules = self.rt.rules
        attempt = state.get("attempts", 1)

        base_k = state.get("top_k") or int(rules.rag_value("top_k", 6))
        widened = int(rules.rag_value("retrieval_expansion_k", 12))
        limit = base_k if attempt <= 1 else min(widened + (attempt - 2) * 4, 30)

        vector = self.rt.embedder.embed_query(state.get("query") or state["question"])
        hits = self.rt.vectors.search(
            vector,
            limit=limit,
            score_threshold=float(rules.rag_value("score_threshold", 0.15)),
            invoice_id=state.get("invoice_id"),
            vendor=state.get("vendor"),
            source_type=state.get("source_type"),
        )

        logger.info(
            "rag retrieval",
            extra={"attempt": attempt, "limit": limit, "hits": len(hits)},
        )
        return {"retrieved": hits}

    def augmentation(self, state: RagState) -> dict[str, Any]:
        """Rerank, de-duplicate and assemble the context window.

        Reranking is a deterministic fusion of the vector score, literal term
        overlap and a small bias towards report chunks. A cross-encoder would
        be better, but this account has no Bedrock rerank access and a second
        local model would not fit the image budget.
        """
        rules = self.rt.rules
        hits = state.get("retrieved") or []
        if not hits:
            return {"context": "", "citations": []}

        question_terms = _terms(state.get("query") or state["question"])
        boosts = rules.rag_value("source_type_boost", {}) or {}

        scored: list[tuple[float, dict[str, Any]]] = []
        for hit in hits:
            text = hit.get("chunk_text") or ""
            overlap = _overlap(question_terms, _terms(text))
            boost = float(boosts.get(hit.get("source_type"), 0.0))
            scored.append((hit.get("score", 0.0) + 0.25 * overlap + boost, hit))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        max_chars = int(rules.rag_value("max_context_chars", 9000))
        seen: set[str] = set()
        passages: list[str] = []
        citations: list[dict[str, Any]] = []
        used = 0

        for rank_score, hit in scored:
            text = (hit.get("chunk_text") or "").strip()
            fingerprint = text[:160]
            if not text or fingerprint in seen:
                continue
            if used + len(text) > max_chars and passages:
                break
            seen.add(fingerprint)

            label = (
                f"[{len(passages) + 1}] source={hit.get('source_type')} "
                f"invoice={hit.get('invoice_number') or hit.get('invoice_id')} "
                f"vendor={hit.get('vendor') or 'unknown'}"
            )
            passages.append(f"{label}\n{text}")
            used += len(text)

            citations.append(
                {
                    "chunk_id": hit.get("id"),
                    "invoice_id": hit.get("invoice_id"),
                    "invoice_number": hit.get("invoice_number"),
                    "source_type": hit.get("source_type"),
                    "vendor": hit.get("vendor"),
                    "language": hit.get("language"),
                    "score": round(float(rank_score), 4),
                    "chunk_text": text,
                }
            )

        return {"context": "\n\n".join(passages), "citations": citations}

    def generation(self, state: RagState) -> dict[str, Any]:
        context = state.get("context") or ""
        if not context.strip():
            return {"answer": REFUSAL, "note": "nothing relevant was retrieved"}

        persona = self.rt.prompts.persona("rag_generator")
        try:
            answer = self.rt.llm.complete(
                persona,
                self.rt.prompts.render(
                    "rag_generate", question=state["question"], context=context
                ),
            )
        except LLMUnavailable as exc:
            logger.error("rag generation failed", extra={"error": str(exc)[:160]})
            return {
                "answer": (
                    "The answering model is currently unavailable. The most relevant "
                    "passages retrieved for this question are listed as citations."
                ),
                "note": f"generation unavailable: {exc}",
            }
        return {"answer": answer.strip()}

    def reflection(self, state: RagState) -> dict[str, Any]:
        rules = self.rt.rules
        threshold = float(rules.rag_value("min_triad_score", 0.7))
        max_attempts = int(rules.rag_value("max_attempts", 3))
        attempt = state.get("attempts", 1)
        answer = state.get("answer") or ""

        if not (state.get("context") or "").strip():
            return {
                "scores": TriadScores(reasoning="no context was retrieved").model_dump(),
                "overall_score": 0.0,
                "accepted": attempt >= max_attempts,
                "note": "no indexed content matched this question",
            }

        persona = self.rt.prompts.persona("rag_reflector")
        try:
            payload = self.rt.llm.complete_json(
                persona,
                self.rt.prompts.render(
                    "rag_reflect",
                    question=state["question"],
                    context=state.get("context", ""),
                    answer=answer,
                ),
            )
            scores = TriadScores(
                context_relevance=_score(payload.get("context_relevance")),
                groundedness=_score(payload.get("groundedness")),
                answer_relevance=_score(payload.get("answer_relevance")),
                reasoning=str(payload.get("reasoning") or "")[:500] or None,
                missing_information=str(payload.get("missing_information") or "")[:500] or None,
            )
        except LLMUnavailable as exc:
            logger.warning("reflection unavailable", extra={"error": str(exc)[:160]})
            return {
                "scores": TriadScores(reasoning="evaluator unavailable").model_dump(),
                "overall_score": 0.0,
                "accepted": True,
                "note": f"answer returned without RAG Triad verification: {exc}",
            }

        overall = scores.overall
        accepted = overall >= threshold or attempt >= max_attempts

        logger.info(
            "rag reflection",
            extra={"attempt": attempt, "overall": overall, "accepted": accepted},
        )

        note = None
        if not accepted:
            note = f"triad score {overall} below {threshold}; retrieving again"
        elif overall < threshold:
            note = (
                f"returned after {attempt} attempts with a triad score of {overall}, "
                f"which is below the {threshold} bar; treat with caution"
            )

        return {
            "scores": scores.model_dump(),
            "overall_score": overall,
            "accepted": accepted,
            "note": note,
        }

    def rewrite(self, state: RagState) -> dict[str, Any]:
        attempt = state.get("attempts", 1) + 1
        scores = state.get("scores") or {}
        persona = self.rt.prompts.persona("query_rewriter")

        try:
            payload = self.rt.llm.complete_json(
                persona,
                self.rt.prompts.render(
                    "rag_rewrite",
                    question=state["question"],
                    previous_score=state.get("overall_score", 0.0),
                    missing_information=scores.get("missing_information") or "unclear",
                ),
            )
            query = str(payload.get("rewritten_query") or "").strip()
        except LLMUnavailable:
            query = ""

        if not query:
            query = state.get("query") or state["question"]

        logger.info("rag query rewritten", extra={"attempt": attempt, "query": query[:160]})
        return {"query": query, "attempts": attempt}




def _after_reflection(state: RagState) -> str:
    return "done" if state.get("accepted") else "retry"


def build_rag_graph(runtime: "Runtime"):
    nodes = RagNodes(runtime)

    graph = StateGraph(RagState)
    graph.add_node("retrieval", nodes.retrieval)
    graph.add_node("augmentation", nodes.augmentation)
    graph.add_node("generation", nodes.generation)
    graph.add_node("reflection", nodes.reflection)
    graph.add_node("rewrite", nodes.rewrite)

    graph.add_edge(START, "retrieval")
    graph.add_edge("retrieval", "augmentation")
    graph.add_edge("augmentation", "generation")
    graph.add_edge("generation", "reflection")
    graph.add_conditional_edges(
        "reflection", _after_reflection, {"done": END, "retry": "rewrite"}
    )
    graph.add_edge("rewrite", "retrieval")

    compiled = graph.compile()
    logger.info("rag workflow compiled")
    return compiled


def answer_question(
    runtime: "Runtime",
    *,
    question: str,
    invoice_id: UUID | None = None,
    vendor: str | None = None,
    source_type: str | None = None,
    top_k: int | None = None,
    asked_by: str | None = None,
    use_cache: bool = True,
):
    from ..models import RagAnswer

    started = time.perf_counter()
    key = cache_key(
        "rag",
        question.strip().lower(),
        str(invoice_id or ""),
        vendor or "",
        source_type or "",
        str(top_k or ""),
    )

    if use_cache:
        cached = runtime.cache.get_json(key)
        if cached:
            answer = RagAnswer.model_validate(cached)
            answer.cache_hit = True
            answer.latency_ms = int((time.perf_counter() - started) * 1000)
            audit_repo.log_rag_query(
                question=question, answer=answer.answer, route=answer.route,
                invoice_id=invoice_id, attempts=answer.attempts,
                scores=(answer.scores.model_dump() if answer.scores else None),
                overall_score=answer.overall_score, accepted=answer.accepted,
                chunk_ids=[c.chunk_id for c in answer.citations], cache_hit=True,
                latency_ms=answer.latency_ms, asked_by=asked_by,
            )
            return answer

    initial: RagState = {
        "question": question.strip(),
        "query": question.strip(),
        "route": "vector",
        "invoice_id": str(invoice_id) if invoice_id else None,
        "vendor": vendor,
        "source_type": source_type,
        "asked_by": asked_by,
        "attempts": 1,
    }
    if top_k:
        initial["top_k"] = top_k

    final = runtime.rag_graph.invoke(
        initial, config={"recursion_limit": 40}
    )

    scores = TriadScores.model_validate(final["scores"]) if final.get("scores") else None
    citations = [RetrievedChunk.model_validate(c) for c in (final.get("citations") or [])]

    answer = RagAnswer(
        question=question,
        answer=final.get("answer") or REFUSAL,
        route=final.get("route", "vector"),
        accepted=bool(final.get("accepted")),
        attempts=int(final.get("attempts", 1)),
        scores=scores,
        overall_score=final.get("overall_score"),
        citations=citations,
        cache_hit=False,
        latency_ms=int((time.perf_counter() - started) * 1000),
        note=final.get("note"),
    )

    if use_cache and answer.accepted and answer.answer != REFUSAL:
        runtime.cache.set_json(
            key, answer.model_dump(mode="json"), runtime.settings.llm_cache_ttl_seconds
        )

    audit_repo.log_rag_query(
        question=question, answer=answer.answer, route=answer.route, invoice_id=invoice_id,
        attempts=answer.attempts,
        scores=(scores.model_dump() if scores else None),
        overall_score=answer.overall_score, accepted=answer.accepted,
        chunk_ids=[c.chunk_id for c in citations], cache_hit=False,
        latency_ms=answer.latency_ms, asked_by=asked_by,
    )
    return answer



_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-/.]{2,}")
_STOPWORDS = {
    "the", "and", "for", "was", "were", "are", "with", "from", "this", "that",
    "what", "which", "how", "many", "much", "does", "did", "has", "have", "any",
    "all", "about", "invoice", "invoices",
}


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def _overlap(left: set[str], right: set[str]) -> float:
    if not left:
        return 0.0
    return len(left & right) / len(left)


def _score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
