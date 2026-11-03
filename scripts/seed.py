"""Seed/refresh the owner user from env. Agents and settings are seeded by migrations.

Usage: uv run python scripts/seed.py [--set-pin]
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import session_scope  # noqa: E402
from app.services import audit, users  # noqa: E402
from app.services.common import SYSTEM  # noqa: E402


def main() -> None:
    with session_scope() as s:
        owner = users.ensure_owner(s)
        print(f"Owner: {owner.name} {owner.whatsapp_number}")
        if "--set-pin" in sys.argv:
            pin = getpass.getpass("New approval PIN (4-12 digits): ")
            if not (pin.isdigit() and 4 <= len(pin) <= 12):
                sys.exit("PIN must be 4-12 digits")
            if pin != getpass.getpass("Repeat PIN: "):
                sys.exit("PINs do not match")
            owner.approval_pin_hash = users.hash_pin(pin)
            audit.record(s, SYSTEM, "approval_pin_set", target_type="user", target_id=owner.id)
            print("PIN saved (hashed).")


if __name__ == "__main__":
    main()
