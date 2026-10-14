"""Real facet transports retain current session, egress, and historical eligibility."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.consent_contracts import EgressMode
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from open_brain.services.facets import FacetAppAdapter
from open_brain.services.launcher_policy import LauncherPolicyError
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.local_operations import current_brain_identity
from open_brain.services.mcp_protocol import McpCallError
from open_brain.services.session_authority import TrustedSessionAuthority
from open_brain.services.session_consent import DurableProviderConsentStore
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition
from packages.app.tests.unit.engine.test_paging import capture_work, external_authority
from packages.engine.tests.contract.test_facets_acceptance import FacetHarness

CONSENT_ID = "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
READ_GRANTS = frozenset({"facet-read", "search", "history-read", "facet-assign"})


def _mcp(
    engine: BrainEngine,
    authority: EffectiveAuthority,
    source: TrustedSessionAuthority | None = None,
) -> LocalMcpAdapter:
    revalidate = None if source is None else lambda: source.revalidate(authority)
    return LocalMcpAdapter(
        authority=authority,
        revalidate_authority=revalidate,
        facets=FacetAppAdapter(
            engine.facets,
            authority,
            publication=engine.facet_publication,
            granted_capabilities=authority.capabilities,
            revalidate_authority=revalidate,
        ),
    )


def _rows(engine: BrainEngine) -> tuple[tuple[Any, ...], ...]:
    with sqlite3.connect(engine.profile.root / ".open-brain/state/phase1.sqlite3") as connection:
        return tuple(connection.execute("SELECT * FROM facet_operations ORDER BY operation_id"))


def _policy(
    path: Path,
    engine: BrainEngine,
    grants: frozenset[str],
    *,
    external: bool = False,
) -> None:
    brain_id, issuer_epoch = current_brain_identity(engine.tasks)
    path.write_text(
        json.dumps(
            {
                "policy_version": "launcher-policy.v1",
                "principal_id": "synthetic-facet-agent",
                "session_id": "synthetic-facet-session",
                "capabilities": sorted(grants),
                "space_ids": None,
                "allowed_read_tiers": ["work"],
                "allowed_capture_tiers": [],
                "egress_mode": "external_provider" if external else "owner_local",
                "provider_id": "synthetic-provider" if external else None,
                "consent_id": CONSENT_ID if external else None,
                "authorization_generation": 1 if external else 0,
                "brain_id": brain_id,
                "issuer_epoch": issuer_epoch,
            }
        )
    )


def _authority_source(
    tmp_path: Path, engine: BrainEngine, grants: frozenset[str], *, external: bool = False
) -> tuple[TrustedSessionAuthority, Path, DurableProviderConsentStore | None]:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    policy_path = root / "session.json"
    _policy(policy_path, engine, grants, external=external)
    consent = None
    consent_path = None
    if external:
        consent_path = root / "consent.json"
        brain_id, issuer_epoch = current_brain_identity(engine.tasks)
        consent = DurableProviderConsentStore(
            consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch
        )
        consent.grant(
            provider_id="synthetic-provider",
            allowed_tiers=frozenset({PrivacyTier.WORK}),
            operation_id="synthetic-grant",
            decided_at="2026-10-13T12:00:00Z",
            consent_id_factory=lambda: CONSENT_ID,
        )
    return (
        TrustedSessionAuthority(
            engine.tasks, policy_path=policy_path, consent_state_path=consent_path
        ),
        policy_path,
        consent,
    )


def test_facet_mcp_rejects_different_authority_objects(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    authority = EffectiveAuthority("synthetic", "session", READ_GRANTS, None)
    with pytest.raises(ValueError, match="invalid MCP facet authority"):
        LocalMcpAdapter(
            authority=authority,
            facets=FacetAppAdapter(engine.facets, replace(authority)),
        )
    assert _rows(engine) == ()


@pytest.mark.parametrize("external", [False, True])
def test_live_policy_or_consent_revocation_blocks_facet_discovery_and_writes(
    tmp_path: Path, external: bool
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    grants = frozenset({"facet-catalog-write"})
    source, policy_path, consent = _authority_source(tmp_path, engine, grants, external=external)
    adapter = _mcp(engine, source.load(), source)
    request = {
        "dto_version": 1,
        "name": "Synthetic project",
        "expected_version": 0,
        "operation_id": "synthetic-create",
    }
    receipt = adapter.call_tool("brain_facet_dimension_create", request)
    assert receipt["status"] == "applied"
    before = _rows(engine)
    assert len(before) == 1
    if consent is None:
        _policy(policy_path, engine, frozenset())
    else:
        consent.revoke(
            consent_id=CONSENT_ID,
            operation_id="synthetic-revoke",
            decided_at="2026-10-13T12:01:00Z",
        )
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        adapter.list_tools()
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        adapter.call_tool("brain_facet_dimension_create", request | {"operation_id": "denied"})
    assert _rows(engine) == before


def test_real_cli_intersects_selected_facet_flags_with_trusted_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    _, policy_path, _ = _authority_source(tmp_path, engine, frozenset({"facet-read"}))
    observed = []

    def serve(adapter: LocalMcpAdapter, **_kwargs: object) -> None:
        observed.append(adapter)
        assert adapter.facets is not None
        assert adapter.facets.authority is adapter.authority
        assert adapter.authority.capabilities == frozenset({"facet-read"})
        names = {tool["name"] for tool in adapter.list_tools()}
        assert "brain_facet_dimension_list" in names
        assert "brain_facet_dimension_create" not in names
        with pytest.raises(McpCallError):
            adapter.call_tool("brain_facet_dimension_create", {"owner": True})

    monkeypatch.setattr("open_brain.services.mcp_protocol.serve_stdio_mcp", serve)
    assert (
        run_cli(
            (
                "mcp",
                "--data-dir",
                str(engine.profile.root),
                "--session-policy",
                str(policy_path),
                "--allow-facet-read",
                "--allow-facet-catalog-write",
            ),
            filesystem_type_probe=lambda _p, platform: "apfs" if platform == "darwin" else "ext4",
        )
        == 0
    )
    assert len(observed) == 1
    assert _rows(engine) == ()


def test_durable_consent_revoked_during_real_handler_blocks_response_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    source, _, consent = _authority_source(
        tmp_path, engine, frozenset({"facet-catalog-write"}), external=True
    )
    assert consent is not None
    adapter = _mcp(engine, source.load(), source)
    task_type = type(engine.facets)
    original = task_type.dimension_create

    def revoke_after_real_handler(self: Any, request: Any, *, authority: EffectiveAuthority) -> Any:
        response = original(self, request, authority=authority)
        consent.revoke(
            consent_id=CONSENT_ID,
            operation_id="synthetic-handler-revoke",
            decided_at="2026-10-13T12:01:00Z",
        )
        return response

    monkeypatch.setattr(task_type, "dimension_create", revoke_after_real_handler)
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        adapter.call_tool(
            "brain_facet_dimension_create",
            {
                "dto_version": 1,
                "name": "Synthetic completed mutation",
                "expected_version": 0,
                "operation_id": "synthetic-handler-mutation",
            },
        )
    # The real handler completed. Revocation suppresses output rather than
    # inventing a rollback of a mutation which already committed.
    assert len(_rows(engine)) == 1


def test_external_facet_query_assignment_and_history_intersect_stored_egress(
    tmp_path: Path,
) -> None:
    brain = FacetHarness(tmp_path)
    value = brain.value(brain.dimension())
    visible = capture_work(
        brain.engine, delivery_id="facet.visible", text="Synthetic visible", external_egress=True
    )
    denied = capture_work(
        brain.engine, delivery_id="facet.denied", text="Synthetic denied", external_egress=False
    )
    for record in (visible, denied):
        brain.patch(record, add=(value,))
    external = replace(external_authority(brain.engine), capabilities=READ_GRANTS)
    adapter = _mcp(brain.engine, external)
    selector = {"dto_version": 1, "selector": {"all_value_ids": [value]}}
    result = adapter.call_tool("brain_facet_query_page", selector)
    assert [
        item["record"]["record_id"] for item in cast(list[dict[str, Any]], result["items"])
    ] == [visible]
    before = _rows(brain.engine)
    with pytest.raises(McpCallError, match="not_found"):
        adapter.call_tool("brain_facet_assignment_get", {"dto_version": 1, "record_id": denied})
    with pytest.raises(McpCallError, match="not_found"):
        adapter.call_tool(
            "brain_facet_assignment_patch",
            {
                "dto_version": 1,
                "record_id": denied,
                "expected_revision_id": denied,
                "expected_version": 1,
                "add_value_ids": [],
                "remove_value_ids": [value],
                "operation_id": "egress-denied-patch",
            },
        )
    subject = brain.call("facet.assignment.get", record_id=denied)["item"]["subject"]
    with pytest.raises(McpCallError, match="not_found"):
        adapter.call_tool("brain_facet_history_list", {"dto_version": 1, "subject": subject})
    local = _mcp(
        brain.engine,
        replace(external, egress_mode=EgressMode.OWNER_LOCAL, provider_id=None, consent_id=None),
    )
    assert {
        item["record"]["record_id"]
        for item in cast(
            list[dict[str, Any]], local.call_tool("brain_facet_query_page", selector)["items"]
        )
    } == {visible, denied}
    assert _rows(brain.engine) == before


def test_pending_historical_fence_denies_facet_candidates_assignment_and_history(
    tmp_path: Path,
) -> None:
    brain = FacetHarness(tmp_path)
    transition = _claim_transition(brain.engine)
    record = brain.capture()
    value = brain.value(brain.dimension())
    brain.patch(record, add=(value,))
    subject = brain.call("facet.assignment.get", record_id=record)["item"]["subject"]
    authority = EffectiveAuthority("synthetic-agent", "session", READ_GRANTS, None)
    adapter = _mcp(brain.engine, authority)
    selector = {"dto_version": 1, "selector": {"all_value_ids": [value]}}
    assert (
        len(cast(list[object], adapter.call_tool("brain_facet_query_page", selector)["items"])) == 1
    )
    before = _rows(brain.engine)
    HistoricalPendingFence(brain.root, brain.profile.root_identity).prepare(transition)
    assert adapter.call_tool("brain_facet_query_page", selector)["items"] == []
    with pytest.raises(McpCallError, match="not_found"):
        adapter.call_tool("brain_facet_assignment_get", {"dto_version": 1, "record_id": record})
    # Pending historical authority blocks ordinary writes before subject lookup.
    with pytest.raises(McpCallError, match="operation_pending"):
        adapter.call_tool(
            "brain_facet_assignment_patch",
            {
                "dto_version": 1,
                "record_id": record,
                "expected_revision_id": record,
                "expected_version": 1,
                "add_value_ids": [],
                "remove_value_ids": [value],
                "operation_id": "historical-denied-patch",
            },
        )
    with pytest.raises(McpCallError, match="not_found"):
        adapter.call_tool("brain_facet_history_list", {"dto_version": 1, "subject": subject})
    owner = _mcp(brain.engine, replace(authority, owner=True))
    assert owner.call_tool(
        "brain_facet_history_list",
        {
            "dto_version": 1,
            "subject": subject,
        },
    )["items"]
    assert _rows(brain.engine) == before
