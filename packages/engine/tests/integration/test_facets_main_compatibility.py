"""Facet selection retains main's withdrawal and historical authority boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine.facet_contracts import FacetError, ProposeFromFacetQueryRequest
from open_brain_engine.engine.historical_contracts import HistoricalDestination
from open_brain_engine.engine.historical_contracts_v2 import HistoricalClaimRequestV2
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.historical_tasks import register_historical_claim
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspectRequest,
    SourceWithdrawRequest,
)
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
)

from packages.app.tests.unit.engine._historical_compatibility_fixtures import evidence_v2
from packages.app.tests.unit.engine.test_historical_tasks import _current_cas
from packages.engine.tests.contract.test_facets_acceptance import FacetHarness
from packages.engine.tests.integration.test_facet_publication import LiveFacetBrain


def _reader(brain: FacetHarness) -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id="synthetic-facet-reader",
        session_id="synthetic-facet-reader",
        capabilities=frozenset({"facet-read", "search", "history-read"}),
        space_ids=None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )


def _withdraw(engine: BrainEngine, capture_id: str, owner: EffectiveAuthority) -> None:
    with engine._store.connect() as connection:
        source = connection.execute(
            "SELECT source_id FROM source_revisions WHERE capture_id=?", (capture_id,)
        ).fetchone()[0]
    inspected = engine.sources.inspect(SourceInspectRequest(source_id=source), authority=owner)
    engine.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic.facet",
            source_id=source,
            expected_head=capture_id,
            expected_lifecycle_version=inspected.lifecycle_version,
            brain_id=inspected.destination_brain_id,
            issuer_epoch=inspected.issuer_epoch,
            reason_code="synthetic_owner_choice",
        ),
        authority=owner,
    )


def _selected(brain: FacetHarness, value: str, authority: EffectiveAuthority) -> set[str]:
    return {
        item["record"]["record_id"]
        for item in brain.call(
            "facet.query.page", selector={"all_value_ids": [value]}, authority=authority
        )["items"]
    }


def test_withdrawal_removes_facet_selection_and_cursors_but_retains_owner_history(
    tmp_path: Path,
) -> None:
    brain = FacetHarness(tmp_path)
    value = brain.value(brain.dimension())
    target, control = brain.capture("Synthetic target"), brain.capture("Synthetic control")
    for capture in (target, control):
        brain.patch(capture, add=(value,))
    reader = _reader(brain)
    cursor = brain.call(
        "facet.query.page", selector={"all_value_ids": [value]}, limit=1, authority=reader
    )["next_cursor"]
    assert cursor is not None
    for authority in (brain.authority, reader):
        assert _selected(brain, value, authority) == {target, control}
    retained = {
        path: path.read_bytes()
        for path in (brain.root / "sources" / "captures").rglob("*.json")
    }
    _withdraw(brain.engine, target, brain.authority)
    for authority in (brain.authority, reader):
        assert _selected(brain, value, authority) == {control}
        with pytest.raises(FacetError, match="^not_found$"):
            brain.call("facet.assignment.get", record_id=target, authority=authority)
    with pytest.raises(FacetError, match="^cursor_stale$"):
        brain.call(
            "facet.query.page", selector={"all_value_ids": [value]}, limit=1,
            cursor=cursor, authority=reader,
        )
    history = brain.engine.history.read_history(
        RecordReadRequest(record_id=target, expected_revision_id=target),
        authority=brain.authority,
    ).to_wire()
    assert cast(dict[str, Any], history["content"])["text"] == "Synthetic target"
    assert retained and {path: path.read_bytes() for path in retained} == retained
    brain.engine = BrainEngine.open(brain.profile)
    assert _selected(brain, value, reader) == {control}


def test_v2_denied_historical_claim_is_not_ordinary_facet_permission(tmp_path: Path) -> None:
    brain = FacetHarness(tmp_path)
    value = brain.value(brain.dimension())
    target, control = brain.capture("Synthetic claimed"), brain.capture("Synthetic ordinary")
    for capture in (target, control):
        brain.patch(capture, add=(value,))
    reader = _reader(brain)
    assert _selected(brain, value, reader) == {target, control}
    with brain.engine._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        source = connection.execute(
            "SELECT source_id FROM source_revisions WHERE capture_id=?", (target,)
        ).fetchone()[0]
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    registry = HistoricalRegistryStore(brain.root, brain.profile.root_identity).read(destination)
    cas = _current_cas(brain.engine, source)
    request = HistoricalClaimRequestV2(
        operation_id="claim.synthetic.facet",
        destination=destination,
        source_cas=cas,
        capture_source_cas=cas,
        retained_capture=evidence_v2(brain.engine, target),
        claim_role="baseline_original",
        expected_claim_generation=registry.generation,
    )
    with exclusive_runtime_admission(brain.profile) as admission:
        receipt = register_historical_claim(
            brain.profile, request, authority=brain.authority, admission=admission,
            validate_before_write=lambda: None,
        )
    assert receipt.dto_version == 2
    assert receipt.outcome == "denied_claim_recorded"
    assert _selected(brain, value, reader) == {control}
    assert _selected(brain, value, brain.authority) == {target, control}
    with pytest.raises(FacetError, match="^not_found$"):
        brain.call("facet.assignment.get", record_id=target, authority=reader)
    brain.engine = BrainEngine.open(brain.profile)
    assert _selected(brain, value, reader) == {control}


@pytest.mark.parametrize("reader_kind", ["owner", "delegated"])
def test_frozen_publication_revalidates_withdrawn_selected_sources(
    tmp_path: Path, reader_kind: str,
) -> None:
    brain = LiveFacetBrain(tmp_path)
    proposal = brain.propose("synthetic-before-withdrawal")
    _withdraw(brain.engine, brain.captures[0].capture_id, brain.owner)
    authority = brain.owner if reader_kind == "owner" else replace(
        brain.owner, owner=False,
        capabilities=frozenset({"facet-read", "search", "review-propose", "facet-query-write"}),
    )
    request = _publication_request(brain, "synthetic-before-withdrawal")
    if reader_kind == "owner":
        assert brain.publication.propose_from_facet_query(
            request, authority=authority,
        ).to_wire() == proposal
    else:
        with pytest.raises(FacetError, match="^not_found$"):
            brain.publication.propose_from_facet_query(request, authority=authority)
    with pytest.raises((FacetError, ValueError), match="not_found|review.*conflict"):
        brain.approve(proposal)
    with brain.engine._store.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM decisions WHERE page_id=? AND outcome IN ('approved','edited')",
            (proposal["page_id"],),
        ).fetchone() is None


def _publication_request(
    brain: LiveFacetBrain, operation_id: str,
) -> ProposeFromFacetQueryRequest:
    return ProposeFromFacetQueryRequest(
        operation_id=operation_id, query_id=brain.query, expected_query_version=1,
        target_page_id=None, expected_page_revision_id=None,
        destination_space_id=brain.spaces[0].space_id,
        title="Synthetic facet page", markdown="Frozen synthetic synthesis.",
    )


def test_withdrawn_provenance_hides_canonical_facet_and_blocks_target_update(
    tmp_path: Path,
) -> None:
    brain = LiveFacetBrain(tmp_path)
    proposal = brain.propose("synthetic-canonical-before-withdrawal")
    brain.approve(proposal)
    assignment = brain.call("facet.assignment.get", {"record_id": proposal["page_id"]})["item"]
    brain.write("facet.assignment.patch", {
        "record_id": proposal["page_id"],
        "expected_revision_id": assignment["revision_id"],
        "expected_version": 0, "add_value_ids": [brain.value], "remove_value_ids": [],
    })
    selector = {"all_value_ids": [brain.value], "record_types": ["canonical"]}
    assert brain.call("facet.query.page", {"selector": selector})["items"][0]["subject"] == {
        "kind": "canonical", "id": proposal["page_id"],
    }
    _withdraw(brain.engine, brain.captures[0].capture_id, brain.owner)
    assert brain.call("facet.query.page", {"selector": selector})["items"] == []
    with pytest.raises(FacetError, match="^not_found$"):
        brain.call("facet.assignment.get", {"record_id": proposal["page_id"]})
    with pytest.raises(FacetError, match="^not_found$"):
        brain.propose(
            "synthetic-withdrawn-target", target_page_id=proposal["page_id"],
            expected_page_revision_id=assignment["revision_id"], destination_space_id=None,
        )
