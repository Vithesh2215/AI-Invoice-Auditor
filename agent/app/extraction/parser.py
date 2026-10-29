
from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from ..models import InvoiceFields, LineItem

logger = logging.getLogger(__name__)


INVOICE_NUMBER_LABELS = (
    "invoice number", "invoice no", "invoice nr", "invoice #", "invoice num",
    "rechnungsnummer", "rechnungs-nr", "rechnung nr", "rechnung",
    "factura no", "numero de factura", "número de factura", "factura num",
    "no de factura", "n de factura", "factura", "facture no", "invoice",
    "debit note no", "bill no", "document no",
)

DATE_LABELS = (
    "invoice date", "date of issue", "issue date", "billing date",
    "rechnungsdatum", "belegdatum", "datum",
    "fecha de factura", "fecha de emision", "fecha de emisión", "fecha",
    "date",
)

PO_LABELS = (
    "po reference", "purchase order number", "purchase order no",
    "purchase order", "po number", "po no", "po ref", "p.o. no", "p.o.",
    "bestellnummer", "bestellung nr", "bestellung", "auftragsnummer",
    "referencia de pedido", "numero de pedido", "número de pedido",
    "orden de compra", "pedido", "po",
)

VENDOR_LABELS = (
    "vendor", "supplier", "seller", "from", "lieferant", "verkaufer",
    "verkäufer", "proveedor", "vendedor", "bill from",
)

SUBTOTAL_LABELS = (
    "subtotal", "sub total", "sub-total", "net amount", "net total",
    "zwischensumme", "nettobetrag", "netto", "importe neto", "base imponible",
)

TAX_LABELS = (
    "total vat", "tax amount", "sales tax", "vat amount", "gst amount",
    "mehrwertsteuer", "umsatzsteuer", "steuerbetrag",
    "impuesto", "tax", "vat", "iva", "gst", "mwst", "ust",
)

TOTAL_LABELS = (
    "total amount due", "total amount payable", "total including tax",
    "total incl. tax", "amount due", "balance due", "grand total",
    "total amount", "gesamtbetrag", "rechnungsbetrag", "endbetrag",
    "gesamtsumme", "total a pagar", "importe total", "total factura",
    "total general", "total",
)

CURRENCY_LABELS = ("currency", "waehrung", "währung", "moneda", "divisa")

LABEL_SUFFIX_BLOCKLIST = (" by", " to", " from", " address", " contact", " name")

LINE_HEADER_TOKENS = (
    "item code", "itemcode", "description", "qty", "quantity", "unit price",
    "unit", "amount", "line total", "artikelcode", "beschreibung", "menge",
    "preis", "gesamt", "codigo", "código", "descripcion", "descripción",
    "cantidad", "precio", "total",
)

ITEM_CODE_RE = re.compile(r"^[A-Z]{2,6}[-_ ]?\d{2,8}[A-Z]?$", re.IGNORECASE)
VENDOR_ID_RE = re.compile(r"\bVEND[-_ ]?\d{1,6}\b", re.IGNORECASE)
PO_VALUE_RE = re.compile(r"\bPO[-_ ]?\d{2,8}\b", re.IGNORECASE)
ISO_CURRENCY_RE = re.compile(r"\b(USD|EUR|INR|GBP|JPY|CHF|CAD|AUD|VND|SGD|AED)\b")
PERCENT_RE = re.compile(r"\(?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*\)?")
SEPARATOR_RE = re.compile(r"^[\s\-_=*.+~|]{4,}$")
AMOUNT_TOKEN_RE = re.compile(r"^[^\w\s]{0,2}[\d][\d.,\s']*[^\w\s]{0,2}$")
LABEL_SPLIT_RE = re.compile(r"^\s*([^:：]{1,60})[:：]\s*(.*)$")

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "₹": "INR", "£": "GBP", "¥": "JPY"}

DATE_LANGUAGES = ["en", "es", "de"]




def parse_amount(text: str | None) -> Decimal | None:
    """Parse a monetary token, inferring the decimal separator from context.

    ``1,470.00``, ``1.470,00``, ``1470.00€`` and ``$1,470`` all have to reduce
    to the same Decimal, and a float round trip would lose cents.
    """
    if text is None:
        return None

    cleaned = str(text).strip()
    if not cleaned:
        return None

    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = re.sub(r"[^\d.,\-']", "", cleaned).replace("'", "")
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return None

    has_dot, has_comma = "." in cleaned, "," in cleaned
    if has_dot and has_comma:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        tail = cleaned.rsplit(",", 1)[1]
        cleaned = cleaned.replace(",", "." if len(tail) in (1, 2) else "")
    elif has_dot:
        tail = cleaned.rsplit(".", 1)[1]
        if len(tail) == 3 and cleaned.count(".") > 1:
            cleaned = cleaned.replace(".", "")

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def is_amount_token(token: str) -> bool:
    if not token or not any(ch.isdigit() for ch in token):
        return False
    if any(ch.isalpha() for ch in token.replace("€", "").replace("£", "")):
        return False
    return bool(AMOUNT_TOKEN_RE.match(token.strip()))


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    import dateparser

    candidate = text.strip().strip(".,;")
    parsed = dateparser.parse(
        candidate,
        languages=DATE_LANGUAGES,
        settings={"DATE_ORDER": "DMY", "PREFER_DAY_OF_MONTH": "first", "STRICT_PARSING": False},
    )
    return parsed.date() if parsed else None




def _normalise_label(label: str) -> str:
    return re.sub(r"[^a-z0-9 .#/-]+", " ", label.lower()).strip()


def _match_label(label: str, candidates: Iterable[str]) -> bool:
    normalised = _normalise_label(label)
    if not normalised:
        return False
    if normalised.endswith(LABEL_SUFFIX_BLOCKLIST):
        return False
    for candidate in candidates:
        if normalised == candidate or normalised.endswith(" " + candidate):
            return True
        if normalised.startswith(candidate) and len(normalised) <= len(candidate) + 4:
            return True
    return False


def _iter_label_values(lines: list[str]) -> Iterable[tuple[str, str]]:
    for line in lines:
        match = LABEL_SPLIT_RE.match(line)
        if match:
            yield match.group(1).strip(), match.group(2).strip()




def _looks_like_header(line: str) -> bool:
    lowered = line.lower()
    hits = sum(1 for token in LINE_HEADER_TOKENS if token in lowered)
    return hits >= 2 and not any(ch.isdigit() for ch in lowered.replace("%", ""))


def _row_from_pipes(line: str) -> tuple[str | None, str | None, list[Decimal]] | None:
    cells = [cell.strip() for cell in line.split("|")]
    cells = [cell for cell in cells if cell]
    if len(cells) < 3:
        return None

    numeric: list[Decimal] = []
    text_cells: list[str] = []
    for cell in cells:
        if is_amount_token(cell):
            value = parse_amount(cell)
            if value is not None:
                numeric.append(value)
                continue
        text_cells.append(cell)

    if len(numeric) < 2 or not text_cells:
        return None

    item_code = text_cells[0] if ITEM_CODE_RE.match(text_cells[0]) else None
    description_cells = text_cells[1:] if item_code else text_cells
    description = " ".join(description_cells).strip() or None
    return item_code, description, numeric


def _row_from_whitespace(line: str) -> tuple[str | None, str | None, list[Decimal]] | None:
    tokens = line.split()
    if len(tokens) < 3:
        return None

    index = len(tokens)
    numeric: list[Decimal] = []
    while index > 0 and is_amount_token(tokens[index - 1]):
        value = parse_amount(tokens[index - 1])
        if value is None:
            break
        numeric.insert(0, value)
        index -= 1

    head = tokens[:index]
    if len(numeric) < 2 or not head:
        return None

    item_code = head[0] if ITEM_CODE_RE.match(head[0]) else None
    description = " ".join(head[1:] if item_code else head).strip() or None
    if description and _match_label(description, SUBTOTAL_LABELS + TAX_LABELS + TOTAL_LABELS):
        return None
    return item_code, description, numeric


def _build_line_item(
    line_number: int,
    item_code: str | None,
    description: str | None,
    numbers: list[Decimal],
    currency: str | None,
) -> LineItem | None:
    if len(numbers) >= 3:
        quantity, unit_price, line_total = numbers[-3], numbers[-2], numbers[-1]
    elif len(numbers) == 2:
        quantity, line_total = numbers
        unit_price = (line_total / quantity) if quantity else None
    else:
        return None

    if quantity is not None and quantity <= 0:
        return None
    if line_total is not None and line_total < 0:
        return None

    return LineItem(
        line_number=line_number,
        item_code=item_code.upper().replace(" ", "-") if item_code else None,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        currency=currency,
    )


def _extract_line_items(lines: list[str], currency: str | None) -> list[LineItem]:
    items: list[LineItem] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or SEPARATOR_RE.match(stripped) or _looks_like_header(stripped):
            continue

        parsed = _row_from_pipes(stripped) if "|" in stripped else _row_from_whitespace(stripped)
        if parsed is None:
            continue

        item_code, description, numbers = parsed
        if not item_code and not description:
            continue

        item = _build_line_item(len(items) + 1, item_code, description, numbers, currency)
        if item is not None:
            items.append(item)

    return items


def _narrative_line_item(label_values: dict[str, str], currency: str | None) -> LineItem | None:
    """Single-item invoices that spell the fields out instead of tabulating them.

    ``Description: Container Seals / Quantity: 200 units / Unit Price: $1.25``
    is a real layout in the dataset and produces no table rows at all.
    """
    description = label_values.get("description") or label_values.get("descripcion")
    quantity_text = label_values.get("quantity") or label_values.get("qty") or label_values.get("cantidad")
    price_text = label_values.get("unit price") or label_values.get("price") or label_values.get("precio")

    if not (description and quantity_text and price_text):
        return None

    quantity = parse_amount(quantity_text)
    unit_price = parse_amount(price_text)
    if quantity is None or unit_price is None:
        return None

    return LineItem(
        line_number=1,
        item_code=None,
        description=description.strip(),
        quantity=quantity,
        unit_price=unit_price,
        line_total=(quantity * unit_price).quantize(Decimal("0.01")),
        currency=currency,
    )




def detect_currency(text: str, symbol_map: dict[str, str] | None = None) -> str | None:
    iso = ISO_CURRENCY_RE.search(text)
    if iso:
        return iso.group(1)

    symbols = {**CURRENCY_SYMBOLS, **(symbol_map or {})}
    counts = {code: 0 for code in symbols.values()}
    for symbol, code in symbols.items():
        counts[code] += text.count(symbol)

    best = max(counts.items(), key=lambda item: item[1], default=(None, 0))
    return best[0] if best[1] > 0 else None


def parse_invoice(
    text: str,
    *,
    currency_symbol_map: dict[str, str] | None = None,
) -> InvoiceFields:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    non_empty = [line for line in lines if line.strip()]

    label_values: dict[str, str] = {}
    for label, value in _iter_label_values(non_empty):
        key = _normalise_label(label)
        if key and key not in label_values:
            label_values[key] = value

    fields = InvoiceFields()
    extras: dict[str, Any] = {}

    for label, value in label_values.items():
        if not value:
            continue
        if fields.invoice_number is None and _match_label(label, INVOICE_NUMBER_LABELS):
            fields.invoice_number = _clean_identifier(value)
        elif fields.po_number is None and _match_label(label, PO_LABELS):
            fields.po_number = _clean_identifier(value)
        elif fields.invoice_date is None and _match_label(label, DATE_LABELS):
            fields.invoice_date = parse_date(value)
        elif fields.vendor_name is None and _match_label(label, VENDOR_LABELS):
            fields.vendor_name = value.strip()[:255]
        elif fields.currency is None and _match_label(label, CURRENCY_LABELS):
            fields.currency = (ISO_CURRENCY_RE.search(value.upper()) or [None])[0]

    if not fields.po_number:
        match = PO_VALUE_RE.search(text or "")
        if match:
            fields.po_number = _clean_identifier(match.group(0))

    vendor_id_match = VENDOR_ID_RE.search(text or "")
    if vendor_id_match:
        fields.vendor_id = vendor_id_match.group(0).upper().replace(" ", "-").replace("_", "-")

    if not fields.vendor_name and non_empty:
        first = non_empty[0].strip()
        if not LABEL_SPLIT_RE.match(first) and len(first) <= 120:
            fields.vendor_name = first[:255]

    fields.currency = fields.currency or detect_currency(text or "", currency_symbol_map)

    fields.subtotal_amount = _amount_for(label_values, non_empty, SUBTOTAL_LABELS)
    fields.tax_amount = _amount_for(label_values, non_empty, TAX_LABELS)
    fields.total_amount = _amount_for(label_values, non_empty, TOTAL_LABELS, exclude=SUBTOTAL_LABELS)

    tax_rate = _first_percent(non_empty, TAX_LABELS)
    if tax_rate is not None:
        extras["stated_tax_rate_percent"] = str(tax_rate)

    items = _extract_line_items(non_empty, fields.currency)
    if not items:
        narrative = _narrative_line_item(label_values, fields.currency)
        if narrative is not None:
            items = [narrative]
            extras["line_item_layout"] = "narrative"

    fields.line_items = items

    if fields.total_amount is None and fields.subtotal_amount is not None:
        fields.total_amount = (
            fields.subtotal_amount + (fields.tax_amount or Decimal("0"))
        )

    extras["detected_labels"] = sorted(label_values)[:40]
    extras["line_item_count"] = len(items)
    fields.extras = extras
    return fields


def _clean_identifier(value: str) -> str | None:
    cleaned = value.strip().strip(".,;:").split()
    if not cleaned:
        return None
    token = cleaned[0].strip("()[]")
    return token[:64] or None


def _label_head(tokens: list[str], limit: int = 5) -> str | None:
    """Leading non-numeric tokens of a colon-less line, i.e. its label.

    The label has to be isolated from the amount before matching: a head of
    ``"Total 453,75 EUR"`` is rejected by :func:`_match_label`, whose length
    guard only tolerates a few characters beyond the candidate label, so
    including the amount would make every colon-less total unmatchable.
    """
    head: list[str] = []
    for token in tokens:
        if is_amount_token(token):
            return " ".join(head) if head else None
        if len(head) >= limit:
            return None
        head.append(token)
    return None


def _amount_for(
    label_values: dict[str, str],
    lines: list[str],
    labels: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> Decimal | None:
    for label, value in label_values.items():
        if exclude and _match_label(label, exclude):
            continue
        if _match_label(label, labels):
            amount = parse_amount(value)
            if amount is not None:
                return amount

    for line in lines:
        tokens = line.split()
        if len(tokens) < 2:
            continue
        head = _label_head(tokens)
        if head is None:
            continue
        if exclude and _match_label(head, exclude):
            continue
        if not _match_label(head, labels):
            continue
        for token in reversed(tokens):
            if is_amount_token(token):
                amount = parse_amount(token)
                if amount is not None:
                    return amount
    return None


def _first_percent(lines: list[str], labels: tuple[str, ...]) -> Decimal | None:
    for line in lines:
        head = _label_head(line.split())
        if (head is not None and _match_label(head, labels)) or any(
            label in line.lower() for label in ("tax (", "iva (", "mwst (", "vat (")
        ):
            match = PERCENT_RE.search(line)
            if match:
                return parse_amount(match.group(1))
    return None
