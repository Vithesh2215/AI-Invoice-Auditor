
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..models import Discrepancy, InvoiceFields, Severity
from ..services.rules import Rules
from .utils import percent_difference, ZERO


HEADER_FIELD_MAP: dict[str, str] = {
    "invoice_no": "invoice_number",
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "vendor_id": "vendor_id",
    "vendor_name": "vendor_name",
    "po_number": "po_number",
    "currency": "currency",
    "total_amount": "total_amount",
    "subtotal": "subtotal_amount",
    "tax_amount": "tax_amount",
}

LINE_FIELD_MAP: dict[str, str] = {
    "item_code": "item_code",
    "description": "description",
    "qty": "quantity",
    "quantity": "quantity",
    "unit_price": "unit_price",
    "total": "line_total",
    "line_total": "line_total",
}

MISSING_FIELD_SEVERITY_KEYS = {
    "invoice_number": "missing_invoice_number",
    "po_number": "missing_po_number",
}


@dataclass
class DataChecks:
    """Everything the data validator computed, in a shape fit for a prompt."""

    missing_header_fields: list[str] = field(default_factory=list)
    missing_line_fields: list[dict[str, Any]] = field(default_factory=list)
    line_total_mismatches: list[dict[str, Any]] = field(default_factory=list)
    subtotal_check: dict[str, Any] | None = None
    total_check: dict[str, Any] | None = None
    tax_check: dict[str, Any] | None = None
    currency_check: dict[str, Any] | None = None
    line_item_count: int = 0
    arithmetic_consistent: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "missing_header_fields": self.missing_header_fields,
            "missing_line_fields": self.missing_line_fields,
            "line_total_mismatches": self.line_total_mismatches,
            "subtotal_check": self.subtotal_check,
            "total_check": self.total_check,
            "tax_check": self.tax_check,
            "currency_check": self.currency_check,
            "line_item_count": self.line_item_count,
            "arithmetic_consistent": self.arithmetic_consistent,
        }


def run_data_checks(
    fields: InvoiceFields, rules: Rules
) -> tuple[DataChecks, list[Discrepancy]]:
    checks = DataChecks(line_item_count=len(fields.line_items))
    findings: list[Discrepancy] = []

    _check_required_header(fields, rules, checks, findings)
    _check_required_lines(fields, rules, checks, findings)
    _check_line_arithmetic(fields, rules, checks, findings)
    _check_document_arithmetic(fields, rules, checks, findings)
    _check_currency(fields, rules, checks, findings)

    checks.arithmetic_consistent = not any(
        f.field_name.startswith(("line_total", "subtotal", "total_amount", "tax_amount"))
        for f in findings
    )
    return checks, findings




def _check_required_header(
    fields: InvoiceFields, rules: Rules, checks: DataChecks, findings: list[Discrepancy]
) -> None:
    for rule_name in rules.required_header_fields:
        attribute = HEADER_FIELD_MAP.get(rule_name, rule_name)
        if getattr(fields, attribute, None) in (None, ""):
            checks.missing_header_fields.append(attribute)

            override_key = MISSING_FIELD_SEVERITY_KEYS.get(attribute)
            severity = (
                rules.severity_override(override_key)
                if override_key
                else None
            ) or Severity.MEDIUM

            findings.append(
                Discrepancy(
                    source="data",
                    field_name=attribute,
                    invoice_value=None,
                    erp_value=None,
                    severity=severity,
                    message=f"required field '{attribute}' is missing from the invoice",
                )
            )


def _check_required_lines(
    fields: InvoiceFields, rules: Rules, checks: DataChecks, findings: list[Discrepancy]
) -> None:
    required = rules.required_line_fields
    if not fields.line_items:
        findings.append(
            Discrepancy(
                source="data",
                field_name="line_items",
                severity=Severity.HIGH,
                message="no line items could be read from the invoice",
            )
        )
        return

    for item in fields.line_items:
        missing = [
            LINE_FIELD_MAP.get(name, name)
            for name in required
            if getattr(item, LINE_FIELD_MAP.get(name, name), None) in (None, "")
        ]
        if missing:
            checks.missing_line_fields.append(
                {"line_number": item.line_number, "missing": missing}
            )
            findings.append(
                Discrepancy(
                    source="data",
                    field_name="line_item_fields",
                    line_number=item.line_number,
                    severity=Severity.MEDIUM,
                    message=f"line {item.line_number} is missing {', '.join(missing)}",
                )
            )




def _check_line_arithmetic(
    fields: InvoiceFields, rules: Rules, checks: DataChecks, findings: list[Discrepancy]
) -> None:
    tolerance = rules.abs_tolerance("line_total_abs_tolerance", "0.02")

    for item in fields.line_items:
        if item.quantity is None or item.unit_price is None or item.line_total is None:
            continue
        expected = (item.quantity * item.unit_price).quantize(Decimal("0.01"))
        difference = (item.line_total - expected).copy_abs()
        if difference <= tolerance:
            continue

        deviation = percent_difference(difference, expected)
        checks.line_total_mismatches.append(
            {
                "line_number": item.line_number,
                "stated": str(item.line_total),
                "expected": str(expected),
                "difference": str(difference),
            }
        )
        findings.append(
            Discrepancy(
                source="data",
                field_name="line_total",
                line_number=item.line_number,
                invoice_value=str(item.line_total),
                erp_value=str(expected),
                deviation_pct=deviation,
                severity=rules.severity_for_deviation(deviation),
                message=(
                    f"line {item.line_number}: {item.quantity} x {item.unit_price} "
                    f"is {expected}, the invoice states {item.line_total}"
                ),
            )
        )


def _check_document_arithmetic(
    fields: InvoiceFields, rules: Rules, checks: DataChecks, findings: list[Discrepancy]
) -> None:
    line_sum = sum(
        (item.line_total for item in fields.line_items if item.line_total is not None),
        ZERO,
    )

    if fields.line_items and fields.subtotal_amount is not None:
        tolerance = rules.abs_tolerance("subtotal_abs_tolerance", "0.05")
        difference = (fields.subtotal_amount - line_sum).copy_abs()
        checks.subtotal_check = {
            "stated": str(fields.subtotal_amount),
            "sum_of_lines": str(line_sum),
            "difference": str(difference),
            "within_tolerance": difference <= tolerance,
        }
        if difference > tolerance:
            deviation = percent_difference(difference, line_sum)
            findings.append(
                Discrepancy(
                    source="data",
                    field_name="subtotal_amount",
                    invoice_value=str(fields.subtotal_amount),
                    erp_value=str(line_sum),
                    deviation_pct=deviation,
                    severity=rules.severity_for_deviation(deviation),
                    message=(
                        f"line items sum to {line_sum} but the invoice states a "
                        f"subtotal of {fields.subtotal_amount}"
                    ),
                )
            )

    base = fields.subtotal_amount if fields.subtotal_amount is not None else line_sum
    tax = fields.tax_amount or ZERO

    if fields.total_amount is not None and base:
        tolerance = rules.abs_tolerance("total_abs_tolerance", "0.05")
        expected = (base + tax).quantize(Decimal("0.01"))
        difference = (fields.total_amount - expected).copy_abs()
        checks.total_check = {
            "stated": str(fields.total_amount),
            "expected": str(expected),
            "difference": str(difference),
            "within_tolerance": difference <= tolerance,
        }
        if difference > tolerance:
            deviation = percent_difference(difference, expected)
            findings.append(
                Discrepancy(
                    source="data",
                    field_name="total_amount",
                    invoice_value=str(fields.total_amount),
                    erp_value=str(expected),
                    deviation_pct=deviation,
                    severity=rules.severity_for_deviation(deviation),
                    message=(
                        f"subtotal {base} plus tax {tax} is {expected}, the invoice "
                        f"states a total of {fields.total_amount}"
                    ),
                )
            )

    _check_tax(fields, rules, checks, findings, base)


def _check_tax(
    fields: InvoiceFields,
    rules: Rules,
    checks: DataChecks,
    findings: list[Discrepancy],
    base: Decimal,
) -> None:
    if fields.tax_amount is None or not base:
        return

    stated_rate = fields.extras.get("stated_tax_rate_percent")
    rate = Decimal(str(stated_rate)) if stated_rate else rules.default_tax_rate_pct
    expected = (base * rate / Decimal("100")).quantize(Decimal("0.01"))
    difference = (fields.tax_amount - expected).copy_abs()
    deviation = percent_difference(difference, expected)
    tolerance_pct = rules.tax_tolerance_pct

    checks.tax_check = {
        "rate_percent": str(rate),
        "stated": str(fields.tax_amount),
        "expected": str(expected),
        "deviation_percent": str(deviation),
        "tolerance_percent": str(tolerance_pct),
        "within_tolerance": deviation <= tolerance_pct,
    }

    if deviation > tolerance_pct:
        findings.append(
            Discrepancy(
                source="data",
                field_name="tax_amount",
                invoice_value=str(fields.tax_amount),
                erp_value=str(expected),
                deviation_pct=deviation,
                tolerance_pct=tolerance_pct,
                severity=rules.severity_for_deviation(deviation),
                message=(
                    f"tax of {fields.tax_amount} does not match {rate}% of {base} "
                    f"({expected})"
                ),
            )
        )


def _check_currency(
    fields: InvoiceFields, rules: Rules, checks: DataChecks, findings: list[Discrepancy]
) -> None:
    accepted = rules.accepted_currencies
    checks.currency_check = {
        "invoice_currency": fields.currency,
        "accepted": sorted(accepted),
        "is_accepted": bool(fields.currency and fields.currency.upper() in accepted),
    }

    if fields.currency and fields.currency.upper() not in accepted:
        findings.append(
            Discrepancy(
                source="data",
                field_name="currency",
                invoice_value=fields.currency,
                erp_value=", ".join(sorted(accepted)),
                severity=rules.severity_override("unaccepted_currency") or Severity.CRITICAL,
                message=(
                    f"currency '{fields.currency}' is not in the accepted list; "
                    "policy for this case is to reject"
                ),
            )
        )

