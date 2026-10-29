
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from ..settings import Settings

logger = logging.getLogger(__name__)


class ERPUnavailable(RuntimeError):
    """The ERP service could not be reached or returned a server error."""


class ERPClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.erp_base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.erp_timeout_seconds),
            headers={"accept": "application/json"},
            follow_redirects=False,
        )

    def __enter__(self) -> ERPClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        return self._base_url

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ERPUnavailable(
                f"cannot reach the ERP service at {self._base_url}: {type(exc).__name__}"
            ) from None

        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise ERPUnavailable(
                f"ERP service returned {response.status_code} for {path}"
            )
        response.raise_for_status()
        return response.json()

    def health(self) -> dict[str, Any]:
        payload = self._get("/health")
        if payload is None:
            raise ERPUnavailable("ERP health endpoint returned 404")
        return payload

    def get_purchase_order(self, po_number: str) -> dict[str, Any] | None:
        if not po_number:
            return None
        return self._get(f"/po/{_quote(po_number)}")

    def get_vendor(self, vendor_id: str) -> dict[str, Any] | None:
        if not vendor_id:
            return None
        return self._get(f"/vendor/{_quote(vendor_id)}")

    def get_sku(self, sku_code: str) -> dict[str, Any] | None:
        if not sku_code:
            return None
        return self._get(f"/sku/{_quote(sku_code)}")

    def find_vendors(self, name: str) -> list[dict[str, Any]]:
        if not name:
            return []
        payload = self._get("/vendors", params={"name": name[:255]})
        return payload or []

    def resolve_vendor(self, vendor_name: str | None, vendor_id: str | None) -> dict[str, Any] | None:
        """Vendor by id when the invoice carries one, otherwise by name.

        Most invoices in the dataset print a trading name and no vendor code,
        so a name lookup is the normal path rather than the exception.
        """
        if vendor_id:
            vendor = self.get_vendor(vendor_id)
            if vendor:
                return vendor

        if not vendor_name:
            return None

        matches = self.find_vendors(vendor_name)
        if len(matches) == 1:
            return matches[0]
        if matches:
            return _best_name_match(vendor_name, matches)

        head = _significant_head(vendor_name)
        if head and head.lower() != vendor_name.lower():
            matches = self.find_vendors(head)
            if matches:
                return _best_name_match(vendor_name, matches)
        return None

    def close(self) -> None:
        self._client.close()


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value.strip(), safe="")


def _significant_head(name: str) -> str:
    tokens = [t for t in name.replace(",", " ").split() if len(t) > 2]
    return " ".join(tokens[:2]) if tokens else ""


def _best_name_match(name: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    from difflib import SequenceMatcher

    target = name.lower()
    return max(
        candidates,
        key=lambda c: SequenceMatcher(None, target, (c.get("vendor_name") or "").lower()).ratio(),
    )


def po_line_decimal(line: dict[str, Any], key: str) -> Decimal | None:
    """ERP amounts arrive as decimal strings and must stay exact."""
    value = line.get(key)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
