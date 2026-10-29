
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.extraction import checksum_file, detect_format, extract
from app.extraction.parser import parse_invoice
from app.settings import Settings


def main(folder: str) -> int:
    settings = Settings(agent_db_password="unused-by-this-harness")
    directory = Path(folder).resolve()

    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in settings.supported_extensions:
            continue

        print("=" * 78)
        print(f"{path.name}   [{detect_format(path)}]  sha256={checksum_file(path)[:16]}...")
        try:
            extracted = extract(path, settings)
        except Exception as exc:
            print(f"  EXTRACTION FAILED: {type(exc).__name__}: {exc}")
            continue

        fields = parse_invoice(extracted.raw_text)
        print(f"  method={extracted.extraction_method} chars={len(extracted.raw_text)}")
        print(f"  invoice_number = {fields.invoice_number}")
        print(f"  invoice_date   = {fields.invoice_date}")
        print(f"  vendor_name    = {fields.vendor_name}")
        print(f"  vendor_id      = {fields.vendor_id}")
        print(f"  po_number      = {fields.po_number}")
        print(f"  currency       = {fields.currency}")
        print(f"  subtotal/tax/total = {fields.subtotal_amount} / {fields.tax_amount} / {fields.total_amount}")
        for item in fields.line_items:
            print(
                f"    [{item.line_number}] {item.item_code or '-':<10} "
                f"{(item.description or '')[:34]:<34} "
                f"qty={item.quantity} unit={item.unit_price} total={item.line_total}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/data/incoming"))
