"""Amazon Ads profiles: one row per marketplace the advertising account can reach.

A profile is to the PPC agent what the Amazon account row is to Phase 3 — everything hangs off it,
and nothing touches Amazon without one. Profiles are *discovered* from `/v2/profiles` rather than
configured, because Amazon decides which ones this application may see; a newly discovered profile
arrives `paused` so that connecting a new marketplace never silently starts advertising in it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AmazonAccount, PpcProfile
from app.ppc import api, regions
from app.services import settings_store
from app.services.common import NotFound, ServiceError, utcnow

log = logging.getLogger(__name__)

PROFILES_PATH = "/v2/profiles"


class PpcNotConnected(ServiceError):
    """Amazon Ads isn't usable right now — no profile, or the one we have is in trouble."""


# ---------------- reading what we have ----------------
def get(session: Session, amazon_profile_id: str) -> PpcProfile | None:
    return session.execute(select(PpcProfile).where(
        PpcProfile.amazon_profile_id == str(amazon_profile_id))).scalar_one_or_none()


def by_country(session: Session, country_code: str) -> PpcProfile | None:
    return session.execute(select(PpcProfile).where(
        PpcProfile.country_code == country_code.strip().upper()).order_by(
        PpcProfile.created_at)).scalars().first()


def first(session: Session) -> PpcProfile | None:
    """Any profile we hold, whatever its status — so a broken one can explain itself."""
    return session.execute(select(PpcProfile).order_by(PpcProfile.created_at)).scalars().first()


def active(session: Session) -> list[PpcProfile]:
    """Every profile the agent is allowed to work on, in a stable order."""
    return list(session.execute(select(PpcProfile).where(PpcProfile.status == "active").order_by(
        PpcProfile.country_code, PpcProfile.created_at)).scalars())


def require(session: Session, country_code: str | None = None) -> PpcProfile:
    """The profile to work in, or a message saying exactly what is missing.

    Like `amazon.accounts.require`, this builds the row from `.env` on first use: the settings name
    the US profile, so a fresh deployment doesn't need a discovery run before it can read anything.
    """
    if country_code:
        profile = by_country(session, country_code)
    else:
        # A profile that exists but isn't usable must say why, rather than being reported as missing.
        usable = active(session)
        profile = usable[0] if usable else first(session)
    if profile is None:
        profile = ensure_from_settings(session, country_code or "US")
    if profile is None:
        setting = f"ADS_PROFILE_ID_{(country_code or 'US').strip().upper()}"
        raise PpcNotConnected(
            "Amazon Ads isn't connected yet. Register the Ads application, then set "
            f"ADS_LWA_CLIENT_ID, ADS_LWA_CLIENT_SECRET, ADS_LWA_REFRESH_TOKEN and {setting}.")
    if profile.status != "active":
        raise PpcNotConnected(
            f"The {profile.country_code} advertising profile is {profile.status.replace('_', ' ')}"
            f"{': ' + profile.status_reason if profile.status_reason else ''}.")
    return profile


def get_by_id(session: Session, profile_id) -> PpcProfile:
    row = session.get(PpcProfile, profile_id)
    if row is None:
        raise NotFound("I can't find that advertising profile.")
    return row


# ---------------- settings and write mode ----------------
def ensure_from_settings(session: Session, country_code: str = "US") -> PpcProfile | None:
    """Create the row named in `.env` so the first read works before any discovery run."""
    s = get_settings()
    code = country_code.strip().upper()
    configured = {"US": s.ads_profile_id_us, "CA": s.ads_profile_id_ca}.get(code)
    if not configured:
        return None
    existing = get(session, configured)
    if existing is not None:
        return existing
    from app.amazon import marketplaces as mkt

    marketplace = mkt.by_code(code)
    profile = PpcProfile(
        amazon_profile_id=str(configured), ads_account_id=s.ads_account_id or None,
        marketplace_id=marketplace.marketplace_id, country_code=code,
        region=regions.region_for(code), currency=marketplace.currency,
        timezone=regions.timezone_for(code), account_type="seller",
        # Configured by the owner in `.env`, so it starts usable rather than waiting to be enabled.
        status="active", write_mode=_write_mode(session))
    profile.amazon_account_id = _amazon_account_id(session)
    session.add(profile)
    session.flush()
    log.info("ppc profile created from settings", extra={"profile": profile.amazon_profile_id})
    return profile


def _write_mode(session: Session) -> str:
    """Stored setting beats `.env`, exactly as Phase 3 does, so a stale file can't re-enable writes."""
    stored = settings_store.get(session, "ppc_write_mode", None)
    if stored in ("read_only", "live"):
        return stored
    mode = get_settings().ppc_write_mode
    return mode if mode in ("read_only", "live") else "read_only"


def _amazon_account_id(session: Session):
    account = session.execute(select(AmazonAccount).order_by(AmazonAccount.created_at)).scalars().first()
    return account.id if account else None


def writes_allowed(session: Session, profile: PpcProfile | None = None) -> bool:
    if _write_mode(session) != "live":
        return False
    profile = profile or (active(session)[0] if active(session) else None)
    return bool(profile and profile.status == "active" and profile.write_mode == "live")


def set_status(session: Session, profile: PpcProfile, status: str, reason: str | None = None) -> None:
    profile.status = status
    profile.status_reason = reason
    session.flush()


# ---------------- discovery ----------------
def _timezone(raw: dict[str, Any], fallback: str) -> str:
    return (raw.get("timezone") or raw.get("timeZone") or fallback)[:60]


def upsert(session: Session, raw: dict[str, Any]) -> PpcProfile:
    """Store one profile as Amazon describes it, without changing whether we may use it."""
    amazon_profile_id = str(raw.get("profileId") or "").strip()
    if not amazon_profile_id:
        raise ServiceError("Amazon returned an advertising profile with no profileId.")
    info = raw.get("accountInfo") or {}
    code = (raw.get("countryCode") or "").strip().upper()
    profile = get(session, amazon_profile_id)
    fresh = profile is None
    if profile is None:
        profile = PpcProfile(amazon_profile_id=amazon_profile_id, country_code=code or "US",
                             marketplace_id=info.get("marketplaceStringId") or "",
                             currency=(raw.get("currencyCode") or "USD")[:3],
                             timezone=_timezone(raw, regions.timezone_for(code or "US")),
                             # Discovery never enables a marketplace: the owner does, from WhatsApp.
                             status="paused", status_reason="waiting to be enabled",
                             write_mode="read_only")
        session.add(profile)
    if code:
        profile.country_code = code
        try:
            profile.region = regions.region_for(code)
        except KeyError:
            log.warning("no Ads region known for %s; leaving %s", code, profile.region)
    profile.marketplace_id = info.get("marketplaceStringId") or profile.marketplace_id
    profile.currency = (raw.get("currencyCode") or profile.currency)[:3]
    profile.timezone = _timezone(raw, profile.timezone)
    account_type = (info.get("type") or "").strip().lower()
    if account_type in ("seller", "vendor", "agency"):
        profile.account_type = account_type
    profile.account_name = (info.get("name") or profile.account_name or None)
    profile.seller_id = info.get("id") or profile.seller_id
    profile.ads_account_id = profile.ads_account_id or get_settings().ads_account_id or None
    profile.amazon_account_id = profile.amazon_account_id or _amazon_account_id(session)
    profile.last_verified_at = utcnow()
    session.flush()
    if fresh:
        log.info("ppc profile discovered", extra={"profile": amazon_profile_id, "country": code})
    return profile


def sync(session: Session, client=None) -> dict[str, Any]:
    """Read `/v2/profiles` and store every profile this application may see.

    The profile list is not scoped to a profile (it is how you learn the profile ids), so this is the
    one Ads call made without the scope header.
    """
    client = client or api.client()
    raw = client.get(PROFILES_PATH, operation="listProfiles")
    rows = raw if isinstance(raw, list) else (raw or {}).get("profiles") or []
    known = {p.amazon_profile_id for p in session.execute(select(PpcProfile)).scalars()}
    seen, new, healed = [], [], []
    for row in rows:
        try:
            profile = upsert(session, row)
        except ServiceError as exc:
            log.warning("skipping an advertising profile: %s", exc)
            continue
        seen.append(profile.amazon_profile_id)
        if profile.amazon_profile_id not in known:
            new.append(profile)
        elif profile.status == "auth_error":
            # Amazon is listing it again, so whatever was wrong has been put right. Collection
            # restarts by itself: the owner was told to reply PPC RESUME, and there is no such
            # command, so waiting for one would have meant PPC never came back.
            set_status(session, profile, "active", None)
            healed.append(profile)
    if seen:
        # Only judge a profile missing when Amazon actually listed some. An empty or unexpected
        # response is far more likely to be our request than the loss of every marketplace, and
        # treating it as lost access would disable all collection until someone noticed.
        for profile in session.execute(select(PpcProfile)).scalars():
            if profile.amazon_profile_id not in seen and profile.status == "active":
                set_status(session, profile, "auth_error",
                           "Amazon no longer lists this profile for this application.")
    else:
        log.warning("Amazon listed no advertising profiles; leaving the ones we have alone")
    session.flush()
    stats = {"profiles": len(seen), "new": [p.amazon_profile_id for p in new],
             "recovered": [p.amazon_profile_id for p in healed]}
    log.info("ppc profiles synced", extra=stats)
    return stats


def enable(session: Session, profile: PpcProfile, *, write_mode: str | None = None) -> PpcProfile:
    """Turn a discovered profile on. Writes stay off until the write mode is changed with a PIN."""
    profile.status = "active"
    profile.status_reason = None
    if write_mode in ("read_only", "live"):
        profile.write_mode = write_mode
    session.flush()
    return profile
