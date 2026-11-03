"""Check Amazon SP-API access: credentials, region, seller id, marketplaces, and which roles work.

Run on the server:   docker compose exec -T api python scripts/amazon_verify_access.py
Locally:             uv run python scripts/amazon_verify_access.py

Read-only: every call is a GET. Nothing is created or changed on the Amazon account.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
ENDPOINTS = {"na": "https://sellingpartnerapi-na.amazon.com",
             "eu": "https://sellingpartnerapi-eu.amazon.com",
             "fe": "https://sellingpartnerapi-fe.amazon.com"}
MARKETPLACES = {"US": "ATVPDKIKX0DER", "CA": "A2EUQ1WTGCTBG2", "MX": "A1AM78C64UM0Y8", "BR": "A2Q3Y263D00KWC"}


@dataclass
class Probe:
    role: str
    what: str
    path: str
    params: dict


def probes(marketplace_id: str, seller_id: str) -> list[Probe]:
    return [
        Probe("Selling Partner Insights", "account and marketplace participation",
              "/sellers/v1/marketplaceParticipations", {}),
        Probe("Product Listing", "product type definitions (listing schemas)",
              "/definitions/2020-09-01/productTypes", {"marketplaceIds": marketplace_id, "keywords": "shirt"}),
        Probe("Product Listing", "catalog search",
              "/catalog/2022-04-01/items", {"marketplaceIds": marketplace_id, "keywords": "tape", "pageSize": 1}),
        Probe("Pricing", "competitive pricing",
              "/products/pricing/v0/competitivePrice",
              {"MarketplaceId": marketplace_id, "Asins": "B0CZ8KHMV5", "ItemType": "Asin"}),
        Probe("Inventory and Order Tracking", "FBA inventory summaries",
              "/fba/inventory/v1/summaries",
              {"granularityType": "Marketplace", "granularityId": marketplace_id,
               "marketplaceIds": marketplace_id}),
        Probe("Amazon Fulfillment", "FBA inbound plans (needed for shipments)",
              "/inbound/fba/2024-03-20/inboundPlans", {"pageSize": 1}),
        # Labels are a separate permission from the rest of Fulfillment Inbound: reading shipments can
        # work while downloading their box labels is refused, which is exactly what happened on the
        # first real shipment. Probed on its own so the gap shows up here and not mid-shipment.
        Probe("Amazon Fulfillment (labels)", "FBA box labels",
              "/fba/inbound/v0/shipments/labels",
              {"ShipmentId": "FBA0000000000", "PageType": "PackageLabel_Letter_6",
               "LabelType": "BARCODE_2D", "NumberOfPackages": "1"}),
        Probe("Finance and Accounting", "financial event groups",
              "/finances/v0/financialEventGroups",
              {"MaxResultsPerPage": 1,
               "FinancialEventGroupStartedAfter": (datetime.now(UTC) - timedelta(days=30)).isoformat()}),
        Probe("Brand Analytics / Reports", "report types (sales and traffic)",
              "/reports/2021-06-30/reports", {"reportTypes": "GET_SALES_AND_TRAFFIC_REPORT", "pageSize": 1}),
    ]


def access_token(s) -> str:
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": s.amazon_lwa_refresh_token.get_secret_value(),
        "client_id": s.amazon_lwa_client_id,
        "client_secret": s.amazon_lwa_client_secret.get_secret_value(),
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Login with Amazon refused the refresh token ({r.status_code}): {r.text[:200]}")
    return r.json()["access_token"]


def main() -> None:
    s = get_settings()
    missing = [k for k, v in {
        "AMAZON_LWA_CLIENT_ID": s.amazon_lwa_client_id,
        "AMAZON_LWA_CLIENT_SECRET": s.amazon_lwa_client_secret.get_secret_value(),
        "AMAZON_LWA_REFRESH_TOKEN": s.amazon_lwa_refresh_token.get_secret_value(),
        "AMAZON_SELLER_ID": s.amazon_seller_id,
    }.items() if not v]
    if missing:
        sys.exit("Missing in .env: " + ", ".join(missing))

    base = ENDPOINTS[s.amazon_region]
    enabled = [m.strip().upper() for m in s.amazon_enabled_marketplaces.split(",") if m.strip()]
    marketplace_id = MARKETPLACES[enabled[0]]
    print(f"Region {s.amazon_region} ({base})")
    print(f"Seller {s.amazon_seller_id} · marketplaces enabled for writes: {', '.join(enabled)}")
    print(f"Write mode: {s.amazon_write_mode}\n")

    token = access_token(s)
    print("Login with Amazon: access token obtained ✅\n")

    ok, denied, other = [], [], []
    with httpx.Client(timeout=30, headers={"x-amz-access-token": token}) as client:
        for p in probes(marketplace_id, s.amazon_seller_id):
            try:
                r = client.get(base + p.path, params=p.params)
            except httpx.HTTPError as exc:
                other.append((p, f"unreachable: {exc}"))
                continue
            if r.status_code < 300:
                ok.append(p)
                mark = "✅"
            elif r.status_code in (401, 403):
                denied.append((p, r.text[:160]))
                mark = "⛔"
            else:
                other.append((p, f"{r.status_code}: {r.text[:160]}"))
                mark = "⚠️ "
            print(f"{mark} {p.role:<30} {p.what:<42} HTTP {r.status_code}")

    print(f"\nWorking: {len(ok)}   Denied: {len(denied)}   Other: {len(other)}")
    for p, body in denied:
        print(f"\n⛔ {p.role} — {p.what}\n   {body}")
    for p, body in other:
        print(f"\n⚠️  {p.role} — {p.what}\n   {body}")
    if denied:
        print("\nDenied roles usually mean the app client is missing that role, or the authorisation "
              "was created before the role was added. Re-authorise the app in the Solution Provider Portal.")


if __name__ == "__main__":
    main()
