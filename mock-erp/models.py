from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

class Vendor(BaseModel):
    vendor_id: str
    vendor_name: str
    country: str | None
    currency: str | None
    extras: dict[str, Any] = Field(default_factory=dict)

class SKU(BaseModel):
    item_code: str
    category: str | None
    uom: str | None
    gst_rate: Decimal | None
    extras: dict[str, Any] = Field(default_factory=dict)

class POLine(BaseModel):
    line_number: int
    item_code: str
    description: str
    qty: Decimal
    unit_price: Decimal
    currency: str
    line_total: Decimal
    uom: str | None
    tax_rate: Decimal | None
    extras: dict[str, Any] = Field(default_factory=dict)

class Subtotal(BaseModel):
    currency: str
    subtotal: Decimal


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    vendor_name: str
    po_date: date | None
    status: str | None
    currency: str | None
    currencies: list[str]
    line_items: list[POLine]
    subtotals: list[Subtotal]
    extras: dict[str, Any] = Field(default_factory=dict)