import json
import uuid
from datetime import UTC, datetime

from app.channels.meta_whatsapp import compute_signature

SECRET = "test-app-secret"
OWNER = "+18045550100"


def text_payload(text: str, sender: str = OWNER, msg_id: str | None = None) -> dict:
    return _payload({"type": "text", "text": {"body": text}}, sender, msg_id)


def media_payload(media_id: str, mime: str, caption: str = "", sender: str = OWNER,
                  kind: str = "image", filename: str | None = None, msg_id: str | None = None) -> dict:
    media = {"id": media_id, "mime_type": mime, "caption": caption}
    if filename:
        media["filename"] = filename
    return _payload({"type": kind, kind: media}, sender, msg_id)


def status_payload(provider_id: str, status: str) -> dict:
    return {"entry": [{"changes": [{"value": {"statuses": [{"id": provider_id, "status": status}]}}]}]}


def _payload(body: dict, sender: str, msg_id: str | None) -> dict:
    msg = {"from": sender.lstrip("+"), "id": msg_id or f"wamid.{uuid.uuid4().hex}",
           "timestamp": str(int(datetime.now(UTC).timestamp())), **body}
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "X", "changes": [{"field": "messages", "value": {"messages": [msg]}}]}]}


def post(client, payload: dict, signature: str | None = "auto"):
    raw = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Hub-Signature-256"] = compute_signature(SECRET, raw)
    elif signature:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhooks/whatsapp", content=raw, headers=headers)


# ---- secret leak checks ----
# Random hex is everywhere in stored rows: UUID primary keys, SHA-256 payload hashes, WhatsApp
# message ids. A four-digit PIN turns up inside a 64-character hex digest about one run in a
# hundred, so `"4821" not in str(rows)` is a check that fails for reasons having nothing to do with
# a leak — and a security test that cries wolf gets re-run until it is quiet, which is worse than
# not having it. These walk the structure instead and skip the fields whose content is random by
# construction, so a real leak still fails and a coincidence does not.
_HEXISH = set("0123456789abcdefABCDEF-")


def _random_hex(value: str) -> bool:
    """A UUID, a digest, or a `wamid.` message id — content we generated, not content we stored."""
    body = value[6:] if value.startswith("wamid.") else value
    return len(body) >= 16 and all(char in _HEXISH for char in body)


def strings_in(value, *, skip_random_hex: bool = True) -> list[str]:
    """Every string leaf in a nested structure, minus the randomly-generated identifiers."""
    found: list[str] = []
    if isinstance(value, str):
        if not (skip_random_hex and _random_hex(value)):
            found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(strings_in(key, skip_random_hex=skip_random_hex))
            found.extend(strings_in(item, skip_random_hex=skip_random_hex))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found.extend(strings_in(item, skip_random_hex=skip_random_hex))
    elif value is not None and not isinstance(value, bool):
        found.append(str(value))
    return found


def leaked(secret: str, *values) -> list[str]:
    """The stored strings containing `secret`. Empty is the only acceptable answer."""
    return [text for value in values for text in strings_in(value) if secret in text]
