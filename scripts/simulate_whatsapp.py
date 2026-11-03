"""Post fake, correctly signed WhatsApp webhooks to the API — no phone needed.

Examples:
  uv run python scripts/simulate_whatsapp.py "Find 20 suppliers for silicone mouth tape, high priority"
  uv run python scripts/simulate_whatsapp.py "attach this to T-5" --file ~/Desktop/shot.png
  uv run python scripts/simulate_whatsapp.py "hello" --from +15550001111      # not on allow-list
  uv run python scripts/simulate_whatsapp.py "STATUS" --id wamid.fixed --id-reuse   # dedupe check
Inside Docker:
  docker compose exec api python scripts/simulate_whatsapp.py "STATUS"

Set MESSAGING_BACKEND=console so replies are logged instead of sent to Meta.
Replies are read back from the messages table and printed.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.channels.meta_whatsapp import compute_signature  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import Message  # noqa: E402
from app.db.session import session_scope  # noqa: E402


def build_payload(sender: str, text: str, msg_id: str, media: dict | None) -> dict:
    message: dict = {"from": sender.lstrip("+"), "id": msg_id,
                     "timestamp": str(int(datetime.now(UTC).timestamp()))}
    if media:
        kind = "image" if media["mime_type"].startswith("image/") else "document"
        message["type"] = kind
        message[kind] = {"id": media["id"], "mime_type": media["mime_type"], "caption": text}
        if kind == "document":
            message[kind]["filename"] = media["filename"]
    else:
        message["type"] = "text"
        message["text"] = {"body": text}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "SIM", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550000000", "phone_number_id": "SIM"},
            "contacts": [{"profile": {"name": "Simulator"}, "wa_id": sender.lstrip("+")}],
            "messages": [message],
        }}]}],
    }


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--from", dest="sender", default=s.owner_whatsapp_number)
    ap.add_argument("--file", help="attach an image/PDF")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--id", dest="msg_id", default=None, help="provider message id (for dedupe tests)")
    ap.add_argument("--bad-signature", action="store_true")
    ap.add_argument("--wait", type=float, default=90, help="seconds to wait for replies (0 = don't wait)")
    args = ap.parse_args()

    secret = s.whatsapp_app_secret.get_secret_value()
    if not secret:
        sys.exit("WHATSAPP_APP_SECRET must be set (any value works for simulation)")

    media = None
    if args.file:
        src = Path(args.file).expanduser()
        media_id = f"sim-{uuid.uuid4().hex[:10]}{src.suffix}"
        inbox = Path(s.file_storage_dir) / "_sim_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, inbox / media_id)
        media = {"id": media_id, "mime_type": mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                 "filename": src.name}

    msg_id = args.msg_id or f"wamid.SIM{uuid.uuid4().hex}"
    body = json.dumps(build_payload(args.sender, args.text, msg_id, media)).encode()
    sig = "sha256=deadbeef" if args.bad_signature else compute_signature(secret, body)
    started = datetime.now(UTC)
    r = httpx.post(f"{args.url}/webhooks/whatsapp", content=body,
                   headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig}, timeout=10)
    print(f"→ {args.sender}: {args.text!r}  [{msg_id}]")
    print(f"  webhook: HTTP {r.status_code} {r.text}")
    if r.status_code != 200 or args.wait <= 0:
        return

    seen: set = set()
    deadline = time.time() + args.wait
    quiet_since = None
    while time.time() < deadline:
        with session_scope() as db:
            rows = db.execute(
                select(Message).where(Message.direction == "outbound", Message.created_at >= started)
                .order_by(Message.created_at)
            ).scalars().all()
            new = [m for m in rows if m.id not in seen and m.status not in ("queued",)]
            for m in new:
                seen.add(m.id)
                hold = (m.raw_payload or {}).get("hold_reason")
                tag = f" (held: {hold})" if hold else ""
                print(f"\n← AI Manager [{m.status}{tag}]:\n{m.body}")
        if new:
            quiet_since = time.time()
        elif quiet_since and time.time() - quiet_since > 5:
            return
        time.sleep(1)
    if not seen:
        print("  (no reply yet — is the worker running?)")


if __name__ == "__main__":
    main()
