from __future__ import annotations

from dataclasses import replace

import pytest
from open_brain_engine.ledger.model import ActiveLabelSetSnapshot, BrainState, RecordItemRef
from open_brain_engine.ledger.transitions import (
    BatchLimitError,
    DigestConflict,
    LedgerTransitionError,
    apply_batch,
    resolve_delivery,
)

from ._fixtures import (
    BRAIN,
    batch,
    commit,
    delivery_binding,
    effect_receipt,
    identifier,
    ledger_receipt,
    materialize,
    record,
    snapshot_for,
    stored_record,
)


def test_batch_is_atomic_when_a_later_item_has_narrowed_processor_labels() -> None:
    source = record("a", labels=("private", "work"))
    invalid = record(
        "b",
        labels=("private",),
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    state = BrainState.empty(BRAIN)

    with pytest.raises(ValueError, match="compartments"):
        apply_batch(state, batch(source, invalid), snapshot_for(state))

    assert state.records == {}


def test_replay_conflict_and_delivery_erasure_are_closed_variants() -> None:
    value = stored_record(record("a"))
    committed = commit(item_refs=(RecordItemRef(value.record_id),))
    receipt = ledger_receipt()
    binding = delivery_binding(receipt)
    state = BrainState.empty(
        BRAIN,
        records={value.record_id: value},
        commits={committed.commit_id: committed},
        receipts={receipt.receipt_id: receipt},
        deliveries={receipt.delivery_id: binding},
    )

    replay = resolve_delivery(state, BRAIN, receipt.delivery_id, "a" * 64)
    assert replay is not None and replay.kind == "replayed"
    assert isinstance(resolve_delivery(state, BRAIN, receipt.delivery_id, "b" * 64), DigestConflict)
    purged = BrainState.empty(BRAIN, purged_deliveries={receipt.delivery_id})
    erased = resolve_delivery(purged, BRAIN, receipt.delivery_id, "a" * 64)
    assert erased is not None and erased.kind == "delivery_purged"


def test_capacity_uses_authoritative_post_batch_snapshot_before_state_changes() -> None:
    values = tuple(stored_record(record(token)) for token in ("a", "b", "c", "d"))
    state = BrainState.empty(BRAIN, records={value.record_id: value for value in values})
    snapshot = ActiveLabelSetSnapshot(BRAIN, {("private",): 4}, max_active_label_sets=1)

    with pytest.raises(BatchLimitError) as error:
        apply_batch(state, batch(record("e", labels=("work",))), snapshot)

    assert error.value.code == "active_label_set_limit"
    assert set(state.records) == {value.record_id for value in values}


def test_rejects_tombstoned_ancestor_and_finds_transitive_descendants_in_order() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    grandchild = record(
        "c",
        record_sources=(child.record_id,),
        processor=("summary", "1"),
    )
    empty = BrainState.empty(BRAIN)
    accepted = apply_batch(empty, batch(source, child, grandchild), snapshot_for(empty))
    state = materialize(accepted)

    assert state.descendants_of(source.record_id) == (child.record_id, grandchild.record_id)
    tombstoned = replace(
        state,
        suppressed_ids=frozenset(
            {source.record_id, child.record_id, grandchild.record_id}
        ),
        tombstoned_ids=frozenset({source.record_id}),
    )
    derived = record(
        "d",
        record_sources=(grandchild.record_id,),
        processor=("summary", "2"),
    )
    with pytest.raises(ValueError, match="tombstoned"):
        apply_batch(tombstoned, batch(derived), accepted.active_label_sets)


def test_unknown_effect_reconciliation_is_an_atomic_commit_item() -> None:
    unknown = effect_receipt("a")
    empty = BrainState.empty(BRAIN)
    accepted = apply_batch(empty, batch(unknown), snapshot_for(empty))
    state = materialize(accepted)
    terminal = effect_receipt(
        "b",
        outcome="succeeded",
        reconciles_receipt_id=unknown.receipt_id,
    )

    reconciled = apply_batch(state, batch(terminal), accepted.active_label_sets)
    next_state = materialize(reconciled)

    assert next_state.effects[unknown.effect_id].current == terminal

    wrong_predecessor = effect_receipt(
        "c",
        outcome="failed",
        reconciles_receipt_id=identifier("rcp", "z"),
    )
    with pytest.raises(LedgerTransitionError, match="current receipt"):
        apply_batch(state, batch(wrong_predecessor), accepted.active_label_sets)
