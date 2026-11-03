"""One place to get a configured SP-API client with call logging."""

from __future__ import annotations

import logging

from app.amazon.client import AmazonClient, CallRecord
from app.amazon.factory import get_client as _get_client

log = logging.getLogger(__name__)


def _record(call: CallRecord) -> None:
    """Log the call in its own transaction so it survives whatever the caller does."""
    from app.db.models import AmazonApiCall
    from app.db.session import session_scope

    try:
        with session_scope() as s:
            s.add(AmazonApiCall(**{k: v for k, v in call.items() if k != "operation_id"},
                                operation_id=call.get("operation_id")))
            s.commit()
    except Exception:
        log.warning("could not store amazon api call", exc_info=True)


def client() -> AmazonClient:
    return _get_client(recorder=_record)
