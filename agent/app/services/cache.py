
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis

from ..settings import Settings

logger = logging.getLogger(__name__)


def cache_key(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


class Cache:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = redis.Redis.from_url(
            settings.redis_url,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            decode_responses=True,
            health_check_interval=30,
        )
        self.hits = 0
        self.misses = 0

    def verify(self) -> None:
        self._client.ping()

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def get_json(self, key: str) -> Any | None:
        try:
            payload = self._client.get(key)
        except Exception as exc:
            logger.warning("cache read failed", extra={"error": type(exc).__name__})
            return None
        if payload is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def set_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        try:
            self._client.setex(key, ttl_seconds, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("cache write failed", extra={"error": type(exc).__name__})

    def stats(self) -> dict[str, Any]:
        try:
            info = self._client.info("memory")
            keys = self._client.dbsize()
        except Exception:
            return {"available": False, "hits": self.hits, "misses": self.misses}
        return {
            "available": True,
            "keys": keys,
            "used_memory_human": info.get("used_memory_human"),
            "hits": self.hits,
            "misses": self.misses,
        }

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
