
from __future__ import annotations

import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..models import SEVERITY_ORDER, Recommendation, Severity

logger = logging.getLogger(__name__)


class Rules:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def section(self, name: str, default: Any = None) -> Any:
        return self._data.get(name, default if default is not None else {})

    @property
    def required_header_fields(self) -> list[str]:
        return list(self.section("required_fields").get("header", []))

    @property
    def required_line_fields(self) -> list[str]:
        return list(self.section("required_fields").get("line_item", []))

    @property
    def accepted_currencies(self) -> set[str]:
        return {c.upper() for c in self.section("accepted_currencies", [])}

    @property
    def currency_symbol_map(self) -> dict[str, str]:
        return dict(self.section("currency_symbol_map", {}))

    def tolerance_pct(self, name: str, default: float = 0.0) -> Decimal:
        value = self.section("tolerances").get(name, default)
        return Decimal(str(value))

    @property
    def price_tolerance_pct(self) -> Decimal:
        return self.tolerance_pct("price_difference_percent", 5)

    @property
    def quantity_tolerance_pct(self) -> Decimal:
        return self.tolerance_pct("quantity_difference_percent", 0)

    @property
    def tax_tolerance_pct(self) -> Decimal:
        return self.tolerance_pct("tax_difference_percent", 2)

    def abs_tolerance(self, name: str, default: str = "0.05") -> Decimal:
        value = self.section("arithmetic").get(name, default)
        return Decimal(str(value))

    @property
    def default_tax_rate_pct(self) -> Decimal:
        return Decimal(str(self.section("arithmetic").get("default_tax_rate_percent", 10)))

    def severity_for_deviation(self, deviation_pct: Decimal | float | None) -> Severity:
        if deviation_pct is None:
            return Severity.MEDIUM
        deviation = abs(Decimal(str(deviation_pct)))
        for band in self.section("severity_bands", []):
            ceiling = band.get("max_deviation_percent")
            if ceiling is None or deviation <= Decimal(str(ceiling)):
                return Severity(band["severity"])
        return Severity.CRITICAL

    def severity_override(self, key: str) -> Severity | None:
        value = self.section("severity_overrides", {}).get(key)
        return Severity(value) if value else None

    @property
    def auto_approve_confidence(self) -> Decimal:
        policies = self.section("validation_policies", {})
        return Decimal(str(policies.get("auto_approve_confidence_threshold", 0.95)))

    def recommend(
        self,
        *,
        severities: list[Severity],
        translation_confidence: Decimal | None,
    ) -> tuple[Recommendation, str]:
        """Deterministic recommendation. The LLM explains this, never overrides it."""
        config = self.section("recommendation", {})
        reject_levels = {
            Severity(s) for s in config.get("reject_if", {}).get("any_severity", ["critical"])
        }
        review_levels = {
            Severity(s)
            for s in config.get("review_if", {}).get("any_severity", ["high", "medium"])
        }
        confidence_floor = Decimal(
            str(config.get("review_if", {}).get("below_translation_confidence", 0.85))
        )

        if any(s in reject_levels for s in severities):
            worst = max(severities, key=lambda s: SEVERITY_ORDER[s])
            return Recommendation.REJECT, f"a {worst.value} severity discrepancy was confirmed"

        if any(s in review_levels for s in severities):
            worst = max(severities, key=lambda s: SEVERITY_ORDER[s])
            return (
                Recommendation.REVIEW,
                f"{len(severities)} discrepancies were found, the most serious being {worst.value}",
            )

        if translation_confidence is not None and translation_confidence < confidence_floor:
            return (
                Recommendation.REVIEW,
                f"translation confidence {translation_confidence} is below the "
                f"{confidence_floor} floor for unattended approval",
            )

        if (
            translation_confidence is not None
            and translation_confidence < self.auto_approve_confidence
        ):
            return (
                Recommendation.APPROVE,
                "no discrepancies were found; translation confidence is acceptable",
            )

        return Recommendation.APPROVE, "no discrepancies were found"

    @property
    def rag(self) -> dict[str, Any]:
        return self.section("rag", {})

    def rag_value(self, key: str, default: Any) -> Any:
        return self.rag.get(key, default)


DEFAULT_RULES: dict[str, Any] = {
    "required_fields": {
        "header": ["invoice_no", "invoice_date", "vendor_id", "currency", "total_amount"],
        "line_item": ["item_code", "description", "qty", "unit_price", "total"],
    },
    "tolerances": {
        "price_difference_percent": 5,
        "quantity_difference_percent": 0,
        "tax_difference_percent": 2,
    },
    "accepted_currencies": ["USD", "EUR", "INR", "GBP"],
}


@lru_cache(maxsize=4)
def load_rules(path: str | Path) -> Rules:
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(
            f"rules file not found at {path}; mount config/ into the container"
        )
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    merged = {**DEFAULT_RULES, **data}
    logger.info("rules loaded", extra={"path": str(path), "sections": sorted(merged)})
    return Rules(merged)
