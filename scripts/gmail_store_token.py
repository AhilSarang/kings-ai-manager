"""Store a Gmail refresh token read from stdin (called over SSH by gmail_authorize.py).

The token is encrypted with TOKEN_ENCRYPTION_KEY and never printed or logged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from app.email import accounts  # noqa: E402
from app.email.client import store_refresh_token  # noqa: E402
from app.jobs import queue  # noqa: E402
from app.services.common import utcnow  # noqa: E402


def store(session: Session, refresh_token: str) -> str:
    refresh_token = refresh_token.strip()
    if not refresh_token or len(refresh_token) < 20:
        raise SystemExit("No refresh token received")
    account = accounts.get_primary(session)
    store_refresh_token(session, account, refresh_token)
    stamp = int(utcnow().timestamp())
    queue.enqueue(session, "gmail_watch_renew", {"account_id": str(account.id)},
                  idempotency_key=f"gmail_watch:{account.id}:{stamp}", priority=30)
    queue.enqueue(session, "gmail_sync", {"account_id": str(account.id)},
                  idempotency_key=f"gmail_sync_auth:{account.id}:{stamp}", priority=30)
    return f"Stored an encrypted token for {account.address}; account is active."


def main() -> None:
    from app.db.session import session_scope

    token = sys.stdin.read()
    with session_scope() as s:
        print(store(s, token))


if __name__ == "__main__":
    main()
