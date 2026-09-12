"""No-delete state transitions for local ledger claims."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from .index import ClaimRecord, ClaimStatus


def age_claims(
    claims: tuple[ClaimRecord, ...],
    *,
    now: datetime,
    aging_after: timedelta,
    retire_after: timedelta,
) -> tuple[ClaimRecord, ...]:
    if (
        not isinstance(claims, tuple)
        or any(not isinstance(claim, ClaimRecord) for claim in claims)
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or not isinstance(aging_after, timedelta)
        or not isinstance(retire_after, timedelta)
        or aging_after <= timedelta(0)
        or retire_after <= aging_after
    ):
        raise ValueError("invalid claim aging input")
    aged: list[ClaimRecord] = []
    for claim in claims:
        claim.validate()
        elapsed = now - claim.last_reinforced_at
        status = (
            ClaimStatus.RETIRED
            if elapsed >= retire_after
            else ClaimStatus.AGING
            if elapsed >= aging_after
            else ClaimStatus.ACTIVE
        )
        value = replace(claim, status=status)
        value.validate()
        aged.append(value)
    return tuple(aged)
