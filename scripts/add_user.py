"""Add (or update) someone who can use the manager on WhatsApp.

    uv run python scripts/add_user.py "Name" +15555550100 --role owner [--set-pin]

Roles: owner (everything, including approvals, with their own PIN), manager (act, but never
approve), viewer (read only). An owner also receives every alert, approval and daily summary.

The PIN is typed in hidden and stored hashed — run it with --set-pin as the person themselves, so
nobody else sees it. Anyone not listed here is refused at the webhook and the attempt is logged.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import User  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.services import audit, users  # noqa: E402
from app.services.common import SYSTEM  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Add or update a WhatsApp user")
    parser.add_argument("name")
    parser.add_argument("number", help="WhatsApp number, e.g. +15555550100")
    parser.add_argument("--role", default="manager", choices=["owner", "manager", "viewer"])
    parser.add_argument("--email", default=None)
    parser.add_argument("--set-pin", action="store_true", help="set this person's approval PIN now")
    parser.add_argument("--deactivate", action="store_true", help="switch them off instead")
    args = parser.parse_args()

    number = users.normalize_number(args.number)
    with session_scope() as s:
        user = s.execute(select(User).where(User.whatsapp_number == number)).scalar_one_or_none()
        if user is None:
            user = User(name=args.name, whatsapp_number=number, email=args.email, role=args.role,
                        is_active=not args.deactivate)
            s.add(user)
            what = "added"
        else:
            user.name, user.role = args.name, args.role
            user.email = args.email or user.email
            user.is_active = not args.deactivate
            what = "updated"
        s.flush()

        if args.set_pin:
            pin = getpass.getpass(f"Approval PIN for {user.name} (4-12 digits, hidden): ")
            if not (pin.isdigit() and 4 <= len(pin) <= 12):
                sys.exit("PIN must be 4-12 digits")
            if pin != getpass.getpass("Repeat it: "):
                sys.exit("PINs do not match")
            user.approval_pin_hash = users.hash_pin(pin)
            print("PIN saved (hashed — it is not stored or printed anywhere).")

        audit.record(s, SYSTEM, f"user_{what}", target_type="user", target_id=user.id,
                     details={"role": user.role, "active": user.is_active})
        print(f"{what.title()}: {user.name} {user.whatsapp_number} — {user.role}"
              + ("" if user.is_active else " (inactive)")
              + ("" if user.approval_pin_hash else "  ⚠️ no PIN yet: they can't approve anything"))


if __name__ == "__main__":
    main()
