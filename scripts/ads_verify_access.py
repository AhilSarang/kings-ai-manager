"""Check Amazon Ads API access: credentials, profiles, account id, and which calls work.

    docker compose exec worker python scripts/ads_verify_access.py

Reads only. Prints one line per probe so a missing scope shows up here rather than mid-analysis.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.ppc.regions import LWA_TOKEN_URL, endpoint  # noqa: E402


@dataclass
class Probe:
    what: str
    path: str
    params: dict
    needs_profile: bool = True
    reporting: bool = False


PROBES = [
    Probe("advertising profiles for this account", "/v2/profiles", {}, needs_profile=False),
    Probe("Sponsored Products campaigns", "/sp/campaigns/list", {}),
    Probe("portfolios", "/portfolios", {}),
    Probe("bid recommendations (read-only input)", "/sp/targets/bid/recommendations", {}),
    Probe("report types (v3 reporting)", "/reporting/reports", {}, reporting=True),
]


def access_token(s) -> str:
    r = httpx.post(LWA_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": s.ads_lwa_refresh_token.get_secret_value(),
        "client_id": s.ads_lwa_client_id,
        "client_secret": s.ads_lwa_client_secret.get_secret_value(),
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Login with Amazon refused the Ads refresh token ({r.status_code}): {r.text[:200]}\n"
                 "Re-authorise the Ads application. Tokens issued after 30 July 2026 last 365 days.")
    return r.json()["access_token"]


def main() -> None:
    s = get_settings()
    missing = [k for k, v in {
        "ADS_LWA_CLIENT_ID": s.ads_lwa_client_id,
        "ADS_LWA_CLIENT_SECRET": s.ads_lwa_client_secret.get_secret_value(),
        "ADS_LWA_REFRESH_TOKEN": s.ads_lwa_refresh_token.get_secret_value(),
    }.items() if not v]
    if missing:
        sys.exit("Missing in .env: " + ", ".join(missing))

    base = endpoint(s.ads_region)
    print(f"Region {s.ads_region} ({base})")
    print(f"Profiles configured: US={s.ads_profile_id_us or '—'}  CA={s.ads_profile_id_ca or '—'}")
    print(f"Account id (reporting): {s.ads_account_id or '— not set, v3 reports will be refused'}")
    print(f"Write mode: {s.ppc_write_mode}\n")

    token = access_token(s)
    print("Login with Amazon: access token obtained ✅\n")

    headers = {"Authorization": f"Bearer {token}",
               "Amazon-Advertising-API-ClientId": s.ads_lwa_client_id,
               "Amazon-Ads-ClientId": s.ads_lwa_client_id}
    working = denied = other = 0
    problems = []
    with httpx.Client(timeout=60) as http:
        for probe in PROBES:
            request_headers = dict(headers)
            if probe.needs_profile:
                if not s.ads_profile_id_us:
                    print(f"—  {probe.what:<44} skipped (no US profile id yet)")
                    continue
                request_headers["Amazon-Advertising-API-Scope"] = s.ads_profile_id_us
            if probe.reporting and s.ads_account_id:
                request_headers["Amazon-Ads-AccountId"] = s.ads_account_id
            r = http.get(base + probe.path, params=probe.params, headers=request_headers)
            mark = "✅" if r.status_code < 300 else ("⛔" if r.status_code in (401, 403) else "⚠️ ")
            print(f"{mark} {probe.what:<44} HTTP {r.status_code}")
            if r.status_code < 300:
                working += 1
                if probe.path == "/v2/profiles":
                    for profile in (r.json() or [])[:10]:
                        print(f"     profile {profile.get('profileId')} "
                              f"{(profile.get('countryCode') or '?')} "
                              f"{(profile.get('accountInfo') or {}).get('type', '')}")
            else:
                denied += 1 if r.status_code in (401, 403) else 0
                other += 0 if r.status_code in (401, 403) else 1
                problems.append((probe.what, r.status_code, r.text[:400]))

    print(f"\nWorking: {working}   Denied: {denied}   Other: {other}")
    for what, status, body in problems:
        print(f"\n{'⛔' if status in (401, 403) else '⚠️ '} {what}\n   {body}")
    if denied:
        print("\nDenied usually means the application isn't approved for this advertising account, "
              "or the authorisation predates the approval. Re-authorise in the Ads console.")


if __name__ == "__main__":
    main()
