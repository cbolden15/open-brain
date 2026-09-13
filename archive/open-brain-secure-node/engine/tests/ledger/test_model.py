from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from open_brain_engine.ledger.model import BrainState, FrozenJson, ProcessorIdentity, Record
from open_brain_engine.ledger.transitions import LedgerTransitionError, apply_batch

from ._fixtures import BRAIN, batch, record, snapshot_for, stored_record


def test_ledger_values_defensively_freeze_nested_caller_inputs() -> None:
    labels = ["private"]
    processor = ["summary", "1"]
    nested = ["first"]
    body: dict[str, object] = {"nested": {"items": nested}}
    source_id = "rec_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    value = record(
        "b",
        labels=labels,
        record_sources=(source_id,),
        processor=processor,  # type: ignore[arg-type]
        body=body,
    )

    labels.append("work")
    processor[1] = "2"
    nested.append("second")

    assert value.compartments == ("private",)
    frozen_processor = value.provenance.processor
    assert isinstance(frozen_processor, ProcessorIdentity)
    assert frozen_processor.version == "1"
    frozen_nested = cast(dict[str, FrozenJson], value.body)["nested"]
    assert cast(dict[str, FrozenJson], frozen_nested)["items"] == ("first",)
    with pytest.raises(FrozenInstanceError):
        value.record_id = "rec_caaaaaaaaaaaaaaaaaaaaaaaaa"  # type: ignore[misc]


def test_brain_state_freezes_mappings_and_lifecycle_sets() -> None:
    value = stored_record(record("a"))
    records = {value.record_id: value}
    tombstones = {value.record_id}
    state = BrainState.empty(
        BRAIN,
        records=records,
        suppressed_ids=tombstones,
        tombstoned_ids=tombstones,
    )

    records.clear()
    tombstones.clear()

    assert tuple(state.records) == (value.record_id,)
    assert state.tombstoned_ids == frozenset({value.record_id})
    with pytest.raises(TypeError):
        mutable = cast(dict[str, Record], state.records)
        mutable[value.record_id] = value


def test_state_boundary_revalidates_forged_nested_values() -> None:
    forged = stored_record(record("a"))
    object.__setattr__(forged, "record_type", "")

    with pytest.raises(ValueError, match="record_type"):
        BrainState.empty(BRAIN, records={forged.record_id: forged})


def test_transition_boundary_revalidates_forged_batch_items() -> None:
    state = BrainState.empty(BRAIN)
    forged = record("a")
    object.__setattr__(forged, "body", {"unsupported": object()})

    with pytest.raises(LedgerTransitionError, match="commit batch value is invalid"):
        apply_batch(state, batch(forged), snapshot_for(state))


def test_record_preserves_the_complete_frozen_semantic_inventory() -> None:
    value = record("a", origin_id="source:item:1", body={"text": "kept", "rank": 1})

    assert value.record_type == "note"
    assert value.content_schema.schema_id == "note"
    assert value.producer_principal_id == "pri_aaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert value.origin_id == "source:item:1"
    assert value.captured_at == "2026-09-06T12:00:00Z"
    assert value.observed_at == "2026-09-06T12:00:00Z"
    assert value.body == {"text": "kept", "rank": 1}
    assert value.ciphertext_state == "pending"
    assert value.ciphertext_digest is None
