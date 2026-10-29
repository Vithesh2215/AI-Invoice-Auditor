
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from qdrant_client import QdrantClient, models

from ..settings import Settings

logger = logging.getLogger(__name__)

_NAMESPACE = uuid.UUID("6f1d2e7a-6b3b-4a4e-9f4a-2c5f3f9d1a77")

INDEXED_PAYLOAD_FIELDS = {
    "invoice_id": models.PayloadSchemaType.KEYWORD,
    "source_type": models.PayloadSchemaType.KEYWORD,
    "vendor": models.PayloadSchemaType.KEYWORD,
    "language": models.PayloadSchemaType.KEYWORD,
    "invoice_number": models.PayloadSchemaType.KEYWORD,
    "po_number": models.PayloadSchemaType.KEYWORD,
}


@dataclass
class Chunk:
    invoice_id: str
    chunk_index: int
    chunk_text: str
    source_type: str
    vendor: str | None = None
    language: str | None = None
    invoice_number: str | None = None
    po_number: str | None = None
    extra_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def point_id(self) -> str:
        return str(
            uuid.uuid5(_NAMESPACE, f"{self.invoice_id}|{self.source_type}|{self.chunk_index}")
        )

    def payload(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "chunk_index": self.chunk_index,
            "chunk_text": self.chunk_text,
            "source_type": self.source_type,
            "vendor": self.vendor,
            "language": self.language,
            "invoice_number": self.invoice_number,
            "po_number": self.po_number,
            **self.extra_payload,
        }


class VectorStore:
    def __init__(self, settings: Settings, dimension: int) -> None:
        self._settings = settings
        self._collection = settings.qdrant_collection
        self._dimension = dimension
        self._client = QdrantClient(
            url=settings.qdrant_url,
            timeout=int(settings.qdrant_timeout_seconds),
            prefer_grpc=False,
        )

    @property
    def collection(self) -> str:
        return self._collection

    def ensure_collection(self) -> None:
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._dimension, distance=models.Distance.COSINE
                ),
            )
            logger.info(
                "qdrant collection created",
                extra={"collection": self._collection, "dimension": self._dimension},
            )
        else:
            info = self._client.get_collection(self._collection)
            existing = info.config.params.vectors.size
            if existing != self._dimension:
                raise RuntimeError(
                    f"Qdrant collection '{self._collection}' has dimension {existing} "
                    f"but the configured embedding model produces {self._dimension}. "
                    "Delete the collection or set EMBEDDING_MODEL back."
                )

        for name, schema in INDEXED_PAYLOAD_FIELDS.items():
            try:
                self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=name,
                    field_schema=schema,
                    wait=True,
                )
            except Exception:
                pass

    def verify(self) -> dict[str, Any]:
        info = self._client.get_collection(self._collection)
        return {
            "collection": self._collection,
            "points": info.points_count or 0,
            "status": str(info.status),
        }

    def count(self) -> int:
        try:
            return self._client.count(self._collection, exact=True).count
        except Exception:
            return 0

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunk and vector counts differ")

        self._client.upsert(
            collection_name=self._collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=chunk.point_id, vector=list(vector), payload=chunk.payload()
                )
                for chunk, vector in zip(chunks, vectors)
            ],
        )
        return len(chunks)

    def delete_invoice(self, invoice_id: str) -> None:
        self._client.delete(
            collection_name=self._collection,
            wait=True,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="invoice_id", match=models.MatchValue(value=invoice_id)
                        )
                    ]
                )
            ),
        )

    def drop(self) -> None:
        if self._client.collection_exists(self._collection):
            self._client.delete_collection(self._collection)

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 6,
        score_threshold: float | None = None,
        invoice_id: str | None = None,
        vendor: str | None = None,
        language: str | None = None,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[models.FieldCondition] = []
        for key, value in (
            ("invoice_id", invoice_id),
            ("vendor", vendor),
            ("language", language),
            ("source_type", source_type),
        ):
            if value:
                conditions.append(
                    models.FieldCondition(key=key, match=models.MatchValue(value=value))
                )

        response = self._client.query_points(
            collection_name=self._collection,
            query=list(vector),
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
            query_filter=models.Filter(must=conditions) if conditions else None,
        )
        return [
            {"id": str(point.id), "score": float(point.score), **(point.payload or {})}
            for point in response.points
        ]

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
