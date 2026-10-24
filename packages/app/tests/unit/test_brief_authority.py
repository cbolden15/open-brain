"""One clamped authority per brief session, and a visibility key that separates every scope."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.services.brief_authority import (
    BriefSessionAuthority,
    brief_session_authority,
    visibility_key,
    visibility_preimage,
)
from open_brain.services.brief_contracts import BRIEF_GRANTS
from open_brain.services.followups import FOLLOWUP_GRANTS
from open_brain.services.t03_adapters import agent_authority, owner_authority_for_principal

SPACE_A = "space_455d0d63-a720-4b09-b15d-aa741c598abd"
SPACE_B = "space_d6c9f397-902f-4e73-a49a-b3a7dbb0ebdc"


def _owner() -> EffectiveAuthority:
    return owner_authority_for_principal(principal_id="owner-1", session_id="mcp-1")


def _agent(*grants: str) -> EffectiveAuthority:
    return agent_authority(principal_id="agent-1", session_id="mcp-2", grants=frozenset(grants))


def _external(*grants: str) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic-external-principal",
        "synthetic-external-session",
        frozenset(grants),
        None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
        egress_mode=EgressMode.EXTERNAL_PROVIDER,
        provider_id="synthetic-provider",
        consent_id="consent_" + "0" * 32,
        brain_id="brn_" + "a" * 26,
        issuer_epoch=1,
    )


def _session(
    authority: EffectiveAuthority, *grants: str, launcher_policy: bool = False
) -> BriefSessionAuthority:
    return brief_session_authority(
        authority, grants=frozenset(grants), launcher_policy=launcher_policy
    )


def test_unscoped_session_is_clamped_to_work_and_holds_only_selected_grants() -> None:
    session = _session(_owner(), "brief-read", "followup-read", "followup-decide")
    clamped = session.authority
    assert clamped.owner is False
    assert clamped.allowed_read_tiers == frozenset({PrivacyTier.WORK})
    assert (clamped.principal_id, clamped.session_id) == ("owner-1", "mcp-1")
    assert {"brief-read", "followup-read", "followup-decide"} <= clamped.capabilities
    assert "brief-ack" not in clamped.capabilities
    assert "followup-propose" not in clamped.capabilities
    clamped.require("brief-read")
    with pytest.raises(T03Error) as refused:
        clamped.require("brief-ack")
    assert refused.value.code == "unsupported_capability"
    assert session.brief_grants == frozenset({"brief-read"})
    assert session.followup_grants == frozenset({"followup-read", "followup-decide"})


def test_session_without_brief_grants_records_none_and_cannot_read_freshness() -> None:
    session = _session(_agent("search"), "followup-read")
    assert session.brief_grants == frozenset()
    assert session.followup_grants == frozenset({"followup-read"})
    with pytest.raises(T03Error) as refused:
        session.authority.require("brief-read")
    assert refused.value.code == "unsupported_capability"


def test_launcher_policy_authority_is_returned_unchanged() -> None:
    policy_bound = replace(
        _agent("search", "brief-read", "followup-read"),
        allowed_read_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        space_ids=frozenset({SPACE_A}),
    )
    session = _session(policy_bound, "brief-read", "brief-ack", launcher_policy=True)
    assert session.authority is policy_bound
    assert session.brief_grants == frozenset({"brief-read"})
    assert session.followup_grants == frozenset({"followup-read"})


def test_owner_cli_path_keeps_owner_authority_and_holds_every_grant() -> None:
    owner = _owner()
    session = brief_session_authority(
        owner, grants=frozenset(), launcher_policy=False, owner_cli=True
    )
    assert session.authority is owner
    assert session.authority.owner is True
    assert session.brief_grants == BRIEF_GRANTS
    assert session.followup_grants == FOLLOWUP_GRANTS
    with pytest.raises(ValueError):
        brief_session_authority(_agent(), grants=frozenset(), launcher_policy=False, owner_cli=True)


def test_session_authority_rejects_foreign_grants() -> None:
    with pytest.raises(ValueError):
        BriefSessionAuthority(_agent(), frozenset({"search"}), frozenset())
    with pytest.raises(ValueError):
        BriefSessionAuthority(_agent(), frozenset(), frozenset({"brief-read"}))


def test_visibility_preimage_is_canonical_json() -> None:
    session = _session(_owner(), "brief-read", "followup-read")
    preimage = visibility_preimage(session, selected_space_ids=(SPACE_B, SPACE_A, SPACE_B))
    expected = {
        "allowed_read_tiers": ["work"],
        "brief_grants": ["brief-read"],
        "egress_mode": "owner_local",
        "followup_grants": ["followup-read"],
        "owner": False,
        "provider_id": None,
        "selected": [SPACE_A, SPACE_B],
        "space_ids": "all",
    }
    assert preimage == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert (
        visibility_key(session, selected_space_ids=(SPACE_B, SPACE_A, SPACE_B))
        == sha256(preimage).hexdigest()
    )
    assert (
        visibility_key(session, selected_space_ids=None)
        == sha256(
            json.dumps(
                {**expected, "selected": "all"}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


def test_identical_sessions_share_a_key_and_every_scope_difference_separates() -> None:
    base = _session(_owner(), "brief-read", "followup-read")
    same = _session(
        owner_authority_for_principal(principal_id="owner-2", session_id="mcp-9"),
        "brief-read",
        "followup-read",
    )
    assert visibility_key(base, selected_space_ids=None) == visibility_key(
        same, selected_space_ids=None
    )
    owner_cli = brief_session_authority(
        _owner(), grants=frozenset(), launcher_policy=False, owner_cli=True
    )
    tiers = _session(
        replace(
            _agent("brief-read", "followup-read"),
            allowed_read_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        ),
        "brief-read",
        "brief-ack",
        launcher_policy=True,
    )
    tiers_same_grants = BriefSessionAuthority(
        tiers.authority, base.brief_grants, base.followup_grants
    )
    egress = BriefSessionAuthority(
        _external("brief-read", "followup-read"), base.brief_grants, base.followup_grants
    )
    provider = BriefSessionAuthority(
        replace(egress.authority, provider_id="other-provider"),
        base.brief_grants,
        base.followup_grants,
    )
    brief_grant = _session(_owner(), "brief-read", "brief-ack", "followup-read")
    followup_grant = _session(_owner(), "brief-read")
    spaces = _session(
        replace(
            _agent("brief-read", "followup-read"),
            allowed_read_tiers=frozenset({PrivacyTier.WORK}),
            space_ids=frozenset({SPACE_A}),
        ),
        "brief-read",
        launcher_policy=True,
    )
    keys = {
        "base": visibility_key(base, selected_space_ids=None),
        "owner": visibility_key(owner_cli, selected_space_ids=None),
        "tiers": visibility_key(tiers_same_grants, selected_space_ids=None),
        "egress": visibility_key(egress, selected_space_ids=None),
        "provider": visibility_key(provider, selected_space_ids=None),
        "brief_grant": visibility_key(brief_grant, selected_space_ids=None),
        "followup_grant": visibility_key(followup_grant, selected_space_ids=None),
        "spaces": visibility_key(spaces, selected_space_ids=None),
        "selected": visibility_key(base, selected_space_ids=(SPACE_A,)),
        "selected_other": visibility_key(base, selected_space_ids=(SPACE_B,)),
    }
    assert len(set(keys.values())) == len(keys)
    assert all(len(key) == 64 and int(key, 16) >= 0 for key in keys.values())
