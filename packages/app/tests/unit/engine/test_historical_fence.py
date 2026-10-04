"""Pending forward intent survives restart and fences matching old projections."""

from dataclasses import replace
from pathlib import Path

import pytest
from open_brain_engine.engine.historical_contracts import HistoricalReceipt
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.historical_transition import HistoricalTransition
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.storage.filesystem import capture_root_identity

from packages.app.tests.unit.engine.test_historical_receipts import _value
from packages.app.tests.unit.engine.test_historical_transition import _transition


def test_pending_intent_fences_even_when_old_registry_still_matches_sql(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    identity = capture_root_identity(root)
    registry_store = HistoricalRegistryStore(root, identity)
    record = _transition()
    before = registry_store.initialize_empty(record.request.destination)
    fence = HistoricalPendingFence(root, identity)
    with pytest.raises(SharingError, match="binding_mismatch"):
        fence.assert_settled(before)
    fence.initialize_empty(before)
    fence.assert_settled(before)
    fence.prepare(record)
    with pytest.raises(SharingError, match="operation_pending"):
        fence.assert_settled(before)
    reopened = HistoricalPendingFence(root, identity)
    assert reopened.pending() == record
    reopened.prepare(record)  # Same exact intent is a resume, not another mutation.
    registry_store.advance(before, record.proposed)
    with pytest.raises(SharingError, match="operation_pending"):
        reopened.assert_settled(record.proposed)
    # SQL commit/verification is the future owner task's obligation, not this
    # filesystem primitive's claim. Central reads must also verify SQL equality.
    reopened.mark_complete(record, record.proposed)
    reopened.assert_settled(record.proposed)
    assert reopened.pending() is None
    reopened.prepare(record)
    reopened.mark_complete(record, record.proposed)
    assert reopened.pending() is None  # Completed replay cannot re-pend/reactivate.
    with pytest.raises(SharingError, match="binding_mismatch"):
        reopened.assert_settled(before)


def test_pending_operation_cannot_be_superseded_or_completed_early(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    identity = capture_root_identity(root)
    record = _transition()
    registry = HistoricalRegistryStore(root, identity)
    registry.initialize_empty(record.request.destination)
    fence = HistoricalPendingFence(root, identity)
    fence.initialize_empty(record.previous)
    fence.prepare(record)
    with pytest.raises(SharingError, match="binding_mismatch"):
        fence.mark_complete(record, record.previous)
    with pytest.raises(SharingError, match="binding_mismatch"):
        fence.mark_complete(record, record.proposed)  # Disk has not advanced.
    changed_request = replace(record.request, operation_id="different.operation")
    # The request is deliberately not a valid transition with the old receipt.
    # Type validation must refuse without changing pending evidence.
    with pytest.raises(SharingError):
        replace(record, request=changed_request)
    changed_receipt = HistoricalReceipt.from_value(
        _value(
            **dict(
                {
                    key: item
                    for key, item in record.receipt.value().items()
                    if key != "receipt_sha256"
                },
                operation_id=changed_request.operation_id,
                request_sha256=changed_request.request_sha256,
            )
        )
    )
    competitor = HistoricalTransition.create(
        request=changed_request,
        previous=record.previous,
        proposed=record.proposed,
        receipt=changed_receipt,
    )
    with pytest.raises(SharingError, match="operation_pending"):
        fence.prepare(competitor)
    assert fence.pending() == record


def test_lost_pending_record_refuses_instead_of_unfencing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    identity = capture_root_identity(root)
    record = _transition()
    HistoricalRegistryStore(root, identity).initialize_empty(record.request.destination)
    fence = HistoricalPendingFence(root, identity)
    fence.initialize_empty(record.previous)
    fence.prepare(record)
    records = root / ".open-brain" / "historical-authority" / "historical-transitions.v1"
    records.rename(root / "retained-transitions")
    with pytest.raises(SharingError, match="binding_mismatch"):
        fence.pending()
    with pytest.raises(SharingError, match="operation_pending"):
        fence.assert_settled(record.previous)


def test_crash_after_intent_before_marker_has_no_projection_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    identity = capture_root_identity(root)
    record = _transition()
    store = HistoricalRegistryStore(root, identity)
    store.initialize_empty(record.request.destination)
    fence = HistoricalPendingFence(root, identity)
    fence.initialize_empty(record.previous)

    def crash(*_args: object) -> None:
        raise RuntimeError("synthetic crash")

    with monkeypatch.context() as fault:
        fault.setattr(fence, "_replace", crash)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            fence.prepare(record)
    assert store.read(record.request.destination) == record.previous
    # Unreferenced immutable intent is not a committed or admitted operation.
    assert fence.pending() is None
    fence.prepare(record)
    assert fence.pending() == record
