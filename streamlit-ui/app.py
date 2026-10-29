
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

AGENT_API_URL = os.getenv("AGENT_API_URL", "http://agent:8000").rstrip("/")
AGENT_PUBLIC_URL = os.getenv("AGENT_PUBLIC_URL", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("AGENT_API_TIMEOUT", "180"))

RECOMMENDATION_COLOURS = {
    "approve": "#17683a",
    "review": "#8a5a06",
    "reject": "#9b1c1c",
}
SEVERITY_ORDER = ["critical", "high", "medium", "low"]

HEADER_FIELDS = [
    "invoice_number",
    "vendor_name",
    "vendor_id",
    "po_number",
    "invoice_date",
    "currency",
    "subtotal_amount",
    "tax_amount",
    "total_amount",
]
LINE_FIELDS = ["item_code", "description", "quantity", "unit_price", "line_total"]




@st.cache_resource(show_spinner=False)
def get_pool() -> ConnectionPool:
    password = os.getenv("UI_DB_PASSWORD")
    if not password:
        raise RuntimeError("UI_DB_PASSWORD is required")

    pool = ConnectionPool(
        open=False,
        min_size=1,
        max_size=4,
        timeout=10,
        kwargs={
            "host": os.getenv("UI_DB_HOST", "postgres"),
            "port": int(os.getenv("UI_DB_PORT", "5432")),
            "dbname": os.getenv("UI_DB_NAME", "invoice_auditor"),
            "user": os.getenv("UI_DB_USER", "invoice_ui"),
            "password": password,
            "connect_timeout": 5,
            "options": "-c search_path=audit,public",
            "autocommit": True,
            "row_factory": dict_row,
        },
    )
    pool.open(wait=True, timeout=30)
    return pool


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def api_get(path: str, **params: Any) -> Any:
    response = httpx.get(f"{AGENT_API_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def api_post(path: str, payload: Any = None, **params: Any) -> Any:
    response = httpx.post(
        f"{AGENT_API_URL}{path}", json=payload, params=params, timeout=REQUEST_TIMEOUT
    )
    if response.status_code >= 400:
        raise RuntimeError(_error_detail(response))
    return response.json()


def _error_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))[:500]
    except Exception:
        return response.text[:500]


def frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{k: _plain(v) for k, v in row.items()} for row in rows])


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def money(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f} {currency or ''}".strip()


def md_text(value: str | None) -> str:
    """Escape '$' in free-form text so Streamlit's Markdown renderer does not
    mistake dollar amounts (e.g. "$275.00 ... $25.00") for LaTeX math delimiters.
    """
    if not value:
        return ""
    return value.replace("$", "\\$")


def pill(recommendation: str | None) -> str:
    label = (recommendation or "not assessed").upper()
    colour = RECOMMENDATION_COLOURS.get(recommendation or "", "#6b7280")
    return (
        f"<span style='background:{colour}1a;color:{colour};padding:4px 12px;"
        f"border-radius:999px;font-weight:600;font-size:12px;"
        f"letter-spacing:.05em'>{label}</span>"
    )




def page_overview() -> None:
    st.subheader("Portfolio")

    totals = query(
        """
        SELECT count(*) AS invoices,
               count(*) FILTER (WHERE processing_status = 'completed') AS completed,
               count(*) FILTER (WHERE processing_status = 'failed')    AS failed,
               count(*) FILTER (WHERE recommendation = 'approve')      AS approved,
               count(*) FILTER (WHERE recommendation = 'review')       AS in_review,
               count(*) FILTER (WHERE recommendation = 'reject')       AS rejected,
               count(*) FILTER (WHERE indexed_at IS NOT NULL)          AS indexed,
               COALESCE(sum(total_amount), 0)                          AS total_value,
               (SELECT count(*) FROM audit.discrepancies)              AS discrepancies,
               (SELECT count(*) FROM audit.human_feedback)             AS corrections
        FROM audit.invoice_audit
        """
    )[0]

    top = st.columns(5)
    top[0].metric("Invoices", totals["invoices"])
    top[1].metric("Approved", totals["approved"])
    top[2].metric("In review", totals["in_review"])
    top[3].metric("Rejected", totals["rejected"])
    top[4].metric("Failed", totals["failed"])

    bottom = st.columns(4)
    bottom[0].metric("Total value", f"{float(totals['total_value']):,.2f}")
    bottom[1].metric("Discrepancies", totals["discrepancies"])
    bottom[2].metric("Human corrections", totals["corrections"])
    bottom[3].metric("Indexed for search", totals["indexed"])

    left, right = st.columns(2)

    with left:
        st.markdown("##### By vendor")
        vendors = frame(
            query(
                """
                SELECT COALESCE(vendor_name, 'unknown') AS vendor,
                       count(*) AS invoices,
                       COALESCE(sum(total_amount), 0) AS value,
                       count(*) FILTER (WHERE recommendation = 'reject') AS rejected
                FROM audit.invoice_audit
                GROUP BY 1 ORDER BY invoices DESC LIMIT 15
                """
            )
        )
        if vendors.empty:
            st.caption("Nothing audited yet.")
        else:
            st.dataframe(vendors, use_container_width=True, hide_index=True)

    with right:
        st.markdown("##### Most flagged fields")
        fields = frame(
            query(
                "SELECT field_name, severity, count(*) AS occurrences "
                "FROM audit.discrepancies GROUP BY 1, 2 "
                "ORDER BY occurrences DESC LIMIT 15"
            )
        )
        if fields.empty:
            st.caption("No discrepancies recorded yet.")
        else:
            st.dataframe(fields, use_container_width=True, hide_index=True)

    st.markdown("##### Recent processing events")
    events = frame(
        query(
            "SELECT created_at, file_name, stage, status, duration_ms, detail "
            "FROM audit.processing_events ORDER BY created_at DESC LIMIT 40"
        )
    )
    if events.empty:
        st.caption("No events yet.")
    else:
        st.dataframe(events, use_container_width=True, hide_index=True)


def page_invoices() -> None:
    st.subheader("Audited invoices")

    filters = st.columns([2, 2, 2, 4])
    recommendation = filters[0].selectbox(
        "Recommendation", ["all", "approve", "review", "reject"]
    )
    status = filters[1].selectbox(
        "Status", ["all", "completed", "failed", "processing", "pending", "skipped"]
    )
    limit = filters[2].number_input("Rows", min_value=10, max_value=500, value=100, step=10)
    search = filters[3].text_input("Search invoice / vendor / PO / file name")

    clauses, params = [], []
    if recommendation != "all":
        clauses.append("recommendation = %s")
        params.append(recommendation)
    if status != "all":
        clauses.append("processing_status = %s")
        params.append(status)
    if search.strip():
        clauses.append(
            "(invoice_number ILIKE %s OR vendor_name ILIKE %s "
            "OR po_number ILIKE %s OR file_name ILIKE %s)"
        )
        pattern = (
            "%"
            + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        params.extend([pattern] * 4)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))

    rows = query(
        f"""
        SELECT invoice_id, file_name, invoice_number, vendor_name, po_number,
               invoice_date, currency, total_amount, source_language,
               translation_confidence, processing_status, validation_status,
               recommendation, confidence_score, discrepancy_count,
               critical_count, line_item_count, feedback_count, processed_at
        FROM audit.invoice_overview {where}
        ORDER BY processed_at DESC LIMIT %s
        """,
        tuple(params),
    )

    if not rows:
        st.info("No invoices match these filters. Drop a file into data/incoming or use the Ingest page.")
        return

    table = frame(rows).drop(columns=["invoice_id"])
    st.dataframe(table, use_container_width=True, hide_index=True)

    labels = {
        f"{r['invoice_number'] or r['file_name']} · {r['vendor_name'] or 'unknown vendor'} "
        f"· {r['recommendation'] or 'not assessed'}": str(r["invoice_id"])
        for r in rows
    }
    choice = st.selectbox("Open an invoice", list(labels))
    if choice:
        st.session_state["invoice_id"] = labels[choice]
        page_invoice_detail(labels[choice])


def page_invoice_detail(invoice_id: str) -> None:
    try:
        detail = api_get(f"/invoices/{invoice_id}")
    except Exception as exc:
        st.error(f"Could not load the invoice from the agent API: {exc}")
        return

    invoice = detail["invoice"]

    st.divider()
    header = st.columns([3, 1])
    header[0].markdown(
        f"### {invoice.get('invoice_number') or invoice.get('file_name')}"
    )
    header[1].markdown(pill(invoice.get("recommendation")), unsafe_allow_html=True)

    if invoice.get("report_summary"):
        st.info(md_text(invoice["report_summary"]))
    if invoice.get("error_message"):
        st.error(md_text(invoice["error_message"]))

    cells = st.columns(4)
    cells[0].metric("Vendor", invoice.get("vendor_name") or "—")
    cells[1].metric("Purchase order", invoice.get("po_number") or "—")
    cells[2].metric("Total", money(invoice.get("total_amount"), invoice.get("currency")))
    cells[3].metric(
        "Confidence",
        f"{float(invoice['confidence_score']):.2f}" if invoice.get("confidence_score") is not None else "—",
    )

    cells = st.columns(4)
    cells[0].metric("Language", invoice.get("source_language") or "—")
    cells[1].metric(
        "Translation",
        f"{float(invoice['translation_confidence']):.2f}" if invoice.get("translation_confidence") is not None else "—",
    )
    cells[2].metric("Extraction", invoice.get("extraction_method") or "—")
    cells[3].metric("Validation", invoice.get("validation_status") or "—")

    correctable = (invoice.get("recommendation") or "").lower() in {"review", "reject"}

    labels = ["Discrepancies", "Line items"]
    if correctable:
        labels.append("Correct a field")
    labels += ["Report", "Extracted text", "History"]
    tabs = dict(zip(labels, st.tabs(labels)))

    with tabs["Discrepancies"]:
        discrepancies = detail["discrepancies"]
        if not discrepancies:
            st.success("No discrepancies were found against the ERP purchase order.")
        else:
            table = frame(discrepancies)
            table["severity"] = pd.Categorical(
                table["severity"], categories=SEVERITY_ORDER, ordered=True
            )
            st.dataframe(
                table.sort_values("severity"), use_container_width=True, hide_index=True
            )

    with tabs["Line items"]:
        items = detail["line_items"]
        if not items:
            st.warning("No line items could be read from this document.")
        else:
            st.dataframe(frame(items), use_container_width=True, hide_index=True)

    if correctable:
        with tabs["Correct a field"]:
            render_correction_form(invoice_id, detail)

    with tabs["Report"]:
        if invoice.get("report_html_path"):
            st.link_button(
                "Open the full HTML report",
                f"{AGENT_PUBLIC_URL}/invoices/{invoice_id}/report.html",
            )
        try:
            st.json(api_get(f"/invoices/{invoice_id}/report"), expanded=False)
        except Exception:
            st.caption("No report has been generated for this invoice yet.")

    with tabs["Extracted text"]:
        st.text_area(
            "Raw extracted text",
            detail.get("raw_extracted_text") or "",
            height=320,
            disabled=True,
        )
        if invoice.get("translated_text"):
            st.text_area(
                "English translation", invoice["translated_text"], height=320, disabled=True
            )

    with tabs["History"]:
        feedback = detail["feedback"]
        st.markdown("##### Corrections (append-only)")
        if feedback:
            st.dataframe(frame(feedback), use_container_width=True, hide_index=True)
        else:
            st.caption("No corrections have been recorded.")

        st.markdown("##### Processing events")
        st.dataframe(frame(detail["events"]), use_container_width=True, hide_index=True)


def render_correction_form(invoice_id: str, detail: dict[str, Any]) -> None:
    st.caption(
        "A correction is appended to audit.human_feedback. What the system "
        "originally extracted is never overwritten; re-validation overlays the "
        "correction and runs the workflow again."
    )

    scope = st.radio("Scope", ["Header field", "Line item field"], horizontal=True)

    if scope == "Header field":
        field_name = st.selectbox("Field", HEADER_FIELDS)
        line_number = None
        current = detail["invoice"].get(field_name)
    else:
        items = detail["line_items"]
        if not items:
            st.warning("There are no line items to correct.")
            return
        line_number = st.selectbox(
            "Line", [item["line_number"] for item in items]
        )
        field_name = st.selectbox("Field", LINE_FIELDS)
        current = next(
            (i.get(field_name) for i in items if i["line_number"] == line_number), None
        )

    st.text_input("Currently extracted", value="" if current is None else str(current), disabled=True)
    corrected = st.text_input("Corrected value")
    note = st.text_input("Note (optional)")
    corrected_by = st.text_input("Your name", value="auditor")
    revalidate = st.checkbox("Re-run validation after saving", value=True)

    if st.button("Save correction", type="primary"):
        if not corrected.strip():
            st.error("Enter the corrected value.")
            return
        payload = {
            "field_name": field_name,
            "corrected_value": corrected.strip(),
            "line_number": line_number,
            "note": note.strip() or None,
            "corrected_by": corrected_by.strip() or "auditor",
            "revalidate": revalidate,
        }
        try:
            with st.spinner("Saving and re-validating..."):
                result = api_post(f"/invoices/{invoice_id}/feedback", payload)
        except Exception as exc:
            st.error(str(exc))
            return
        st.success("Correction recorded.")
        st.json(result)


def page_ingest() -> None:
    st.subheader("Ingest invoices")

    st.markdown("##### Upload")
    uploaded = st.file_uploader(
        "PDF, DOCX or a scanned image",
        type=["pdf", "docx", "png", "jpg", "jpeg", "tif", "tiff"],
    )
    process_now = st.checkbox("Audit immediately", value=True)

    if uploaded is not None and st.button("Send to the agent", type="primary"):
        try:
            with st.spinner("Uploading and auditing..."):
                response = httpx.post(
                    f"{AGENT_API_URL}/invoices/upload",
                    files={"file": (uploaded.name, uploaded.getvalue())},
                    params={"process_now": process_now},
                    timeout=REQUEST_TIMEOUT,
                )
            if response.status_code >= 400:
                st.error(_error_detail(response))
            else:
                st.success("Accepted.")
                st.json(response.json())
        except Exception as exc:
            st.error(f"Upload failed: {exc}")

    st.divider()
    st.markdown("##### Scan the watched folder")
    st.caption(
        "The agent polls data/incoming on its own. This triggers a pass right "
        "now, which is what you want after copying files in from the host."
    )
    columns = st.columns([1, 1, 2])
    force = columns[0].checkbox("Reprocess already-audited files")
    limit = columns[1].number_input("Limit", min_value=1, max_value=200, value=20)

    if columns[2].button("Scan now"):
        try:
            with st.spinner("Scanning..."):
                result = api_post("/scan", {"force": force, "limit": int(limit)})
        except Exception as exc:
            st.error(str(exc))
            return
        st.success(
            f"Scanned {result['scanned']}, processed {result['processed']}, "
            f"skipped {result['skipped']}, failed {result['failed']}."
        )
        if result["results"]:
            st.dataframe(frame(result["results"]), use_container_width=True, hide_index=True)


def page_ask() -> None:
    st.subheader("Ask about the audited invoices")
    st.caption(
        "Questions go through retrieval, generation and a RAG Triad "
        "reflection loop."
    )

    question = st.text_input(
        "Question",
        placeholder="Why was the invoice from HafenLogistik flagged?",
    )

    if st.button("Ask", type="primary") and question.strip():
        payload = {
            "question": question.strip(),
            "asked_by": "dashboard",
        }
        try:
            with st.spinner("Retrieving and reasoning..."):
                answer = api_post("/rag/query", payload)
        except Exception as exc:
            st.error(str(exc))
            return

        st.markdown("#### Answer")
        st.write(md_text(answer["answer"]))

        meta = st.columns(5)
        meta[0].metric("Route", answer["route"])
        meta[1].metric("Attempts", answer["attempts"])
        meta[2].metric(
            "Triad score",
            f"{answer['overall_score']:.2f}" if answer.get("overall_score") is not None else "—",
        )
        meta[3].metric("Accepted", "yes" if answer["accepted"] else "no")
        meta[4].metric("Latency", f"{answer['latency_ms']} ms")

        if answer.get("note"):
            st.caption(answer["note"])

        if answer.get("scores"):
            st.markdown("##### RAG Triad")
            st.json(answer["scores"])

        if answer.get("citations"):
            st.markdown("##### Citations")
            for index, citation in enumerate(answer["citations"], start=1):
                with st.expander(
                    f"[{index}] {citation.get('source_type')} · "
                    f"{citation.get('invoice_number') or citation.get('invoice_id')} · "
                    f"score {citation.get('score')}"
                ):
                    st.text(citation.get("chunk_text", ""))

    st.divider()
    st.markdown("##### Recent questions")
    history = frame(
        query(
            "SELECT asked_at, question, route, attempts, overall_score, accepted, "
            "cache_hit, latency_ms FROM audit.rag_queries "
            "ORDER BY asked_at DESC LIMIT 25"
        )
    )
    if history.empty:
        st.caption("Nothing has been asked yet.")
    else:
        st.dataframe(history, use_container_width=True, hide_index=True)


def page_system() -> None:
    st.subheader("System")

    try:
        health = api_get("/health")
    except httpx.HTTPStatusError as exc:
        health = exc.response.json()
    except Exception as exc:
        st.error(f"The agent API is unreachable at {AGENT_API_URL}: {exc}")
        return

    st.markdown(f"**Status:** `{health['status']}`  ·  **Version:** `{health['version']}`")
    for dependency in health["dependencies"]:
        icon = "✅" if dependency["healthy"] else ("⚠️" if not dependency["required"] else "❌")
        st.markdown(
            f"{icon} **{dependency['name']}** — {dependency['detail']} "
            f"({dependency['latency_ms']} ms)"
        )

    st.divider()
    try:
        st.json(api_get("/stats"))
    except Exception as exc:
        st.warning(f"Could not read /stats: {exc}")

    st.divider()
    st.markdown("##### Rebuild the search index")
    st.caption(
        "Qdrant is derived from PostgreSQL. Rebuilding re-chunks and re-embeds "
        "text that is already persisted: no document is reopened and no OCR is "
        "repeated. This is the recovery path after the Qdrant volume is lost."
    )
    if st.button("Rebuild now"):
        try:
            with st.spinner("Rebuilding..."):
                st.json(api_post("/admin/reindex"))
        except Exception as exc:
            st.error(str(exc))

    with st.expander("Rules in force"):
        try:
            st.json(api_get("/config/rules"))
        except Exception as exc:
            st.warning(str(exc))




def main() -> None:
    st.set_page_config(
        page_title="AI Invoice Auditor",
        page_icon="📄",
        layout="wide",
    )
    st.title("AI Invoice Auditor")
    st.caption(
        "PostgreSQL holds the authoritative audit trail. This dashboard reads it "
        "over a read-only role and routes every write through the agent API."
    )

    pages = {
        "Overview": page_overview,
        "Invoices": page_invoices,
        "Ingest": page_ingest,
        "Ask": page_ask,
        "System": page_system,
    }

    with st.sidebar:
        st.markdown("### Navigation")
        selection = st.radio("Page", list(pages), label_visibility="collapsed")
        st.divider()
        st.caption(f"Agent API\n\n`{AGENT_API_URL}`")
        if st.button("Refresh data"):
            st.cache_data.clear()
            st.rerun()

    try:
        pages[selection]()
    except psycopg.Error as exc:
        st.error(
            "Could not read the audit database. Check UI_DB_* in .env.ui and that "
            f"the postgres service is healthy.\n\n`{type(exc).__name__}: {exc}`"
        )
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")


main()
