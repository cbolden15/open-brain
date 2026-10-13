"""Forward records bind exact operation evidence before cross-file changes."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine.historical_contracts import (
    HistoricalClaimRequest,
    HistoricalReceipt,
    HistoricalRevocationRequest,
)
from open_brain_engine.engine.historical_registry import (
    HistoricalClaimMembership,
    HistoricalClaimRegistry,
)
from open_brain_engine.engine.historical_transition import (
    HistoricalTransition,
    HistoricalTransitionStore,
    validate_historical_chain,
)
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.storage.filesystem import DuplicateConflictError, capture_root_identity

from packages.app.tests.unit.engine.test_historical_observation import _baseline
from packages.app.tests.unit.engine.test_historical_receipts import _value
from packages.app.tests.unit.engine.test_historical_requests import _claim, _relation


def _transition() -> HistoricalTransition:
    request = _claim()
    before = HistoricalClaimRegistry.empty(request.destination)
    member = HistoricalClaimMembership(
        capture_id=request.retained_capture.capture_id,
        source_id=request.source_cas.source_id,
        capture_source_id=request.capture_source_cas.source_id,
        claim_role=request.claim_role,
    )
    after = before.register(member)
    receipt = HistoricalReceipt.from_value(
        _value(
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            source_id=request.source_cas.source_id,
            outcome="denied_claim_recorded",
            copy_capture_id=member.capture_id,
            original_capture_id=request.source_cas.expected_head,
        )
    )
    return HistoricalTransition.create(
        request=request, previous=before, proposed=after, receipt=receipt
    )


def test_transition_is_closed_and_exactly_replayable() -> None:
    record = _transition()
    assert HistoricalTransition.from_bytes(record.canonical_bytes()) == record
    assert record.request.request_sha256 == record.receipt.request_sha256
    for field in ("request", "previous", "proposed", "receipt"):
        value = json.loads(record.canonical_bytes())
        value[field]["owner"] = True
        with pytest.raises(SharingError, match="invalid_arguments"):
            HistoricalTransition.from_bytes(json.dumps(value).encode())


def test_chain_requires_every_generation_even_without_membership_change() -> None:
    first = _transition()
    request = replace(
        first.request, operation_id="claim.second", expected_claim_generation=1
    )
    proposed = HistoricalClaimRegistry._create(
        first.proposed.destination, 2, first.proposed.memberships
    )
    receipt = HistoricalReceipt.from_value(
        _value(
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            source_id=request.source_cas.source_id,
            outcome="denied_claim_recorded",
            copy_capture_id=first.receipt.copy_capture_id,
            original_capture_id=first.receipt.original_capture_id,
            claim_generation=2,
        )
    )
    second = HistoricalTransition.create(
        request=request, previous=first.proposed, proposed=proposed, receipt=receipt
    )
    assert validate_historical_chain(first.previous, ()) == ()
    assert validate_historical_chain(proposed, (first, second)) == (first, second)
    assert first.proposed.memberships == second.proposed.memberships
    for damaged in ((), (first,), (second,), (second, first), (first, first)):
        with pytest.raises(SharingError, match="binding_mismatch"):
            validate_historical_chain(proposed, damaged)
    with pytest.raises(SharingError, match="binding_mismatch"):
        validate_historical_chain(first.proposed, (first, second))


@pytest.mark.parametrize(
    "change",
    ["receipt_request", "receipt_operation", "receipt_generation", "extra_member", "remove_member"],
)
def test_forward_record_refuses_conflicting_or_unrequested_evidence(change: str) -> None:
    record = _transition()
    receipt = record.receipt
    previous, proposed = record.previous, record.proposed
    if change.startswith("receipt"):
        changes: dict[str, dict[str, object]] = {
            "receipt_request": {"request_sha256": "c" * 64},
            "receipt_operation": {"operation_id": "another.operation"},
            "receipt_generation": {"claim_generation": 2},
        }
        receipt = HistoricalReceipt.from_value(
            _value(
                **dict(
                    {key: item for key, item in receipt.value().items() if key != "receipt_sha256"},
                    **changes[change],
                )
            )
        )
    elif change == "extra_member":
        proposed = proposed.register(
            HistoricalClaimMembership(
                capture_id="capture_unrequested",
                source_id="source_other",
                capture_source_id="source_other",
                claim_role="baseline_original",
            )
        )
    else:
        previous, proposed = proposed, previous
    with pytest.raises(SharingError, match="invalid_arguments"):
        HistoricalTransition.create(
            request=record.request, previous=previous, proposed=proposed, receipt=receipt
        )


def test_store_persists_original_bytes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    store = HistoricalTransitionStore(root, capture_root_identity(root))
    record = _transition()
    store.persist(record)
    assert store.read(record.request.operation_id) == record


def test_baseline_transition_binds_retained_original_not_observed_capture(tmp_path: Path) -> None:
    request = _baseline(tmp_path)
    previous = HistoricalClaimRegistry.empty(request.destination)
    proposed = previous.register(
        HistoricalClaimMembership(
            capture_id=request.retained_original.capture_id,
            source_id=request.source_cas.source_id,
            capture_source_id=request.source_cas.source_id,
            claim_role="baseline_original",
        )
    )
    receipt = HistoricalReceipt.from_value(
        _value(
            destination=request.destination.value(),
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            source_id=request.source_cas.source_id,
            original_capture_id=request.retained_original.capture_id,
        )
    )
    record = HistoricalTransition.create(
        request=request, previous=previous, proposed=proposed, receipt=receipt
    )
    assert HistoricalTransition.from_bytes(record.canonical_bytes()) == record


@pytest.mark.parametrize("revoke", [False, True])
def test_relation_and_revocation_keep_both_prior_memberships(revoke: bool) -> None:
    relation = replace(_relation(), expected_claim_generation=2)
    previous = (
        HistoricalClaimRegistry.empty(relation.destination)
        .register(
            HistoricalClaimMembership(
                capture_id=relation.source_cas.expected_head,
                source_id=relation.source_cas.source_id,
                capture_source_id=relation.source_cas.source_id,
                claim_role="baseline_original",
            )
        )
        .register(
            HistoricalClaimMembership(
                capture_id=relation.retained_copy.capture_id,
                source_id=relation.source_cas.source_id,
                capture_source_id=relation.copy_source_cas.source_id,
                claim_role="historical_copy",
            )
        )
    )
    proposed = HistoricalClaimRegistry._create(previous.destination, 3, previous.memberships)
    request = (
        HistoricalRevocationRequest(
            operation_id="revoke.synthetic",
            destination=relation.destination,
            source_cas=replace(
                relation.source_cas, expected_head="capture_new_head", expected_lifecycle="retired"
            ),
            relation_operation_id=relation.operation_id,
            expected_relation_version=1,
            expected_claim_generation=2,
            reason_code="retained_approval_revoked",
        )
        if revoke
        else relation
    )
    receipt = HistoricalReceipt.from_value(
        _value(
            operation_id=request.operation_id,
            request_sha256=request.request_sha256,
            source_id=request.source_cas.source_id,
            original_capture_id=relation.source_cas.expected_head,
            copy_capture_id=relation.retained_copy.capture_id,
            claim_generation=3,
            relation_version=2 if revoke else 1,
            outcome="historical_copy_revoked" if revoke else "historical_copy_linked",
        )
    )
    record = HistoricalTransition.create(
        request=request, previous=previous, proposed=proposed, receipt=receipt
    )
    assert HistoricalTransition.from_bytes(record.canonical_bytes()) == record
    assert record.previous.memberships == record.proposed.memberships
    # Retired/advanced current state does not rewrite the historic original ID.
    assert record.receipt.original_capture_id == relation.source_cas.expected_head


def test_store_refuses_changed_operation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    store = HistoricalTransitionStore(root, capture_root_identity(root))
    record = _transition()
    store.persist(record)
    # A different valid request/receipt with the same operation cannot overwrite.
    assert isinstance(record.request, HistoricalClaimRequest)
    request = replace(
        record.request,
        retained_capture=replace(record.request.retained_capture, privacy_sha256="d" * 64),
    )
    receipt = HistoricalReceipt.from_value(
        _value(
            **dict(
                {
                    key: item
                    for key, item in record.receipt.value().items()
                    if key != "receipt_sha256"
                },
                request_sha256=request.request_sha256,
            )
        )
    )
    changed = HistoricalTransition.create(
        request=request, previous=record.previous, proposed=record.proposed, receipt=receipt
    )
    with pytest.raises(DuplicateConflictError):
        store.persist(changed)
    assert store.read(record.request.operation_id) == record
