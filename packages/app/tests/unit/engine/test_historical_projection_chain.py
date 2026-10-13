"""Linked-copy authority references must resolve to exact prior history."""

from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine.historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    HistoricalReceipt,
    HistoricalRevocationRequest,
)
from open_brain_engine.engine.historical_projection import historical_projection_rows
from open_brain_engine.engine.historical_registry import (
    HistoricalClaimMembership,
    HistoricalClaimRegistry,
)
from open_brain_engine.engine.historical_transition import HistoricalRequest, HistoricalTransition
from open_brain_engine.engine.sharing_contracts import SharingError

from packages.app.tests.unit.engine.test_historical_observation import _baseline
from packages.app.tests.unit.engine.test_historical_receipts import _value
from packages.app.tests.unit.engine.test_historical_requests import _claim


def _record(request: HistoricalRequest, previous: HistoricalClaimRegistry) -> HistoricalTransition:
    members = set(previous.memberships)
    original = request.source_cas.expected_head
    copy: str | None = None
    version: int | None = None
    if type(request) is HistoricalBaselineRequest:
        outcome = "baseline_adopted"
        members.add(
            HistoricalClaimMembership(
                capture_id=original,
                source_id=request.source_cas.source_id,
                capture_source_id=request.source_cas.source_id,
                claim_role="baseline_original",
            )
        )
    elif type(request) is HistoricalClaimRequest:
        outcome = "denied_claim_recorded"
        copy = request.retained_capture.capture_id
        members.add(
            HistoricalClaimMembership(
                capture_id=copy,
                source_id=request.source_cas.source_id,
                capture_source_id=request.capture_source_cas.source_id,
                claim_role="historical_copy",
            )
        )
    elif type(request) is HistoricalCopyRelationRequest:
        outcome, copy, version = "historical_copy_linked", request.retained_copy.capture_id, 1
    else:
        outcome, version = "historical_copy_revoked", 2
        copy = next(m.capture_id for m in previous.memberships if m.claim_role == "historical_copy")
    proposed = HistoricalClaimRegistry._create(
        previous.destination,
        previous.generation + 1,
        tuple(sorted(members, key=lambda member: member.capture_id)),
    )
    receipt = HistoricalReceipt.from_value(
        _value(
            destination=request.destination.value(),
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            source_id=request.source_cas.source_id,
            original_capture_id=original,
            copy_capture_id=copy,
            outcome=outcome,
            claim_generation=proposed.generation,
            relation_version=version,
        )
    )
    return HistoricalTransition.create(
        request=request, previous=previous, proposed=proposed, receipt=receipt
    )


def _chain(tmp_path: Path) -> tuple[HistoricalTransition, ...]:
    baseline = _baseline(tmp_path)
    first = _record(baseline, HistoricalClaimRegistry.empty(baseline.destination))
    claim = replace(
        _claim(),
        destination=baseline.destination,
        source_cas=baseline.source_cas,
        expected_claim_generation=1,
    )
    second = _record(claim, first.proposed)
    relation = HistoricalCopyRelationRequest(
        operation_id="relation.synthetic",
        destination=baseline.destination,
        source_cas=baseline.source_cas,
        copy_source_cas=claim.capture_source_cas,
        baseline_operation_id=baseline.operation_id,
        copy_claim_operation_id=claim.operation_id,
        retained_copy=claim.retained_capture,
        approval_evidence_sha256="d" * 64,
        provider_ids=("openai",),
        expected_relation_version=0,
        expected_claim_generation=2,
    )
    third = _record(relation, second.proposed)
    revoke = HistoricalRevocationRequest(
        operation_id="revoke.synthetic",
        destination=baseline.destination,
        source_cas=baseline.source_cas,
        relation_operation_id=relation.operation_id,
        expected_relation_version=1,
        expected_claim_generation=3,
        reason_code="synthetic_owner_revocation",
    )
    return first, second, third, _record(revoke, third.proposed)


def test_complete_chain_retains_link_and_revocation_without_membership_change(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path)
    rows = historical_projection_rows(chain[-1].proposed, chain)
    assert len(rows["historical_claims"]) == 2
    assert len(rows["historical_baselines"]) == 1
    assert len(rows["historical_relations"]) == 1
    assert rows["historical_revocations"] == [("revoke.synthetic", "relation.synthetic", 2)]
    assert chain[1].proposed.memberships == chain[3].proposed.memberships
    with pytest.raises(SharingError, match="binding_mismatch"):
        historical_projection_rows(chain[-1].proposed, chain[:-1])


@pytest.mark.parametrize("reference", ["baseline_operation_id", "copy_claim_operation_id"])
def test_relation_with_dangling_prior_reference_refuses(tmp_path: Path, reference: str) -> None:
    chain = _chain(tmp_path)
    relation = chain[2].request
    assert isinstance(relation, HistoricalCopyRelationRequest)
    request = (
        replace(relation, baseline_operation_id="absent.operation")
        if reference == "baseline_operation_id"
        else replace(relation, copy_claim_operation_id="absent.operation")
    )
    changed = _record(request, chain[1].proposed)
    with pytest.raises(SharingError, match="binding_mismatch"):
        historical_projection_rows(changed.proposed, (*chain[:2], changed))


def test_revocation_cannot_reference_a_different_missing_relation(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    revocation = chain[3].request
    assert isinstance(revocation, HistoricalRevocationRequest)
    request = replace(revocation, relation_operation_id="absent.relation")
    changed = _record(request, chain[2].proposed)
    with pytest.raises(SharingError, match="binding_mismatch"):
        historical_projection_rows(changed.proposed, (*chain[:3], changed))
