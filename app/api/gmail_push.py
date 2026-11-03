"""Pub/Sub push endpoint for Gmail notifications (OIDC-authenticated)."""

from __future__ import annotations

import base64
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.config import get_settings
from app.db.models import EmailAccount
from app.db.session import session_scope
from app.email.sync import enqueue_sync

log = logging.getLogger(__name__)
router = APIRouter()


def verify_push_token(token: str) -> dict:
    """Validate Google's OIDC token: signature, audience, issuer, and our service account."""
    import google.auth.transport.requests
    from google.oauth2 import id_token

    s = get_settings()
    claims = id_token.verify_oauth2_token(token, google.auth.transport.requests.Request(),
                                          audience=s.gmail_push_audience)
    if claims.get("email") != s.gmail_push_service_account or not claims.get("email_verified"):
        raise ValueError("push token is not from the configured service account")
    return claims


@router.post("/webhooks/gmail", status_code=204)
async def gmail_push(request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=403, detail="missing token")
    try:
        await run_in_threadpool(verify_push_token, auth.split(" ", 1)[1].strip())
    except Exception as exc:  # noqa: BLE001 - any verification failure is a rejection
        log.warning("gmail push rejected", extra={"reason": str(exc)[:120]})
        raise HTTPException(status_code=403, detail="invalid token") from None
    try:
        envelope = await request.json()
        data = json.loads(base64.b64decode(envelope["message"]["data"]))
        address = str(data["emailAddress"]).lower()
    except (ValueError, KeyError, TypeError):
        return Response(status_code=204)  # malformed: acknowledge so Pub/Sub doesn't retry forever
    await run_in_threadpool(_enqueue, address)
    return Response(status_code=204)


def _enqueue(address: str) -> None:
    with session_scope() as s:
        account = s.execute(select(EmailAccount).where(EmailAccount.address == address)).scalar_one_or_none()
        if account is not None and account.status == "active":
            enqueue_sync(s, account.id, delay_seconds=5)
