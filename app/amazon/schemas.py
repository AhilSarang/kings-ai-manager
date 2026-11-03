"""Product type schema cache (brief 4.4): listings are validated locally before Amazon sees them."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon import api
from app.amazon import marketplaces as mkt
from app.amazon.errors import AmazonError
from app.db.models import AmazonProductTypeSchema
from app.services.common import utcnow

log = logging.getLogger(__name__)
MAX_AGE = timedelta(days=7)


def search_product_types(marketplace_code: str, keywords: str) -> list[dict[str, Any]]:
    mp = mkt.by_code(marketplace_code)
    resp = api.client().get("/definitions/2020-09-01/productTypes",
                            params={"marketplaceIds": mp.marketplace_id, "keywords": keywords},
                            operation="searchDefinitionsProductTypes")
    return resp.get("productTypes") or []


def fetch_schema(marketplace_code: str, product_type: str) -> tuple[dict[str, Any], str | None]:
    """Amazon returns a link to the JSON Schema; download it."""
    mp = mkt.by_code(marketplace_code)
    meta = api.client().get(f"/definitions/2020-09-01/productTypes/{product_type}",
                            params={"marketplaceIds": mp.marketplace_id, "locale": mp.locale,
                                    "requirements": "LISTING", "sellerId": _seller_id()},
                            operation="getDefinitionsProductType")
    link = ((meta.get("schema") or {}).get("link") or {}).get("resource")
    if not link:
        raise AmazonError(f"Amazon returned no schema link for product type {product_type}")
    r = httpx.get(link, timeout=60)
    if r.status_code != 200:
        raise AmazonError(f"Could not download the {product_type} schema ({r.status_code})")
    return r.json(), meta.get("productTypeVersion", {}).get("version")


def _seller_id() -> str:
    from app.config import get_settings

    return get_settings().amazon_seller_id


def get_schema(session: Session, marketplace_code: str, product_type: str, *,
               refresh: bool = False) -> dict[str, Any]:
    mp = mkt.by_code(marketplace_code)
    row = session.execute(select(AmazonProductTypeSchema).where(
        AmazonProductTypeSchema.product_type == product_type,
        AmazonProductTypeSchema.marketplace_id == mp.marketplace_id,
        AmazonProductTypeSchema.locale == mp.locale)).scalar_one_or_none()
    fresh = row is not None and row.fetched_at and row.fetched_at > utcnow() - MAX_AGE
    if row is not None and fresh and not refresh:
        return row.schema_json
    schema, version = fetch_schema(marketplace_code, product_type)
    if row is None:
        row = AmazonProductTypeSchema(product_type=product_type, marketplace_id=mp.marketplace_id,
                                      locale=mp.locale)
        session.add(row)
    row.schema_json, row.schema_version, row.fetched_at = schema, version, utcnow()
    session.flush()
    return schema


def refresh_all(session: Session) -> int:
    rows = list(session.execute(select(AmazonProductTypeSchema)).scalars())
    for row in rows:
        try:
            get_schema(session, mkt.by_id(row.marketplace_id).code, row.product_type, refresh=True)
        except AmazonError as exc:
            log.warning("schema refresh failed", extra={"product_type": row.product_type, "error": str(exc)[:120]})
    session.commit()
    return len(rows)
