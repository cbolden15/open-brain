"""The engine exposes source_freshness beside followups; brief grants work; 23/16/18 holds."""

from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, local_schema
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.source_freshness import FreshnessReport, SourceFreshnessTasks
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.portable.v18_catalog import PORTABLE_V18_SCHEMA_CATALOG

import open_brain.services.plugin_bridge as bridge
from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session
from open_brain.services.brief_contracts import BRIEF_GRANT_ACK, BRIEF_GRANT_READ
from open_brain.services.catalog import build_catalog, cli_registrations
from open_brain.services.local_entrypoints import _parser
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS
from open_brain.services.t03_adapters import agent_authority, owner_authority
from packages.app.tests.unit.engine.test_followups import FROZEN, Clock


def _engine(tmp_path: Path) -> BrainEngine:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    return BrainEngine.open(profile, clock=Clock(FROZEN))


def test_engine_exposes_source_freshness_tasks(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert isinstance(engine.tasks.source_freshness, SourceFreshnessTasks)
    assert engine.tasks.source_freshness is engine.source_freshness
    assert engine.tasks.followups is engine.followups  # neighbour unchanged
    owner = owner_authority(engine.tasks, session_id="owner-test")
    report = engine.tasks.source_freshness.freshness(owner, space_ids=None, stale_days=7)
    assert isinstance(report, FreshnessReport)
    assert report.families == ()


def test_brief_grants_are_valid_capabilities_and_gate_the_task(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    granted = agent_authority(
        principal_id="agent-1",
        session_id="s",
        grants=frozenset({BRIEF_GRANT_READ, BRIEF_GRANT_ACK}),
    )
    assert isinstance(granted, EffectiveAuthority)
    granted.require(BRIEF_GRANT_READ)
    granted.require(BRIEF_GRANT_ACK)
    report = engine.tasks.source_freshness.freshness(granted, space_ids=None, stale_days=7)
    assert report.families == ()
    ungranted = agent_authority(
        principal_id="agent-1", session_id="s", grants=frozenset({"search"})
    )
    with pytest.raises(T03Error) as refused:
        engine.tasks.source_freshness.freshness(ungranted, space_ids=None, stale_days=7)
    assert refused.value.code == "unsupported_capability"


def test_coordinates_stay_23_16_18() -> None:
    """The brief adds no schema, Portable or runtime-session change (spec, Compatibility)."""
    assert local_schema.PHASE1_STATE_SCHEMA_VERSION == 23
    assert len(LOCAL_MIGRATIONS) == 23
    assert local_runtime_session.RUNTIME_SESSION_VERSION == 16
    assert PORTABLE_V18_SCHEMA_CATALOG["state_schema_version"] == 23
    # Same call `_catalog()` makes in packages/app/tests/unit/test_catalog.py:34-49.
    catalog = build_catalog(
        {"schema_version": 2},
        cli_commands=cli_registrations(_parser()),
        mcp_registered=MCP_REGISTERED_TOOLS,
        mcp_authorized=None,
        mcp_discovery_context="test",
        bridge_base=bridge._BASE_OPERATIONS,
        bridge_negotiated=bridge._NEGOTIATED_OPERATIONS,
        bridge_optional=bridge._COLLECTOR_OPERATIONS,
        bridge_available=None,
        base_executable=Path("/missing/synthetic/open-brain"),
        platform_name="darwin",
        machine="arm64",
    )
    compatibility = catalog["compatibility"]
    assert compatibility == {
        **compatibility,  # type: ignore[dict-item]
        "state_schema": 23,
        "runtime_session": 16,
        "portable_metadata": 18,
    }
