"""Shared helpers for Amazon tests: a connected account and realistic SP-API payloads."""

from __future__ import annotations

from typing import Any

from app.amazon import accounts
from app.amazon.fake import FakeAmazonClient as F

US = "ATVPDKIKX0DER"
CA = "A2EUQ1WTGCTBG2"


def connect(db, *, write_mode: str = "validation_only", marketplaces=("US",)):
    account = accounts.ensure(db)
    account.enabled_marketplaces = list(marketplaces)
    account.write_mode = write_mode
    account.status = "active"
    db.commit()
    return account


def listing_item(sku="SKZ-001", asin="B0SKYZZ001", *, title="Skyzz Mouth Tape 60ct",
                 price=24.99, currency="USD", status=("BUYABLE",), fba=True, brand="Skyzz",
                 product_type="HEALTH_PERSONAL_CARE", issues=(), attributes=None,
                 marketplace_id=US) -> dict[str, Any]:
    """One item as searchListingsItems/getListingsItem returns it."""
    return {
        "sku": sku,
        "summaries": [{"marketplaceId": marketplace_id, "asin": asin, "productType": product_type,
                       "itemName": title, "status": list(status), "brand": brand, "fnSku": "X00SKYZZ01"}],
        "attributes": attributes if attributes is not None else {
            "item_name": [{"value": title, "marketplace_id": marketplace_id}],
            "brand": [{"value": brand, "marketplace_id": marketplace_id}],
            "bullet_point": [{"value": "Gentle on skin", "marketplace_id": marketplace_id}],
        },
        "issues": list(issues),
        "offers": ([{"marketplaceId": marketplace_id, "offerType": "B2C",
                     "price": {"amount": price, "currencyCode": currency}}] if price is not None else []),
        "fulfillmentAvailability": ([{"fulfillmentChannelCode": "AMAZON_NA", "quantity": 0}] if fba
                                    else [{"fulfillmentChannelCode": "DEFAULT", "quantity": 3}]),
    }


def issue(code="90220", message="Main image is missing", severity="ERROR", attributes=("main_product_image_locator",)):
    return {"code": code, "message": message, "severity": severity, "attributeNames": list(attributes)}


def listings_page(items, next_token=None) -> dict[str, Any]:
    page: dict[str, Any] = {"numberOfResults": len(items), "items": items}
    if next_token:
        page["pagination"] = {"nextToken": next_token}
    return page


def inventory_summary(sku="SKZ-001", asin="B0SKYZZ001", *, fulfillable=120, inbound_working=0,
                      inbound_shipped=0, inbound_receiving=0, reserved=0, unfulfillable=0) -> dict[str, Any]:
    return {"sellerSku": sku, "asin": asin, "condition": "NewItem", "totalQuantity": fulfillable,
            "inventoryDetails": {
                "fulfillableQuantity": fulfillable,
                "inboundWorkingQuantity": inbound_working,
                "inboundShippedQuantity": inbound_shipped,
                "inboundReceivingQuantity": inbound_receiving,
                "reservedQuantity": {"totalReservedQuantity": reserved},
                # live Amazon sends this as a breakdown object, not a number
                "unfulfillableQuantity": {"totalUnfulfillableQuantity": unfulfillable,
                                          "customerDamagedQuantity": unfulfillable},
                "researchingQuantity": {"totalResearchingQuantity": 0}}}


def inventory_page(summaries, next_token=None) -> dict[str, Any]:
    page: dict[str, Any] = {"payload": {"inventorySummaries": summaries}}
    if next_token:
        page["pagination"] = {"nextToken": next_token}
    return page


def expect_listings(items, *, page_two=None):
    """Queue one (or two) pages of searchListingsItems."""
    if page_two is None:
        F.expect(r"/listings/2021-08-01/items/", listings_page(items))
    else:
        F.expect(r"/listings/2021-08-01/items/", listings_page(items, next_token="T2"))
        F.expect(r"/listings/2021-08-01/items/", listings_page(page_two))


def expect_inventory(summaries):
    F.expect(r"/fba/inventory/v1/summaries", inventory_page(summaries))


def sales(db, sku, units_per_day=4, days=30, marketplace_id=US, asin="B0SKYZZ001"):
    """Give a SKU a sales history so days-of-cover has something to work with."""
    from datetime import timedelta

    from app.db.models import AmazonSalesDaily
    from app.services.common import utcnow

    today = utcnow().date()
    for i in range(1, days + 1):
        db.add(AmazonSalesDaily(marketplace_id=marketplace_id, asin=asin, sku=sku,
                                date=today - timedelta(days=i), units=units_per_day,
                                ordered_product_sales=units_per_day * 20, currency="USD"))
    db.commit()
