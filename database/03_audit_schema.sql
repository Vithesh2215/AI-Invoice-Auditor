-- =============================================================================
-- AI Invoice Auditor - audit schema (system of record for the audit trail)
--
-- Owner        : agent service (write), streamlit-ui (read)
-- Lifetime     : permanent. Never pruned, unlike langgraph checkpoints.
-- Loaded by    : postgres docker-entrypoint-initdb.d, first start only.
-- =============================================================================

BEGIN;

-- Serialized LangGraph state. Tables inside are created at runtime by the
-- LangGraph PostgreSQL checkpointer; the schema must exist before it connects.
CREATE SCHEMA IF NOT EXISTS langgraph;

CREATE SCHEMA IF NOT EXISTS audit;

-- -----------------------------------------------------------------------------
-- invoice_audit: one row per physical invoice file that was detected.
-- Rows are also written for files that fail to process, so that the trail
-- records every detection and not only the successes.
-- -----------------------------------------------------------------------------
CREATE TABLE audit.invoice_audit (
    invoice_id             UUID PRIMARY KEY,

    -- The binary never enters the database. We keep a reference plus the
    -- content checksum, which is what makes reprocessing detectable even
    -- when a vendor renames the file.
    file_name              VARCHAR(255)  NOT NULL,
    file_path              VARCHAR(512)  NOT NULL,
    file_checksum          VARCHAR(64)   NOT NULL UNIQUE,
    file_size_bytes        BIGINT,
    source_format          VARCHAR(16),            -- pdf | docx | png | jpg ...
    extraction_method      VARCHAR(32),            -- pdfplumber | docx | tesseract | pdf2image+tesseract

    -- Unstructured form: exactly what the extractor read, kept so that the
    -- vector index can be rebuilt without repeating OCR.
    raw_extracted_text     TEXT,
    translated_text        TEXT,
    source_language        VARCHAR(8),
    translation_confidence NUMERIC(4,3) CHECK (translation_confidence BETWEEN 0 AND 1),
    translation_engine     VARCHAR(64),

    -- Structured form: typed columns, because we filter, sort and sum on these.
    invoice_number         VARCHAR(64),
    vendor_name            VARCHAR(255),
    vendor_id              VARCHAR(64),
    po_number              VARCHAR(64),
    invoice_date           DATE,
    currency               CHAR(3),
    subtotal_amount        NUMERIC(14,2),
    tax_amount             NUMERIC(14,2),
    total_amount           NUMERIC(14,2),

    -- Outcome of the pipeline.
    processing_status      VARCHAR(32)  NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN ('pending','processing','completed','failed','skipped')),
    validation_status      VARCHAR(32)
        CHECK (validation_status IN ('passed','failed','partial')),
    recommendation         VARCHAR(32)
        CHECK (recommendation IN ('approve','review','reject')),
    confidence_score       NUMERIC(4,3) CHECK (confidence_score BETWEEN 0 AND 1),
    report_summary         TEXT,
    error_message          TEXT,

    -- Variable form: format specific leftovers that do not justify a column.
    extracted_raw          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Paths of the rendered report artefacts on the mounted volume.
    report_json_path       VARCHAR(512),
    report_html_path       VARCHAR(512),

    -- Links this business record back to its LangGraph working state.
    thread_id              VARCHAR(128),

    indexed_at             TIMESTAMPTZ,
    processed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_invoice_audit_vendor       ON audit.invoice_audit (vendor_id);
CREATE INDEX idx_invoice_audit_vendor_name  ON audit.invoice_audit (vendor_name);
CREATE INDEX idx_invoice_audit_po           ON audit.invoice_audit (po_number);
CREATE INDEX idx_invoice_audit_number       ON audit.invoice_audit (invoice_number);
CREATE INDEX idx_invoice_audit_recommend    ON audit.invoice_audit (recommendation);
CREATE INDEX idx_invoice_audit_status       ON audit.invoice_audit (processing_status);
CREATE INDEX idx_invoice_audit_date         ON audit.invoice_audit (invoice_date);
CREATE INDEX idx_invoice_audit_processed_at ON audit.invoice_audit (processed_at DESC);

-- -----------------------------------------------------------------------------
-- invoice_line_items: separate rows, never a JSON array on the invoice row.
-- Validation is a comparison of two relational structures (invoice lines
-- against PO lines), which is precisely what a join does.
-- -----------------------------------------------------------------------------
CREATE TABLE audit.invoice_line_items (
    id           BIGSERIAL PRIMARY KEY,
    invoice_id   UUID NOT NULL REFERENCES audit.invoice_audit(invoice_id) ON DELETE CASCADE,
    line_number  INTEGER NOT NULL CHECK (line_number > 0),
    item_code    VARCHAR(64),
    description  TEXT,
    quantity     NUMERIC(12,3),
    unit_price   NUMERIC(14,4),
    line_total   NUMERIC(14,2),
    currency     CHAR(3),
    tax_rate     NUMERIC(6,3),
    extras       JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (invoice_id, line_number)
);

CREATE INDEX idx_line_items_invoice   ON audit.invoice_line_items (invoice_id);
CREATE INDEX idx_line_items_item_code ON audit.invoice_line_items (item_code);

-- -----------------------------------------------------------------------------
-- discrepancies: one row per mismatched field, with both values and deviation.
-- -----------------------------------------------------------------------------
CREATE TABLE audit.discrepancies (
    id             BIGSERIAL PRIMARY KEY,
    invoice_id     UUID NOT NULL REFERENCES audit.invoice_audit(invoice_id) ON DELETE CASCADE,
    run_id         UUID,                      -- groups the findings of one validation pass
    source         VARCHAR(32) NOT NULL DEFAULT 'business'
        CHECK (source IN ('data','business')),
    line_number    INTEGER,                   -- NULL for header level findings
    field_name     VARCHAR(64) NOT NULL,
    invoice_value  TEXT,
    erp_value      TEXT,
    deviation_pct  NUMERIC(10,4),
    tolerance_pct  NUMERIC(10,4),
    severity       VARCHAR(16) NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('low','medium','high','critical')),
    message        TEXT,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_discrepancies_invoice  ON audit.discrepancies (invoice_id);
CREATE INDEX idx_discrepancies_field    ON audit.discrepancies (field_name);
CREATE INDEX idx_discrepancies_severity ON audit.discrepancies (severity);

-- -----------------------------------------------------------------------------
-- human_feedback: append only. A correction is an INSERT, never an UPDATE that
-- overwrites what the system originally extracted.
-- -----------------------------------------------------------------------------
CREATE TABLE audit.human_feedback (
    id              BIGSERIAL PRIMARY KEY,
    invoice_id      UUID NOT NULL REFERENCES audit.invoice_audit(invoice_id) ON DELETE CASCADE,
    line_number     INTEGER,
    field_name      VARCHAR(64) NOT NULL,
    original_value  TEXT,
    corrected_value TEXT,
    note            TEXT,
    corrected_by    VARCHAR(128) NOT NULL DEFAULT 'unknown',
    corrected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revalidated     BOOLEAN NOT NULL DEFAULT FALSE,
    revalidated_at  TIMESTAMPTZ
);

CREATE INDEX idx_feedback_invoice ON audit.human_feedback (invoice_id);

-- Enforce append-only semantics at the database level rather than by convention.
CREATE OR REPLACE FUNCTION audit.reject_feedback_overwrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (OLD.field_name, OLD.original_value, OLD.corrected_value, OLD.corrected_by)
       IS DISTINCT FROM
       (NEW.field_name, NEW.original_value, NEW.corrected_value, NEW.corrected_by)
    THEN
        RAISE EXCEPTION
            'audit.human_feedback is append-only; insert a new correction instead';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_human_feedback_append_only
    BEFORE UPDATE ON audit.human_feedback
    FOR EACH ROW EXECUTE FUNCTION audit.reject_feedback_overwrite();

-- -----------------------------------------------------------------------------
-- rag_queries: the reflection loop's own audit trail. Lets the dashboard show
-- which answers were accepted, and after how many retrieval attempts.
-- -----------------------------------------------------------------------------
CREATE TABLE audit.rag_queries (
    id                     BIGSERIAL PRIMARY KEY,
    question               TEXT NOT NULL,
    answer                 TEXT,
    route                  VARCHAR(16) NOT NULL DEFAULT 'vector'
        CHECK (route IN ('vector','sql')),
    invoice_id             UUID REFERENCES audit.invoice_audit(invoice_id) ON DELETE SET NULL,
    attempts               INTEGER NOT NULL DEFAULT 1,
    answer_relevance       NUMERIC(4,3),
    groundedness           NUMERIC(4,3),
    context_relevance      NUMERIC(4,3),
    overall_score          NUMERIC(4,3),
    accepted               BOOLEAN NOT NULL DEFAULT FALSE,
    retrieved_chunk_ids    JSONB NOT NULL DEFAULT '[]'::jsonb,
    cache_hit              BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms             INTEGER,
    asked_by               VARCHAR(128),
    asked_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_rag_queries_asked_at ON audit.rag_queries (asked_at DESC);

-- -----------------------------------------------------------------------------
-- processing_events: every detection, including files that never parsed.
-- -----------------------------------------------------------------------------
CREATE TABLE audit.processing_events (
    id          BIGSERIAL PRIMARY KEY,
    invoice_id  UUID REFERENCES audit.invoice_audit(invoice_id) ON DELETE CASCADE,
    file_name   VARCHAR(255),
    checksum    VARCHAR(64),
    stage       VARCHAR(32) NOT NULL,
    status      VARCHAR(16) NOT NULL CHECK (status IN ('started','ok','skipped','error')),
    detail      TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_events_invoice    ON audit.processing_events (invoice_id);
CREATE INDEX idx_events_created_at ON audit.processing_events (created_at DESC);

-- -----------------------------------------------------------------------------
-- Convenience view for the dashboard: header plus rolled-up counts.
-- -----------------------------------------------------------------------------
CREATE VIEW audit.invoice_overview AS
SELECT
    i.invoice_id,
    i.file_name,
    i.invoice_number,
    i.vendor_id,
    i.vendor_name,
    i.po_number,
    i.invoice_date,
    i.currency,
    i.total_amount,
    i.source_language,
    i.translation_confidence,
    i.extraction_method,
    i.processing_status,
    i.validation_status,
    i.recommendation,
    i.confidence_score,
    i.indexed_at,
    i.processed_at,
    i.error_message,
    COALESCE(d.total, 0)    AS discrepancy_count,
    COALESCE(d.critical, 0) AS critical_count,
    COALESCE(l.total, 0)    AS line_item_count,
    COALESCE(f.total, 0)    AS feedback_count
FROM audit.invoice_audit i
LEFT JOIN (
    SELECT invoice_id,
           count(*) AS total,
           count(*) FILTER (WHERE severity IN ('high','critical')) AS critical
    FROM audit.discrepancies GROUP BY invoice_id
) d ON d.invoice_id = i.invoice_id
LEFT JOIN (
    SELECT invoice_id, count(*) AS total
    FROM audit.invoice_line_items GROUP BY invoice_id
) l ON l.invoice_id = i.invoice_id
LEFT JOIN (
    SELECT invoice_id, count(*) AS total
    FROM audit.human_feedback GROUP BY invoice_id
) f ON f.invoice_id = i.invoice_id;

COMMIT;
