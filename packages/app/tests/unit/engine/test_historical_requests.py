"""Historical operation bytes bind denial separately from positive authority."""

from dataclasses import replace
from hashlib import sha256
from typing import Any, cast

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.historical_contracts import (
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    HistoricalDestination,
    HistoricalRevocationRequest,
    HistoricalSourceCAS,
    RetainedCaptureEvidence,
)
from open_brain_engine.engine.sharing_contracts import SharingError


def _source() -> HistoricalSourceCAS:
    return HistoricalSourceCAS(
        source_id="source_synthetic",
        expected_head="capture_original",
        expected_head_version=1,
        expected_route_version=0,
        expected_lifecycle_version=0,
        expected_control_epoch=0,
        expected_lifecycle="active",
        expected_availability="available",
        expected_historical_only=False,
    )


def _retained() -> RetainedCaptureEvidence:
    return RetainedCaptureEvidence(
        capture_id="capture_copy",
        source_sha256="a" * 64,
        retained_delivery_id="synthetic.copy",
        retained_request_sha256="b" * 64,
        privacy_sha256="c" * 64,
    )


def _claim() -> HistoricalClaimRequest:
    return HistoricalClaimRequest(
        operation_id="claim.synthetic",
        destination=HistoricalDestination(brain_id="brn_synthetic", issuer_epoch=1),
        source_cas=_source(),
        capture_source_cas=replace(
            _source(), source_id="source_copy", expected_head="capture_copy"
        ),
        retained_capture=_retained(),
        claim_role="historical_copy",
        expected_claim_generation=0,
    )


def _relation() -> HistoricalCopyRelationRequest:
    claim = _claim()
    return HistoricalCopyRelationRequest(
        operation_id="relation.synthetic",
        destination=claim.destination,
        source_cas=claim.source_cas,
        copy_source_cas=claim.capture_source_cas,
        baseline_operation_id="baseline.synthetic",
        copy_claim_operation_id=claim.operation_id,
        retained_copy=claim.retained_capture,
        approval_evidence_sha256="d" * 64,
        provider_ids=("anthropic", "openai"),
        expected_relation_version=0,
        expected_claim_generation=1,
    )


def test_claim_request_is_closed_and_domain_separated() -> None:
    request = _claim()
    assert HistoricalClaimRequest.from_value(request.value()) == request
    assert (
        request.request_sha256
        == sha256(
            b"open-brain-historical-request.v1\0" + portable_canonical_json_bytes(request.value())
        ).hexdigest()
    )
    for change in ({"owner": True}, {"dto_version": True}, {"claim_role": "eligible_copy"}):
        with pytest.raises(SharingError, match="invalid_arguments"):
            HistoricalClaimRequest.from_value(dict(request.value(), **change))


@pytest.mark.parametrize(
    "change",
    [
        {"expected_lifecycle": "retired"},
        {"expected_availability": "missing"},
        {"expected_availability": "inaccessible"},
        {"expected_availability": "unknown"},
        {"expected_historical_only": True},
    ],
)
def test_denial_and_revocation_allow_inactive_witness_but_relation_does_not(
    change: dict[str, object],
) -> None:
    source = HistoricalSourceCAS.from_value(dict(_source().value(), **change))
    claim = replace(_claim(), source_cas=source)
    assert HistoricalClaimRequest.from_value(claim.value()) == claim
    revoke = HistoricalRevocationRequest(
        operation_id="revoke.synthetic",
        destination=claim.destination,
        source_cas=source,
        relation_operation_id="relation.synthetic",
        expected_relation_version=1,
        expected_claim_generation=2,
        reason_code="retained_approval_revoked",
    )
    assert HistoricalRevocationRequest.from_value(revoke.value()) == revoke
    with pytest.raises(SharingError, match="revision_changed"):
        replace(_relation(), source_cas=source)


def test_relation_provider_and_approval_evidence_are_bound() -> None:
    relation = _relation()
    assert HistoricalCopyRelationRequest.from_value(relation.value()) == relation
    changed = replace(relation, approval_evidence_sha256="e" * 64)
    assert changed.request_sha256 != relation.request_sha256
    for providers in ((), ("openai", "anthropic"), ("openai", "openai"), ("bad provider",)):
        with pytest.raises(SharingError, match="invalid_arguments"):
            replace(relation, provider_ids=providers)


def test_direct_request_construction_cannot_substitute_unvalidated_nested_mapping() -> None:
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(_claim(), destination=cast(Any, {"brain_id": "brn_synthetic", "issuer_epoch": 1}))


def test_copy_source_identity_is_separate_and_its_lifecycle_is_fenced() -> None:
    claim = _claim()
    assert claim.source_cas.source_id != claim.capture_source_cas.source_id
    retired_copy = replace(claim.capture_source_cas, expected_lifecycle="retired")
    denied = replace(claim, capture_source_cas=retired_copy)
    assert HistoricalClaimRequest.from_value(denied.value()) == denied
    with pytest.raises(SharingError, match="revision_changed"):
        replace(_relation(), copy_source_cas=retired_copy)
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(claim, claim_role="baseline_original")


@pytest.mark.parametrize("kind", ["claim", "relation"])
def test_source_witnesses_share_one_control_epoch(kind: str) -> None:
    """Two source snapshots cannot describe different global writer fences."""
    if kind == "claim":
        request = _claim()
        with pytest.raises(SharingError, match="invalid_arguments"):
            replace(
                request,
                capture_source_cas=replace(request.capture_source_cas, expected_control_epoch=1),
            )
    else:
        relation = _relation()
        with pytest.raises(SharingError, match="invalid_arguments"):
            replace(
                relation,
                copy_source_cas=replace(relation.copy_source_cas, expected_control_epoch=1),
            )


def test_one_source_cannot_have_two_different_state_witnesses() -> None:
    claim = _claim()
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(
            claim,
            capture_source_cas=replace(claim.source_cas, expected_head="capture_copy"),
        )
    # Denial may refer to an older capture in the same source. It does not
    # require that capture to be the source's current head.
    same_source = replace(claim, capture_source_cas=claim.source_cas)
    assert HistoricalClaimRequest.from_value(same_source.value()) == same_source
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(
            _relation(),
            copy_source_cas=replace(claim.source_cas, expected_head="capture_copy"),
        )
