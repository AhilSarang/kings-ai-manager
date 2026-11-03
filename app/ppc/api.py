"""One place to get a configured Ads client with call logging."""

from __future__ import annotations

import logging

from app.ppc.client import AdsClient, CallRecord
from app.ppc.factory import get_client as _get_client

log = logging.getLogger(__name__)


def _record(call: CallRecord) -> None:
    """Log the call in its own transaction so it survives whatever the caller does.

    The `ppc_api_calls` table arrives with M2; until then this quietly does nothing.
    """
    try:
        from app.db.models import PpcApiCall
    except ImportError:
        return
    from app.db.session import session_scope

    try:
        with session_scope() as s:
            s.add(PpcApiCall(**call))
            s.commit()
    except Exception:
        log.warning("could not store ads api call", exc_info=True)


def client() -> AdsClient:
    return _get_client(recorder=_record)
