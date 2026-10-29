
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

logger = logging.getLogger(__name__)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice audit - {{ invoice.invoice_number or invoice.file_name }}</title>
<style>
  :root { --ink:#1a1d24; --muted:#6b7280; --line:#e5e7eb; --bg:#f7f8fa; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         margin:0; padding:32px; background:var(--bg); color:var(--ink); line-height:1.5; }
  .sheet { max-width:980px; margin:0 auto; background:#fff; border:1px solid var(--line);
           border-radius:12px; padding:32px; }
  h1 { font-size:22px; margin:0 0 4px; }
  h2 { font-size:15px; text-transform:uppercase; letter-spacing:.06em;
       color:var(--muted); margin:32px 0 12px; }
  .sub { color:var(--muted); font-size:14px; margin-bottom:24px; }
  .pill { display:inline-block; padding:5px 14px; border-radius:999px; font-weight:600;
          font-size:13px; letter-spacing:.03em; text-transform:uppercase; }
  .approve { background:#e7f6ec; color:#17683a; }
  .review  { background:#fdf3e2; color:#8a5a06; }
  .reject  { background:#fdeaea; color:#9b1c1c; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; }
  .cell { border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
  .cell .k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
  .cell .v { font-size:16px; font-weight:600; margin-top:4px; word-break:break-word; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line);
           vertical-align:top; }
  th { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .sev { font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:.05em; }
  .sev-critical { color:#9b1c1c; } .sev-high { color:#b45309; }
  .sev-medium { color:#8a5a06; }  .sev-low { color:var(--muted); }
  .summary { background:#f3f6fb; border-left:3px solid #4f7bd1; padding:14px 18px;
             border-radius:0 8px 8px 0; }
  ul { margin:8px 0 0; padding-left:20px; }
  .empty { color:var(--muted); font-style:italic; }
  footer { margin-top:36px; padding-top:14px; border-top:1px solid var(--line);
           color:var(--muted); font-size:12px; }
  pre { white-space:pre-wrap; font-size:12px; background:#fafbfc; border:1px solid var(--line);
        border-radius:8px; padding:14px; max-height:380px; overflow:auto; }
</style>
</head>
<body>
<div class="sheet">
  <h1>Invoice audit report</h1>
  <div class="sub">
    {{ invoice.file_name }} &middot; generated {{ generated_at }}
  </div>

  <span class="pill {{ invoice.recommendation or 'review' }}">
    {{ (invoice.recommendation or 'review') }}
  </span>

  {% if summary %}
  <h2>Auditor summary</h2>
  <div class="summary">
    <div>{{ summary }}</div>
    {% if key_findings %}
    <ul>{% for finding in key_findings %}<li>{{ finding }}</li>{% endfor %}</ul>
    {% endif %}
    {% if suggested_action %}
    <div style="margin-top:10px"><strong>Next step:</strong> {{ suggested_action }}</div>
    {% endif %}
  </div>
  {% endif %}

  <h2>Invoice</h2>
  <div class="grid">
    <div class="cell"><div class="k">Invoice number</div><div class="v">{{ invoice.invoice_number or '—' }}</div></div>
    <div class="cell"><div class="k">Vendor</div><div class="v">{{ invoice.vendor_name or '—' }}</div></div>
    <div class="cell"><div class="k">Vendor ID</div><div class="v">{{ invoice.vendor_id or '—' }}</div></div>
    <div class="cell"><div class="k">Purchase order</div><div class="v">{{ invoice.po_number or '—' }}</div></div>
    <div class="cell"><div class="k">Invoice date</div><div class="v">{{ invoice.invoice_date or '—' }}</div></div>
    <div class="cell"><div class="k">Currency</div><div class="v">{{ invoice.currency or '—' }}</div></div>
    <div class="cell"><div class="k">Total</div><div class="v">{{ invoice.total_amount if invoice.total_amount is not none else '—' }}</div></div>
    <div class="cell"><div class="k">Validation</div><div class="v">{{ invoice.validation_status or '—' }}</div></div>
  </div>

  <h2>Extraction and translation</h2>
  <div class="grid">
    <div class="cell"><div class="k">Method</div><div class="v">{{ invoice.extraction_method or '—' }}</div></div>
    <div class="cell"><div class="k">Source language</div><div class="v">{{ invoice.source_language or '—' }}</div></div>
    <div class="cell"><div class="k">Translation confidence</div>
      <div class="v">{{ invoice.translation_confidence if invoice.translation_confidence is not none else '—' }}</div></div>
    <div class="cell"><div class="k">Engine</div><div class="v">{{ invoice.translation_engine or '—' }}</div></div>
  </div>

  <h2>Line items</h2>
  {% if line_items %}
  <table>
    <tr><th>#</th><th>Item</th><th>Description</th>
        <th class="num">Qty</th><th class="num">Unit price</th><th class="num">Line total</th></tr>
    {% for item in line_items %}
    <tr>
      <td>{{ item.line_number }}</td>
      <td>{{ item.item_code or '—' }}</td>
      <td>{{ item.description or '—' }}</td>
      <td class="num">{{ item.quantity if item.quantity is not none else '—' }}</td>
      <td class="num">{{ item.unit_price if item.unit_price is not none else '—' }}</td>
      <td class="num">{{ item.line_total if item.line_total is not none else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p class="empty">No line items were extracted.</p>{% endif %}

  <h2>Discrepancies ({{ discrepancies | length }})</h2>
  {% if discrepancies %}
  <table>
    <tr><th>Severity</th><th>Field</th><th>Line</th><th>Invoice</th><th>ERP</th>
        <th class="num">Deviation</th><th>Detail</th></tr>
    {% for d in discrepancies %}
    <tr>
      <td class="sev sev-{{ d.severity }}">{{ d.severity }}</td>
      <td>{{ d.field_name }}</td>
      <td>{{ d.line_number if d.line_number is not none else '—' }}</td>
      <td>{{ d.invoice_value or '—' }}</td>
      <td>{{ d.erp_value or '—' }}</td>
      <td class="num">{% if d.deviation_pct is not none %}{{ '%.2f'|format(d.deviation_pct|float) }}%{% else %}—{% endif %}</td>
      <td>{{ d.message or '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p class="empty">No discrepancies were found.</p>{% endif %}

  {% if missing_fields %}
  <h2>Missing fields</h2>
  <ul>{% for field in missing_fields %}<li>{{ field }}</li>{% endfor %}</ul>
  {% endif %}

  {% if purchase_order %}
  <h2>Purchase order on file (via mock-erp)</h2>
  <table>
    <tr><th>#</th><th>Item</th><th>Description</th>
        <th class="num">Qty</th><th class="num">Unit price</th><th>Currency</th></tr>
    {% for line in purchase_order.get('line_items', []) %}
    <tr>
      <td>{{ line.line_number }}</td><td>{{ line.item_code }}</td>
      <td>{{ line.description }}</td><td class="num">{{ line.qty }}</td>
      <td class="num">{{ line.unit_price }}</td><td>{{ line.currency }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  <h2>Extracted text</h2>
  <pre>{{ raw_text }}</pre>

  <footer>
    invoice_id {{ invoice.invoice_id }} &middot; thread {{ invoice.thread_id or '—' }}<br>
    PostgreSQL holds the authoritative record; this file is a rendering of it.
  </footer>
</div>
</body>
</html>
"""


class ReportWriter:
    def __init__(self, reports_dir: Path) -> None:
        self._dir = Path(reports_dir)
        self._env = Environment(autoescape=select_autoescape(["html"]))
        self._template = self._env.from_string(_HTML_TEMPLATE)

    def ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def build_payload(
        self,
        *,
        invoice: dict[str, Any],
        line_items: list[dict[str, Any]],
        discrepancies: list[dict[str, Any]],
        purchase_order: dict[str, Any] | None,
        data_checks: dict[str, Any],
        business_comparison: dict[str, Any],
        narrative: dict[str, Any],
        raw_text: str,
    ) -> dict[str, Any]:
        severity_counts: dict[str, int] = {}
        for item in discrepancies:
            key = str(item.get("severity", "medium"))
            severity_counts[key] = severity_counts.get(key, 0) + 1

        return {
            "report_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "invoice": invoice,
            "line_items": line_items,
            "discrepancy_summary": {
                "total": len(discrepancies),
                "by_severity": severity_counts,
                "fields": sorted({str(d.get("field_name")) for d in discrepancies}),
            },
            "discrepancies": discrepancies,
            "missing_fields": data_checks.get("missing_header_fields", []),
            "translation": {
                "source_language": invoice.get("source_language"),
                "confidence": invoice.get("translation_confidence"),
                "engine": invoice.get("translation_engine"),
            },
            "data_checks": data_checks,
            "business_comparison": business_comparison,
            "purchase_order": purchase_order,
            "recommendation": invoice.get("recommendation"),
            "validation_status": invoice.get("validation_status"),
            "narrative": narrative,
            "raw_extracted_text_chars": len(raw_text or ""),
        }

    def write(self, payload: dict[str, Any], *, raw_text: str, stem: str) -> tuple[str, str]:
        self.ensure_dir()
        safe_stem = _safe_stem(stem)

        json_path = self._dir / f"{safe_stem}.json"
        json_path.write_text(
            json.dumps(payload, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )

        html_path = self._dir / f"{safe_stem}.html"
        html_path.write_text(
            self._template.render(
                invoice=payload["invoice"],
                line_items=payload["line_items"],
                discrepancies=payload["discrepancies"],
                missing_fields=payload["missing_fields"],
                purchase_order=payload["purchase_order"],
                summary=payload["narrative"].get("summary"),
                key_findings=payload["narrative"].get("key_findings") or [],
                suggested_action=payload["narrative"].get("suggested_action"),
                raw_text=(raw_text or "")[:20000],
                generated_at=payload["generated_at"],
            ),
            encoding="utf-8",
        )

        logger.info("report written", extra={"json": str(json_path), "html": str(html_path)})
        return str(json_path), str(html_path)


def _safe_stem(stem: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)
    return cleaned[:120] or "report"
