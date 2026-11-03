"""Amazon Ads regional hosts, confirmed against the live docs on 2026-09-24.

https://advertising.amazon.com/API/docs/en-us/reference/api-overview
"""

from __future__ import annotations

REGION_ENDPOINTS = {
    "na": "https://advertising-api.amazon.com",     # US, CA, MX, BR
    "eu": "https://advertising-api-eu.amazon.com",
    "fe": "https://advertising-api-fe.amazon.com",
}
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# Which region serves a marketplace, so a profile's country picks its own host.
MARKETPLACE_REGION = {
    "US": "na", "CA": "na", "MX": "na", "BR": "na",
    "UK": "eu", "GB": "eu", "FR": "eu", "IT": "eu", "ES": "eu", "DE": "eu", "NL": "eu", "AE": "eu",
    "PL": "eu", "TR": "eu", "EG": "eu", "SA": "eu", "SE": "eu", "BE": "eu", "IN": "eu", "ZA": "eu",
    "JP": "fe", "AU": "fe", "SG": "fe",
}


# Ads reporting dates are in the profile's own time zone (brief 4.4), and Amazon reports that zone
# on the profile. This map is only the fallback used before a profile has been discovered.
MARKETPLACE_TIMEZONE = {
    "US": "America/Los_Angeles", "CA": "America/Los_Angeles", "MX": "America/Los_Angeles",
    "BR": "America/Sao_Paulo",
    "UK": "Europe/London", "GB": "Europe/London", "DE": "Europe/Berlin", "FR": "Europe/Paris",
    "IT": "Europe/Rome", "ES": "Europe/Madrid", "NL": "Europe/Amsterdam", "SE": "Europe/Stockholm",
    "PL": "Europe/Warsaw", "BE": "Europe/Brussels", "TR": "Europe/Istanbul", "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh", "EG": "Africa/Cairo", "IN": "Asia/Kolkata", "ZA": "Africa/Johannesburg",
    "JP": "Asia/Tokyo", "AU": "Australia/Sydney", "SG": "Asia/Singapore",
}


def timezone_for(country_code: str) -> str:
    return MARKETPLACE_TIMEZONE.get(country_code.strip().upper(), "America/Los_Angeles")


def endpoint(region: str) -> str:
    try:
        return REGION_ENDPOINTS[region.strip().lower()]
    except KeyError:
        raise KeyError(f"Unknown Ads region '{region}'. Known: {', '.join(REGION_ENDPOINTS)}") from None


def region_for(country_code: str) -> str:
    try:
        return MARKETPLACE_REGION[country_code.strip().upper()]
    except KeyError:
        raise KeyError(f"No Ads region known for marketplace '{country_code}'") from None
