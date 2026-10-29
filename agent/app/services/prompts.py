
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    system: str
    model_alias: str = "reasoning"
    temperature: float = 0.0
    max_tokens: int = 1200
    response_format: str = "text"

    @property
    def expects_json(self) -> bool:
        return self.response_format == "json"


class PromptRegistry:
    def __init__(self, personas: dict[str, Persona], templates: dict[str, str]) -> None:
        self._personas = personas
        self._templates = templates

    def persona(self, key: str) -> Persona:
        try:
            return self._personas[key]
        except KeyError:
            raise RuntimeError(
                f"persona '{key}' is not defined in config/personas.yaml"
            ) from None

    def render(self, key: str, **values: Any) -> str:
        try:
            template = self._templates[key]
        except KeyError:
            raise RuntimeError(
                f"prompt template '{key}' is not defined in config/prompts.yaml"
            ) from None
        return template.format(**values)

    @property
    def persona_keys(self) -> list[str]:
        return sorted(self._personas)


@lru_cache(maxsize=4)
def load_prompts(personas_path: str | Path, prompts_path: str | Path) -> PromptRegistry:
    personas_doc = _read_yaml(personas_path)
    prompts_doc = _read_yaml(prompts_path)

    defaults = personas_doc.get("defaults", {}) or {}
    personas: dict[str, Persona] = {}
    for key, spec in (personas_doc.get("personas") or {}).items():
        merged = {**defaults, **(spec or {})}
        personas[key] = Persona(
            key=key,
            name=merged.get("name", key),
            system=(merged.get("system") or "").strip(),
            model_alias=merged.get("model_alias", "reasoning"),
            temperature=float(merged.get("temperature", 0.0)),
            max_tokens=int(merged.get("max_tokens", 1200)),
            response_format=merged.get("response_format", "text"),
        )

    templates = {k: str(v) for k, v in (prompts_doc.get("templates") or {}).items()}
    logger.info(
        "prompt registry loaded",
        extra={"personas": sorted(personas), "templates": sorted(templates)},
    )
    return PromptRegistry(personas, templates)


def _read_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"configuration file not found at {path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}
