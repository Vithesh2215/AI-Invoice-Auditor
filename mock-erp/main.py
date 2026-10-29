import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from psycopg import Connection, Error
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from db import create_pool, get_connection, validate_reader
from models import SKU, PurchaseOrder, Vendor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("erp")

DB = Annotated[Connection, Depends(get_connection)]
ID = Annotated[str, Path(min_length=1, max_length=64)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = create_pool()

    try:
        pool.open(wait=True, timeout=10)

        with pool.connection() as connection:
            validate_reader(connection)
            connection.execute(
                "SELECT p.po_number, p.po_date, p.status, p.extras, "
                "v.vendor_id, v.vendor_name, v.country, v.currency, v.extras, "
                "l.line_number, l.item_code, l.description, l.qty, l.unit_price, "
                "l.currency, l.uom, l.tax_rate, l.extras, s.item_code, "
                "s.category, s.uom, s.gst_rate, s.extras "
                "FROM erp.purchase_orders p "
                "JOIN erp.vendors v ON v.vendor_id = p.vendor_id "
                "JOIN erp.po_line_items l ON l.po_number = p.po_number "
                "JOIN erp.sku_master s ON s.item_code = l.item_code LIMIT 0"
            )
    except Exception as exc:
        pool.close()
        logger.error("ERP startup failed: %s", type(exc).__name__)
        raise RuntimeError(
            "Check ERP database connection, schema, and reader permissions"
        ) from None

    app.state.pool = pool

    try:
        yield
    finally:
        pool.close()

app = FastAPI(
    title="Invoice Auditor ERP",
    version="1.0.0",
    lifespan=lifespan,
)


async def database_error(request, exc):
    logger.error("ERP database failure: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "ERP database is unavailable"},
    )

for exception in (Error, PoolTimeout, PoolClosed, TooManyRequests):
    app.add_exception_handler(exception, database_error)


@app.get("/health")
def health(connection: DB):
    connection.execute(
        "SELECT (SELECT count(*) FROM erp.vendors), "
        "(SELECT count(*) FROM erp.sku_master), "
        "(SELECT count(*) FROM erp.purchase_orders), "
        "(SELECT count(*) FROM erp.po_line_items)"
    ).fetchone()

    return {"status": "healthy"}

@app.get("/vendor/{vendor_id}", response_model=Vendor)
def get_vendor(vendor_id: ID, connection: DB):
    record = connection.execute(
        "SELECT vendor_id, vendor_name, country, currency, extras "
        "FROM erp.vendors WHERE vendor_id = %s",
        (vendor_id,),
    ).fetchone()

    if record is None:
        raise HTTPException(404, "Vendor not found")

    return record


@app.get("/vendors", response_model=list[Vendor])
def find_vendors(
    connection: DB,
    name: Annotated[str, Query(min_length=1, max_length=255)],
):
    pattern = (
        "%"
        + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
    )
    return connection.execute(
        "SELECT vendor_id, vendor_name, country, currency, extras "
        "FROM erp.vendors WHERE vendor_name ILIKE %s "
        "ORDER BY vendor_id LIMIT 100",
        (pattern,),
    ).fetchall()

@app.get("/sku/{sku_code}", response_model=SKU)
def get_sku(sku_code: ID, connection: DB):
    record = connection.execute(
        "SELECT item_code, category, uom, gst_rate, extras "
        "FROM erp.sku_master WHERE item_code = %s",
        (sku_code,),
    ).fetchone()

    if record is None:
        raise HTTPException(404, "SKU not found")

    return record

@app.get("/po/{po_number}", response_model=PurchaseOrder)
def get_po(po_number: ID, connection: DB):
    header = connection.execute(
        "SELECT p.po_number, p.vendor_id, v.vendor_name, "
        "p.po_date, p.status, p.extras "
        "FROM erp.purchase_orders p "
        "JOIN erp.vendors v ON v.vendor_id = p.vendor_id "
        "WHERE p.po_number = %s",
        (po_number,),
    ).fetchone()

    if header is None:
        raise HTTPException(404, "Purchase order not found")

    lines = connection.execute(
        "SELECT l.line_number, l.item_code, l.description, "
        "l.qty, l.unit_price, l.currency, "
        "l.qty * l.unit_price AS line_total, "
        "COALESCE(l.uom, s.uom) AS uom, "
        "COALESCE(l.tax_rate, s.gst_rate) AS tax_rate, l.extras "
        "FROM erp.po_line_items l "
        "JOIN erp.sku_master s ON s.item_code = l.item_code "
        "WHERE l.po_number = %s ORDER BY l.line_number",
        (po_number,),
    ).fetchall()

    subtotals = connection.execute(
        "SELECT currency, sum(qty * unit_price) AS subtotal "
        "FROM erp.po_line_items WHERE po_number = %s "
        "GROUP BY currency ORDER BY currency",
        (po_number,),
    ).fetchall()

    currencies = [row["currency"] for row in subtotals]

    return {
        **header,
        "line_items": lines,
        "subtotals": subtotals,
        "currencies": currencies,
        "currency": currencies[0] if len(currencies) == 1 else None,
    }