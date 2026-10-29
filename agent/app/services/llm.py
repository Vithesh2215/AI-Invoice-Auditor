
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import litellm
from litellm import completion as litellm_completion

from ..settings import Settings
from .cache import Cache, cache_key
from .prompts import Persona

logger = logging.getLogger(__name__)

litellm.drop_params = True
litellm.suppress_debug_info = True
litellm.telemetry = False

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _exception_types(*names: str) -> tuple[type[BaseException], ...]:
    found = tuple(
        exc for name in names
        if isinstance(exc := getattr(litellm, name, None), type) and issubclass(exc, BaseException)
    )
    return found or (NotImplementedError,)


FATAL_ERRORS = _exception_types(
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "BadRequestError",
    "UnsupportedParamsError",
)


class LLMUnavailable(RuntimeError):
    """Raised when the model gateway cannot produce a response."""


class LLMClient:
    def __init__(self, settings: Settings, cache: Cache | None = None) -> None:
        self._settings = settings
        self._cache = cache
        self._models = {
            "reasoning": settings.llm_reasoning_model,
            "translation": settings.llm_translation_model,
            "rag": settings.llm_rag_model,
        }
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self._breaker_until = 0.0
        self._breaker_reason = ""

    def model_for(self, alias: str) -> str:
        return self._models.get(alias, self._settings.llm_reasoning_model)

    def _credentials(self) -> dict[str, Any]:
        settings = self._settings
        creds: dict[str, Any] = {"aws_region_name": settings.aws_region}
        if settings.aws_access_key_id:
            creds["aws_access_key_id"] = settings.aws_access_key_id.get_secret_value()
        if settings.aws_secret_access_key:
            creds["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
        if settings.aws_session_token:
            creds["aws_session_token"] = settings.aws_session_token.get_secret_value()
        return creds

    def verify(self) -> dict[str, Any]:
        """One cheap round trip, used by the health endpoint."""
        if not self._settings.llm_enabled:
            return {"enabled": False, "detail": "LLM_ENABLED=false"}
        self._breaker_until = 0.0
        started = time.perf_counter()
        text = self._invoke(
            model=self.model_for("reasoning"),
            system="Reply with the single word OK.",
            user="ping",
            temperature=0.0,
            max_tokens=5,
        )
        return {
            "enabled": True,
            "model": self.model_for("reasoning"),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "reply": text.strip()[:20],
        }

    def complete(
        self,
        persona: Persona,
        user_prompt: str,
        *,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        cache_namespace: str = "llm",
    ) -> str:
        if not self._settings.llm_enabled:
            raise LLMUnavailable("LLM calls are disabled by configuration")

        model = self.model_for(persona.model_alias)
        key = cache_key(
            cache_namespace,
            model,
            persona.key,
            str(persona.temperature),
            persona.system,
            user_prompt,
        )

        if use_cache and self._cache is not None:
            cached = self._cache.get_json(key)
            if isinstance(cached, dict) and "text" in cached:
                self.cache_hits += 1
                logger.debug("llm cache hit", extra={"persona": persona.key})
                return cached["text"]

        text = self._invoke(
            model=model,
            system=persona.system,
            user=user_prompt,
            temperature=persona.temperature,
            max_tokens=persona.max_tokens,
        )

        if use_cache and self._cache is not None:
            self._cache.set_json(
                key,
                {"text": text, "model": model},
                cache_ttl or self._settings.llm_cache_ttl_seconds,
            )
        return text

    def complete_json(
        self,
        persona: Persona,
        user_prompt: str,
        *,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        cache_namespace: str = "llm",
    ) -> dict[str, Any]:
        raw = self.complete(
            persona,
            user_prompt,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
            cache_namespace=cache_namespace,
        )
        parsed = parse_json_object(raw)
        if parsed is None:
            raise LLMUnavailable(
                f"persona '{persona.key}' did not return parseable JSON"
            )
        return parsed

    def _invoke(
        self, *, model: str, system: str, user: str, temperature: float, max_tokens: int
    ) -> str:
        remaining = self._breaker_until - time.monotonic()
        if remaining > 0:
            raise LLMUnavailable(
                f"{model} is circuit-broken for another {int(remaining)}s after "
                f"{self._breaker_reason}"
            )

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        attempts = 0

        for attempt in range(1, self._settings.llm_max_retries + 1):
            attempts = attempt
            try:
                self.calls += 1
                response = litellm_completion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=self._settings.llm_timeout_seconds,
                    num_retries=0,
                    **self._credentials(),
                )
                content = response["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        block.get("text", "") for block in content if isinstance(block, dict)
                    )
                if not content or not str(content).strip():
                    raise LLMUnavailable("model returned an empty response")
                self._breaker_until = 0.0
                return str(content)
            except Exception as exc:
                last_error = exc
                self.failures += 1
                fatal = isinstance(exc, FATAL_ERRORS)
                logger.warning(
                    "llm call failed",
                    extra={
                        "model": model,
                        "attempt": attempt,
                        "fatal": fatal,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:200],
                    },
                )
                if fatal:
                    self._open_breaker(type(exc).__name__)
                    break
                if attempt < self._settings.llm_max_retries:
                    time.sleep(min(2**attempt, 8))

        raise LLMUnavailable(
            f"{model} did not respond after {attempts} attempt(s): "
            f"{type(last_error).__name__}"
        ) from None

    def _open_breaker(self, reason: str) -> None:
        window = self._settings.llm_circuit_breaker_seconds
        if window <= 0:
            return
        self._breaker_until = time.monotonic() + window
        self._breaker_reason = reason
        logger.error(
            "llm circuit opened",
            extra={"reason": reason, "seconds": window},
        )


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or code fences often enough that a bare
    json.loads is not reliable on its own.
    """
    if not raw:
        return None

    candidates: list[str] = [raw.strip()]

    fenced = _JSON_FENCE.search(raw)
    if fenced:
        candidates.insert(0, fenced.group(1))

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None
