BEGIN;

CREATE SCHEMA IF NOT EXISTS erp;

CREATE TABLE erp.vendors (
    vendor_id VARCHAR(64) PRIMARY KEY,
    vendor_name VARCHAR(255) NOT NULL CHECK (btrim(vendor_name) <> ''),
    country VARCHAR(128),
    currency VARCHAR(3) CHECK (currency ~ '^[A-Z]{3}$'),
    extras JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE erp.sku_master (
    item_code VARCHAR(64) PRIMARY KEY,
    category VARCHAR(128),
    uom VARCHAR(32),
    gst_rate NUMERIC CHECK (gst_rate BETWEEN 0 AND 100),
    extras JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE erp.purchase_orders (
    po_number VARCHAR(64) PRIMARY KEY,
    vendor_id VARCHAR(64) NOT NULL REFERENCES erp.vendors(vendor_id),
    po_date DATE,
    status VARCHAR(32),
    extras JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE erp.po_line_items (
    po_number VARCHAR(64)
        REFERENCES erp.purchase_orders(po_number) ON DELETE CASCADE,
    line_number INTEGER CHECK (line_number > 0),
    item_code VARCHAR(64) NOT NULL REFERENCES erp.sku_master(item_code),
    description TEXT NOT NULL CHECK (btrim(description) <> ''),
    qty NUMERIC NOT NULL CHECK (
        qty > 0 AND qty NOT IN ('NaN'::numeric, 'Infinity'::numeric)
    ),
    unit_price NUMERIC NOT NULL CHECK (
        unit_price >= 0
        AND unit_price NOT IN ('NaN'::numeric, 'Infinity'::numeric)
    ),
    currency VARCHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    uom VARCHAR(32),
    tax_rate NUMERIC CHECK (tax_rate BETWEEN 0 AND 100),
    extras JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (po_number, line_number)
);

CREATE INDEX ON erp.purchase_orders(vendor_id);
CREATE INDEX ON erp.po_line_items(item_code);

INSERT INTO erp.vendors
SELECT
    v->>'vendor_id',
    v->>'vendor_name',
    v->>'country',
    v->>'currency',
    v - ARRAY['vendor_id', 'vendor_name', 'country', 'currency']
FROM jsonb_array_elements(
    pg_read_file('/erp-data/vendors.json')::jsonb
) AS source(v);

INSERT INTO erp.sku_master
SELECT
    s->>'item_code',
    s->>'category',
    s->>'uom',
    (s->>'gst_rate')::numeric,
    s - ARRAY['item_code', 'category', 'uom', 'gst_rate']
FROM jsonb_array_elements(
    pg_read_file('/erp-data/sku_master.json')::jsonb
) AS source(s);

INSERT INTO erp.purchase_orders
SELECT
    p->>'po_number',
    p->>'vendor_id',
    (p->>'po_date')::date,
    p->>'status',
    p - ARRAY['po_number', 'vendor_id', 'po_date', 'status', 'line_items']
FROM jsonb_array_elements(
    pg_read_file('/erp-data/PO Records.json')::jsonb
) AS source(p);

INSERT INTO erp.po_line_items
SELECT
    p->>'po_number',
    COALESCE((l->>'line_number')::integer, n::integer),
    l->>'item_code',
    l->>'description',
    COALESCE(l->>'qty', l->>'quantity')::numeric,
    (l->>'unit_price')::numeric,
    l->>'currency',
    l->>'uom',
    (l->>'tax_rate')::numeric,
    l - ARRAY[
        'line_number', 'item_code', 'description', 'qty', 'quantity',
        'unit_price', 'currency', 'uom', 'tax_rate'
    ]
FROM jsonb_array_elements(
    pg_read_file('/erp-data/PO Records.json')::jsonb
) AS source(p)
CROSS JOIN LATERAL jsonb_array_elements(
    p->'line_items'
) WITH ORDINALITY AS lines(l, n);

COMMIT;