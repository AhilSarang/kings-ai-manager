"""Queue an urgent WhatsApp message to the owner (used by server alerts).

Usage: python scripts/notify_owner.py "text"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import session_scope  # noqa: E402
from app.services.notify import notify_owner  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.exit('usage: notify_owner.py "text"')
    with session_scope() as s:
        notify_owner(s, sys.argv[1].strip(), urgent=True, respect_quiet_hours=False)
    print("queued")


if __name__ == "__main__":
    main()
