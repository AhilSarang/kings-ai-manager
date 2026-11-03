"""WhatsApp webhook: verify, allow-list, dedupe, store, enqueue, return 200 fast."""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.channels import get_channel
from app.channels.base import ParsedWebhook
from app.config import get_settings
from app.db.models import Message
from app.db.session import session_scope
from app.jobs import queue
from app.services import audit, outbound, users
from app.services.common import SYSTEM, Actor, utcnow

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> str:
    expected = get_settings().whatsapp_verify_token.get_secret_value()
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/webhooks/whatsapp")
async def receive_webhook(request: Request) -> dict:
    raw = await request.body()
    channel = get_channel()
    if not channel.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        log.warning("webhook signature rejected", extra={"client": request.client.host if request.client else None})
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    parsed = channel.parse_webhook(payload)
    stats = await run_in_threadpool(_ingest, parsed)
    return {"status": "ok", **stats}


def _ingest(parsed: ParsedWebhook) -> dict:
    with session_scope() as session:
        return ingest(session, parsed)


def ingest(session: Session, parsed: ParsedWebhook) -> dict:
    stats = {"accepted": 0, "duplicates": 0, "rejected": 0, "statuses": 0}
    for st in parsed.statuses:
        if outbound.update_status(session, st.provider_message_id, st.status, st.error):
            stats["statuses"] += 1

    for m in parsed.messages:
        user = users.get_active_by_number(session, m.from_number)
        if user is None:
            stats["rejected"] += 1
            audit.record(session, SYSTEM, "inbound_rejected", target_type="phone",
                         target_id=m.from_number[-20:],
                         details={"reason": "sender not on allow-list", "raw": m.raw})
            continue

        stmt = (
            insert(Message)
            .values(direction="inbound", channel="whatsapp", provider_message_id=m.provider_message_id,
                    user_id=user.id, body=m.text or "", status="received",
                    raw_payload={**m.raw, "_type": m.type})
            .on_conflict_do_nothing(index_elements=[Message.provider_message_id])
            .returning(Message.id)
        )
        message_id = session.execute(stmt).scalar_one_or_none()
        if message_id is None:
            stats["duplicates"] += 1
            continue
        stats["accepted"] += 1

        user.last_inbound_at = utcnow()
        audit.record(session, Actor.user(user.id), "message_received", target_type="message",
                     target_id=message_id, trigger_message_id=message_id,
                     details={"type": m.type, "chars": len(m.text or "")})
        # The owner is active again: the 24h window is open, release held updates.
        queue.enqueue(session, "release_held_messages", {"user_id": str(user.id), "reasons": ["window"]},
                      priority=15, idempotency_key=f"release:{m.provider_message_id}")
        if m.media_id:
            queue.enqueue(session, "download_media", {
                "message_id": str(message_id), "media_id": m.media_id,
                "mime_type": m.media_mime, "filename": m.filename,
            }, priority=20, idempotency_key=f"media:{m.provider_message_id}")
        else:
            queue.enqueue(session, "process_inbound_message", {"message_id": str(message_id)},
                          priority=20, idempotency_key=f"inbound:{m.provider_message_id}")
    return stats
