"""Permission tiers (Section 9)."""

from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    READ = 0          # read / analyse
    INTERNAL = 1      # create/update tasks, drafts, files
    EXTERNAL = 2      # external communication: approval required
    FINANCIAL = 3     # money/account-affecting: approval required, risk >= high
    PROHIBITED = 4    # never executable by agents


def requires_approval(tier: Tier) -> bool:
    return tier in (Tier.EXTERNAL, Tier.FINANCIAL)
