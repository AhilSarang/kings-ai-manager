"""Marketplace and region configuration (brief Section 4.1). Marketplace-aware from day one."""

from __future__ import annotations

from dataclasses import dataclass

REGION_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


@dataclass(frozen=True)
class Marketplace:
    code: str
    marketplace_id: str
    currency: str
    locale: str
    region: str
    fulfillment_channel: str  # fulfillment_availability channel code for FBA
    country: str


MARKETPLACES: dict[str, Marketplace] = {
    "US": Marketplace("US", "ATVPDKIKX0DER", "USD", "en_US", "na", "AMAZON_NA", "US"),
    "CA": Marketplace("CA", "A2EUQ1WTGCTBG2", "CAD", "en_CA", "na", "AMAZON_NA", "CA"),
    "MX": Marketplace("MX", "A1AM78C64UM0Y8", "MXN", "es_MX", "na", "AMAZON_NA", "MX"),
    "BR": Marketplace("BR", "A2Q3Y263D00KWC", "BRL", "pt_BR", "na", "AMAZON_NA", "BR"),
}
BY_ID = {m.marketplace_id: m for m in MARKETPLACES.values()}


def by_code(code: str) -> Marketplace:
    try:
        return MARKETPLACES[code.strip().upper()]
    except KeyError:
        raise KeyError(f"Unknown marketplace '{code}'. Known: {', '.join(MARKETPLACES)}") from None


def by_id(marketplace_id: str) -> Marketplace:
    try:
        return BY_ID[marketplace_id]
    except KeyError:
        raise KeyError(f"Unknown marketplace id '{marketplace_id}'") from None


def enabled(setting_value: str) -> list[Marketplace]:
    """Marketplaces the agent may write to, from AMAZON_ENABLED_MARKETPLACES."""
    codes = [c.strip().upper() for c in (setting_value or "").split(",") if c.strip()]
    return [by_code(c) for c in codes]
