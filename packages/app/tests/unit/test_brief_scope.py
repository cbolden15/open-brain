"""source_freshness runs under the clamped brief session authority, never under owner authority."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local
from open_brain.services.brief_authority import brief_session_authority, visibility_key
from open_brain.services.followups import followup_session_authority
from open_brain.services.t03_adapters import agent_authority, owner_authority
from packages.app.tests.unit.engine.test_followups import FROZEN, Clock
from packages.app.tests.unit.engine.test_source_freshness import (
    _import_vault,
    _markdown_heads,
    _namespace_source,
)


def _brain(tmp_path: Path) -> tuple[BrainEngine, str, str]:
    """A Brain with a Work family in Notes, a Personal family in Archive, Personal Markdown."""
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes", "Archive")),
        clock=Clock(FROZEN),
    )
    notes, archive = engine.inbox.spaces()
    work = _namespace_source(engine, "work", "Synthetic work session")
    home = _namespace_source(
        engine, "home", "Synthetic personal session", tier=PrivacyTier.PERSONAL
    )
    engine.inbox.route(work.head, notes.space_id, delivery_id="route.work")
    engine.inbox.route(home.head, archive.space_id, delivery_id="route.home")
    _import_vault(engine, tmp_path / "vault", {"a.md": "Synthetic a"}, tier=PrivacyTier.PERSONAL)
    assert _markdown_heads(engine)
    return engine, notes.space_id, archive.space_id


def _ids(
    engine: BrainEngine,
    authority: EffectiveAuthority,
    *,
    space_ids: frozenset[str] | None = None,
) -> set[str]:
    report = engine.tasks.source_freshness.freshness(authority, space_ids=space_ids, stale_days=7)
    return {family.family_id for family in report.families}


def test_work_only_mcp_session_never_sees_personal_families(tmp_path: Path) -> None:
    engine, notes, archive = _brain(tmp_path)
    owner = owner_authority(engine.tasks, session_id="mcp-1")
    session = brief_session_authority(
        owner, grants=frozenset({"brief-read", "followup-read"}), launcher_policy=False
    )
    assert session.authority.owner is False
    assert _ids(engine, session.authority) == {f"consumer:synthetic/work:{notes}"}
    owner_cli = brief_session_authority(
        owner, grants=frozenset(), launcher_policy=False, owner_cli=True
    )
    assert _ids(engine, owner_cli.authority) == {
        f"consumer:synthetic/work:{notes}",
        f"consumer:synthetic/home:{archive}",
        "markdown:unassigned",
    }
    assert visibility_key(session, selected_space_ids=None) != visibility_key(
        owner_cli, selected_space_ids=None
    )


def test_launcher_policy_space_and_tier_restrictions_are_enforced(tmp_path: Path) -> None:
    engine, notes, archive = _brain(tmp_path)
    policy_bound = replace(
        agent_authority(
            principal_id="agent-1", session_id="mcp-2", grants=frozenset({"brief-read"})
        ),
        allowed_read_tiers=frozenset({PrivacyTier.WORK, PrivacyTier.PERSONAL}),
        space_ids=frozenset({archive}),
    )
    session = brief_session_authority(
        policy_bound, grants=frozenset({"brief-read"}), launcher_policy=True
    )
    assert session.authority is policy_bound
    assert _ids(engine, session.authority) == {f"consumer:synthetic/home:{archive}"}
    # Selecting a space outside the policy yields nothing; selecting inside keeps it.
    assert _ids(engine, session.authority, space_ids=frozenset({notes})) == set()
    assert _ids(engine, session.authority, space_ids=frozenset({archive})) == {
        f"consumer:synthetic/home:{archive}"
    }
    # The owner path has no space restriction, so the explicit selection is the filter.
    owner_cli = brief_session_authority(
        owner_authority(engine.tasks, session_id="cli"),
        grants=frozenset(),
        launcher_policy=False,
        owner_cli=True,
    )
    assert _ids(engine, owner_cli.authority, space_ids=frozenset({notes})) == {
        f"consumer:synthetic/work:{notes}"
    }


def test_session_without_brief_read_is_refused(tmp_path: Path) -> None:
    engine, _notes, _archive = _brain(tmp_path)
    owner = owner_authority(engine.tasks, session_id="mcp-3")
    without = brief_session_authority(
        owner, grants=frozenset({"followup-read"}), launcher_policy=False
    )
    assert without.brief_grants == frozenset()
    with pytest.raises(T03Error) as refused:
        engine.tasks.source_freshness.freshness(without.authority, space_ids=None, stale_days=7)
    assert refused.value.code == "unsupported_capability"
    # The follow-up clamp alone never confers brief-read, so Package 2 must route through
    # brief_session_authority rather than reuse the follow-up callbacks' authority.
    clamped_only = followup_session_authority(
        owner, grants=frozenset({"followup-read"}), launcher_policy=False
    )
    with pytest.raises(T03Error) as refused:
        engine.tasks.source_freshness.freshness(clamped_only, space_ids=None, stale_days=7)
    assert refused.value.code == "unsupported_capability"
