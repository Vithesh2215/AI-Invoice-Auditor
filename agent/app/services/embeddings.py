from __future__ import annotations

import json
import logging
from typing import Protocol, Sequence

from ..settings import Settings

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class MockEmbedder:
    """Mock embedder for development/testing when LLM is disabled."""
    def __init__(self, dimension: int = 384) -> None:
        self.name = "mock"
        self.dimension = dimension
        logger.info(
            "embedding model ready",
            extra={"provider": self.name, "dimension": self.dimension},
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Return mock embeddings (zeros) of correct dimension."""
        return [[0.0] * self.dimension for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        """Return mock query embedding (zeros) of correct dimension."""
        return [0.0] * self.dimension


class FastEmbedEmbedder:
    def __init__(self, settings: Settings) -> None:
        from fastembed import TextEmbedding

        self.name = f"fastembed/{settings.embedding_model}"
        self._model = TextEmbedding(
            model_name=settings.embedding_model,
            cache_dir=settings.embedding_cache_dir,
        )
        probe = next(iter(self._model.embed(["dimension probe"])))
        self.dimension = len(probe)
        logger.info(
            "embedding model ready",
            extra={"provider": self.name, "dimension": self.dimension},
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return [vector.tolist() for vector in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed([text]))).tolist()


class BedrockEmbedder:
    def __init__(self, settings: Settings) -> None:
        import boto3

        self.name = f"bedrock/{settings.bedrock_embedding_model}"
        self._model_id = settings.bedrock_embedding_model
        self.dimension = settings.embedding_dim

        kwargs = {"region_name": settings.aws_region}
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            kwargs["aws_access_key_id"] = settings.aws_access_key_id.get_secret_value()
            kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
            if settings.aws_session_token:
                kwargs["aws_session_token"] = settings.aws_session_token.get_secret_value()
        self._client = boto3.client("bedrock-runtime", **kwargs)

        self.dimension = len(self._embed_one("dimension probe", "search_document"))
        logger.info(
            "embedding model ready",
            extra={"provider": self.name, "dimension": self.dimension},
        )

    def _embed_one(self, text: str, input_type: str) -> list[float]:
        if self._model_id.startswith("cohere."):
            body = {"texts": [text], "input_type": input_type}
        else:
            body = {"inputText": text}

        response = self._client.invoke_model(
            modelId=self._model_id, body=json.dumps(body)
        )
        payload = json.loads(response["body"].read())

        vector = payload.get("embedding") or payload.get("embeddings")
        if vector and isinstance(vector[0], list):
            vector = vector[0]
        return list(vector)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text, "search_document") for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text, "search_query")


def build_embedder(settings: Settings) -> Embedder:
    if not settings.llm_enabled:
        return MockEmbedder(settings.embedding_dim)
    if settings.embedding_provider == "bedrock":
        return BedrockEmbedder(settings)
    return FastEmbedEmbedder(settings)
