"""One session authority for every brief read, and the visibility key it derives.

``brief_session_authority`` applies the follow-up clamp first (Work-only, non-owner, unless a
launcher policy governs) and then records which brief and follow-up grants the session holds. Every
provider the composer runs reads under ``BriefSessionAuthority.authority``; no provider ever runs
under owner authority for a non-owner session. ``visibility_key`` names the log file two sessions
share only when every scope input is equal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256

from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.services.brief_contracts import BRIEF_GRANTS
from open_brain.services.followups import FOLLOWUP_GRANTS, followup_session_authority


@dataclass(frozen=True)
class BriefSessionAuthority:
    authority: EffectiveAuthority
    brief_grants: frozenset[str]
    followup_grants: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority, EffectiveAuthority)
            or type(self.brief_grants) is not frozenset
            or type(self.followup_grants) is not frozenset
            or not self.brief_grants <= BRIEF_GRANTS
            or not self.followup_grants <= FOLLOWUP_GRANTS
        ):
            raise ValueError("invalid brief session authority")


def brief_session_authority(
    authority: EffectiveAuthority,
    *,
    grants: frozenset[str],
    launcher_policy: bool,
    owner_cli: bool = False,
) -> BriefSessionAuthority:
    """Clamp like follow-ups, then record the brief and follow-up grants the session holds.

    Without a launcher policy the session runs as a non-owner Work-only authority holding the
    selected brief and follow-up grants (``followup_session_authority`` adds the follow-up ones;
    this function adds the brief ones so ``require("brief-read")`` can pass). With a policy the
    authority already carries the policy intersection and is returned unchanged. The owner CLI
    passes ``owner_cli=True`` with owner authority and holds every grant; it never passes through
    the clamp.
    """
    if owner_cli:
        if not authority.owner:
            raise ValueError("owner CLI brief authority requires owner authority")
        return BriefSessionAuthority(authority, BRIEF_GRANTS, FOLLOWUP_GRANTS)
    clamped = followup_session_authority(
        authority, grants=grants & FOLLOWUP_GRANTS, launcher_policy=launcher_policy
    )
    if not launcher_policy:
        clamped = replace(clamped, capabilities=clamped.capabilities | (grants & BRIEF_GRANTS))
    return BriefSessionAuthority(
        clamped,
        frozenset(BRIEF_GRANTS & clamped.capabilities),
        frozenset(FOLLOWUP_GRANTS & clamped.capabilities),
    )


def visibility_preimage(
    session: BriefSessionAuthority, *, selected_space_ids: tuple[str, ...] | None
) -> bytes:
    """Canonical JSON over every input that decides what a brief may contain."""
    authority = session.authority
    preimage = {
        "owner": authority.owner,
        "allowed_read_tiers": sorted(tier.value for tier in authority.allowed_read_tiers),
        "space_ids": "all" if authority.space_ids is None else sorted(authority.space_ids),
        "egress_mode": authority.egress_mode.value,
        "provider_id": authority.provider_id,
        "brief_grants": sorted(session.brief_grants),
        "followup_grants": sorted(session.followup_grants),
        "selected": "all" if selected_space_ids is None else sorted(set(selected_space_ids)),
    }
    return json.dumps(preimage, sort_keys=True, separators=(",", ":")).encode("utf-8")


def visibility_key(
    session: BriefSessionAuthority, *, selected_space_ids: tuple[str, ...] | None
) -> str:
    return sha256(visibility_preimage(session, selected_space_ids=selected_space_ids)).hexdigest()


__all__ = [
    "BriefSessionAuthority",
    "brief_session_authority",
    "visibility_key",
    "visibility_preimage",
]
