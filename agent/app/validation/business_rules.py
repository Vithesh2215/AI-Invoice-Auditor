
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from ..models import Discrepancy, InvoiceFields, LineItem, Severity
from ..services.rules import Rules
from .utils import percent_difference, ZERO

DESCRIPTION_MATCH_FLOOR = 0.55




@dataclass
class BusinessComparison:
    po_found: bool = False
    vendor_found: bool = False
    po_number: str | None = None
    po_vendor_id: str | None = None
    po_vendor_name: str | None = None
    resolved_vendor_id: str | None = None
    matched_lines: list[dict[str, Any]] = field(default_factory=list)
    unmatched_invoice_lines: list[int] = field(default_factory=list)
    unmatched_po_lines: list[int] = field(default_factory=list)
    po_total: Decimal | None = None
    invoice_total: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "po_found": self.po_found,
            "vendor_found": self.vendor_found,
            "po_number": self.po_number,
            "po_vendor_id": self.po_vendor_id,
            "po_vendor_name": self.po_vendor_name,
            "resolved_vendor_id": self.resolved_vendor_id,
            "matched_lines": self.matched_lines,
            "unmatched_invoice_lines": self.unmatched_invoice_lines,
            "unmatched_po_lines": self.unmatched_po_lines,
            "po_total": str(self.po_total) if self.po_total is not None else None,
            "invoice_total": str(self.invoice_total) if self.invoice_total is not None else None,
        }


def match_lines(
    invoice_lines: list[LineItem], po_lines: list[dict[str, Any]]
) -> tuple[list[tuple[LineItem, dict[str, Any], str]], list[LineItem], list[dict[str, Any]]]:
    """Pair invoice lines with PO lines.

    Item code is the reliable key, but several invoices in the dataset print no
    item code at all, so description similarity and finally position are used
    as fallbacks rather than giving up on the comparison.
    """
    remaining = list(po_lines)
    matched: list[tuple[LineItem, dict[str, Any], str]] = []
    unmatched_invoice: list[LineItem] = []

    pending: list[LineItem] = []

    for line in invoice_lines:
        if not line.item_code:
            pending.append(line)
            continue
        code = line.item_code.upper()
        hit = next(
            (p for p in remaining if str(p.get("item_code", "")).upper() == code), None
        )
        if hit is not None:
            remaining.remove(hit)
            matched.append((line, hit, "item_code"))
        else:
            pending.append(line)

    for line in pending:
        best, best_score = None, 0.0
        if line.description:
            for candidate in remaining:
                score = SequenceMatcher(
                    None,
                    line.description.lower(),
                    str(candidate.get("description", "")).lower(),
                ).ratio()
                if score > best_score:
                    best, best_score = candidate, score

        if best is not None and best_score >= DESCRIPTION_MATCH_FLOOR:
            remaining.remove(best)
            matched.append((line, best, "description"))
        elif len(invoice_lines) == 1 and len(remaining) == 1:
            matched.append((line, remaining[0], "sole_line"))
            remaining = []
        else:
            unmatched_invoice.append(line)

    matched.sort(key=lambda pair: pair[0].line_number)
    return matched, unmatched_invoice, remaining


def compare_with_erp(
    fields: InvoiceFields,
    *,
    purchase_order: dict[str, Any] | None,
    vendor: dict[str, Any] | None,
    sku_lookup: dict[str, dict[str, Any] | None],
    rules: Rules,
) -> tuple[BusinessComparison, list[Discrepancy]]:
    comparison = BusinessComparison(
        po_number=fields.po_number,
        invoice_total=fields.total_amount,
        vendor_found=vendor is not None,
        resolved_vendor_id=(vendor or {}).get("vendor_id"),
    )
    findings: list[Discrepancy] = []

    if vendor is None:
        findings.append(
            Discrepancy(
                field_name="vendor",
                invoice_value=fields.vendor_name or fields.vendor_id,
                severity=rules.severity_override("vendor_not_found") or Severity.HIGH,
                message=(
                    f"vendor '{fields.vendor_name or fields.vendor_id}' is not in the "
                    "ERP vendor master"
                ),
            )
        )

    if purchase_order is None:
        findings.append(
            Discrepancy(
                field_name="po_number",
                invoice_value=fields.po_number,
                severity=rules.severity_override("po_not_found") or Severity.CRITICAL,
                message=(
                    f"purchase order '{fields.po_number}' was not found in the ERP system"
                    if fields.po_number
                    else "the invoice references no purchase order, so it cannot be matched"
                ),
            )
        )
        return comparison, findings

    comparison.po_found = True
    comparison.po_vendor_id = purchase_order.get("vendor_id")
    comparison.po_vendor_name = purchase_order.get("vendor_name")

    _check_vendor_matches_po(fields, vendor, purchase_order, comparison, rules, findings)
    _check_currency(fields, purchase_order, rules, findings)

    po_lines = list(purchase_order.get("line_items") or [])
    matched, unmatched_invoice, unmatched_po = match_lines(fields.line_items, po_lines)

    comparison.unmatched_invoice_lines = [line.line_number for line in unmatched_invoice]
    comparison.unmatched_po_lines = [
        int(line.get("line_number", 0)) for line in unmatched_po
    ]

    for line, po_line, strategy in matched:
        comparison.matched_lines.append(
            {
                "invoice_line": line.line_number,
                "po_line": po_line.get("line_number"),
                "item_code": po_line.get("item_code"),
                "matched_on": strategy,
            }
        )
        _compare_line(line, po_line, rules, findings)

    for line in unmatched_invoice:
        findings.append(
            Discrepancy(
                field_name="line_not_on_po",
                line_number=line.line_number,
                invoice_value=f"{line.item_code or line.description} x {line.quantity}",
                erp_value=None,
                severity=rules.severity_override("line_not_on_po") or Severity.HIGH,
                message=(
                    f"line {line.line_number} ({line.item_code or line.description}) "
                    f"does not appear on purchase order {fields.po_number}"
                ),
            )
        )

    for po_line in unmatched_po:
        findings.append(
            Discrepancy(
                field_name="po_line_not_invoiced",
                invoice_value=None,
                erp_value=f"{po_line.get('item_code')} x {po_line.get('qty')}",
                severity=Severity.LOW,
                message=(
                    f"purchase order line {po_line.get('line_number')} "
                    f"({po_line.get('item_code')}) was not invoiced"
                ),
            )
        )

    _check_unknown_skus(fields, sku_lookup, rules, findings)

    comparison.po_total = sum(
        (_dec(line.get("line_total")) or ZERO for line in po_lines), ZERO
    )
    return comparison, findings




def _check_vendor_matches_po(
    fields: InvoiceFields,
    vendor: dict[str, Any] | None,
    purchase_order: dict[str, Any],
    comparison: BusinessComparison,
    rules: Rules,
    findings: list[Discrepancy],
) -> None:
    po_vendor_id = purchase_order.get("vendor_id")
    invoice_vendor_id = (vendor or {}).get("vendor_id") or fields.vendor_id
    if not po_vendor_id or not invoice_vendor_id:
        return
    if str(po_vendor_id).upper() == str(invoice_vendor_id).upper():
        return

    findings.append(
        Discrepancy(
            field_name="vendor_id",
            invoice_value=f"{invoice_vendor_id} ({fields.vendor_name})",
            erp_value=f"{po_vendor_id} ({purchase_order.get('vendor_name')})",
            severity=rules.severity_override("vendor_mismatch") or Severity.HIGH,
            message=(
                f"the invoice was raised by {invoice_vendor_id} but purchase order "
                f"{purchase_order.get('po_number')} belongs to {po_vendor_id}"
            ),
        )
    )


def _check_currency(
    fields: InvoiceFields,
    purchase_order: dict[str, Any],
    rules: Rules,
    findings: list[Discrepancy],
) -> None:
    po_currencies = {
        str(c).upper() for c in (purchase_order.get("currencies") or []) if c
    }
    if not po_currencies:
        return

    if not fields.currency:
        findings.append(
            Discrepancy(
                field_name="currency",
                invoice_value=None,
                erp_value=", ".join(sorted(po_currencies)),
                severity=Severity.MEDIUM,
                message=(
                    "no currency could be read from the invoice; the purchase order "
                    f"is denominated in {', '.join(sorted(po_currencies))}"
                ),
            )
        )
        return

    if fields.currency.upper() not in po_currencies:
        findings.append(
            Discrepancy(
                field_name="currency",
                invoice_value=fields.currency,
                erp_value=", ".join(sorted(po_currencies)),
                severity=rules.severity_override("currency_mismatch") or Severity.CRITICAL,
                message=(
                    f"the invoice is in {fields.currency} but the purchase order is in "
                    f"{', '.join(sorted(po_currencies))}; no conversion is applied"
                ),
            )
        )


def _compare_line(
    line: LineItem,
    po_line: dict[str, Any],
    rules: Rules,
    findings: list[Discrepancy],
) -> None:
    po_qty = _dec(po_line.get("qty"))
    po_price = _dec(po_line.get("unit_price"))
    po_currency = po_line.get("currency")
    item_code = po_line.get("item_code")

    if line.quantity is not None and po_qty is not None:
        deviation = percent_difference(line.quantity - po_qty, po_qty)
        tolerance = rules.quantity_tolerance_pct
        if deviation > tolerance:
            findings.append(
                Discrepancy(
                    field_name="quantity",
                    line_number=line.line_number,
                    invoice_value=str(line.quantity),
                    erp_value=str(po_qty),
                    deviation_pct=deviation,
                    tolerance_pct=tolerance,
                    severity=rules.severity_for_deviation(deviation),
                    message=(
                        f"line {line.line_number} ({item_code}): invoiced quantity "
                        f"{line.quantity} against {po_qty} on the purchase order"
                    ),
                )
            )

    if line.unit_price is not None and po_price is not None:
        deviation = percent_difference(line.unit_price - po_price, po_price)
        tolerance = rules.price_tolerance_pct
        if deviation > tolerance:
            overcharge = ((line.unit_price - po_price) * (line.quantity or Decimal("0"))).quantize(
                Decimal("0.01")
            )
            findings.append(
                Discrepancy(
                    field_name="unit_price",
                    line_number=line.line_number,
                    invoice_value=str(line.unit_price),
                    erp_value=str(po_price),
                    deviation_pct=deviation,
                    tolerance_pct=tolerance,
                    severity=rules.severity_for_deviation(deviation),
                    message=(
                        f"line {line.line_number} ({item_code}): invoiced at "
                        f"{line.unit_price} against {po_price} on the purchase order, "
                        f"a difference of {overcharge} on this line"
                    ),
                )
            )

    if line.currency and po_currency and line.currency.upper() != str(po_currency).upper():
        findings.append(
            Discrepancy(
                field_name="line_currency",
                line_number=line.line_number,
                invoice_value=line.currency,
                erp_value=str(po_currency),
                severity=rules.severity_override("currency_mismatch") or Severity.CRITICAL,
                message=(
                    f"line {line.line_number} is billed in {line.currency} but the "
                    f"purchase order line is in {po_currency}"
                ),
            )
        )


def _check_unknown_skus(
    fields: InvoiceFields,
    sku_lookup: dict[str, dict[str, Any] | None],
    rules: Rules,
    findings: list[Discrepancy],
) -> None:
    for line in fields.line_items:
        if not line.item_code:
            continue
        if sku_lookup.get(line.item_code.upper(), "missing") is None:
            findings.append(
                Discrepancy(
                    field_name="item_code",
                    line_number=line.line_number,
                    invoice_value=line.item_code,
                    erp_value=None,
                    severity=rules.severity_override("unknown_item_code") or Severity.HIGH,
                    message=(
                        f"item code '{line.item_code}' on line {line.line_number} is "
                        "not in the ERP SKU master"
                    ),
                )
            )


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None

