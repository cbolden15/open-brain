from __future__ import annotations

from dataclasses import replace

import pytest
from open_brain_engine.ledger.model import (
    ActiveLabelSetSnapshot,
    Artifact,
    BrainState,
    CommitBatch,
    Effect,
    ProposalItemRef,
    RecordItemRef,
    ResourceLimitError,
    processor_output_identity,
)
from open_brain_engine.ledger.transitions import (
    LedgerTransitionError,
    RevisionConflict,
    apply_batch,
    evaluate_batch,
)

from ._fixtures import (
    BRAIN,
    POLICY_DIGEST,
    batch,
    commit,
    decision,
    delivery_binding,
    effect_receipt,
    identifier,
    ledger_receipt,
    materialize,
    proposal,
    provenance,
    purge_transition,
    record,
    revision,
    snapshot_for,
    stored_record,
)


def test_commit_batch_defensively_freezes_caller_items() -> None:
    source = record("a")
    items = [source]
    value = CommitBatch(
        brain_id=BRAIN,
        delivery_id=identifier("dlv", "a"),
        digest="a" * 64,
        sequencer_epoch=1,
        issuer_epoch=1,
        policy_digest=POLICY_DIGEST,
        items=items,
    )

    items.clear()

    assert value.items == (source,)


def test_processor_identity_defensively_freezes_caller_values() -> None:
    processor = ["summary", "1"]
    value = provenance(
        record_sources=(identifier("rec", "a"),),
        derivation="processor",
        processor=processor,  # type: ignore[arg-type]
    )
    identity = processor_output_identity(value)

    processor[1] = "2"

    assert processor_output_identity(value) == identity


def test_brain_state_defensively_freezes_tombstone_sets() -> None:
    source = stored_record(record("a"))
    tombstones = {source.record_id}
    state = BrainState.empty(
        BRAIN,
        records={source.record_id: source},
        suppressed_ids=tombstones,
        tombstoned_ids=tombstones,
    )

    tombstones.clear()

    assert state.tombstoned_ids == frozenset({source.record_id})


def test_transitive_tombstoned_ancestor_rejects_new_descendant() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        derivation="processor",
        processor=("summary", "1"),
    )
    empty = BrainState.empty(BRAIN)
    accepted = apply_batch(empty, batch(source, child), snapshot_for(empty))
    state = materialize(accepted)
    poisoned = replace(
        state,
        suppressed_ids=frozenset({source.record_id, child.record_id}),
        tombstoned_ids=frozenset({source.record_id}),
    )
    grandchild = record(
        "c",
        record_sources=(child.record_id,),
        derivation="processor",
        processor=("summary", "2"),
    )

    with pytest.raises(ValueError, match="tombstoned"):
        apply_batch(poisoned, batch(grandchild), snapshot_for(poisoned))


def test_same_batch_purge_race_is_rejected_in_both_item_orders() -> None:
    source = stored_record(record("a"))
    state = BrainState.empty(BRAIN, records={source.record_id: source})
    descendant = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    purge = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )

    for items in ((descendant, purge), (purge, descendant)):
        with pytest.raises(LedgerTransitionError, match="purge-pending"):
            apply_batch(state, batch(*items), snapshot_for(state))


def test_provenance_graph_is_order_independent_and_includes_effect_receipts() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    effect = effect_receipt(
        "c",
        source_records=(child.record_id,),
        processor=("actuator", "1"),
    )
    empty = BrainState.empty(BRAIN)

    application = apply_batch(empty, batch(effect, child, source), snapshot_for(empty))
    state = materialize(application)

    assert state.descendants_of(source.record_id) == (child.record_id, effect.receipt_id)


def test_authoritative_state_rejects_dangling_and_cyclic_provenance() -> None:
    missing = record(
        "a",
        record_sources=(identifier("rec", "b"),),
        processor=("summary", "1"),
    )
    with pytest.raises(ValueError, match="source record is unknown"):
        BrainState.empty(BRAIN, records={missing.record_id: stored_record(missing)})

    first = stored_record(
        record(
            "a",
            record_sources=(identifier("rec", "b"),),
            processor=("summary", "1"),
        )
    )
    second = stored_record(
        record(
            "b",
            record_sources=(first.record_id,),
            processor=("summary", "2"),
        )
    )
    with pytest.raises(ValueError, match="cycle"):
        BrainState.empty(BRAIN, records={first.record_id: first, second.record_id: second})


@pytest.mark.parametrize("derivation", ["owner", "import"])
def test_any_lineage_must_preserve_the_source_label_union(derivation: str) -> None:
    source = record("a", labels=("private", "secret"))
    narrowed = record(
        "b",
        labels=("private",),
        record_sources=(source.record_id,),
        derivation=derivation,  # type: ignore[arg-type]
    )
    empty = BrainState.empty(BRAIN)

    with pytest.raises(ValueError, match="compartments"):
        apply_batch(empty, batch(source, narrowed), snapshot_for(empty))


def test_processor_output_identity_is_unique_across_proposals() -> None:
    source = record("a")
    first = proposal(
        "a",
        source_records=(source.record_id,),
        processor=("proposal", "1"),
    )
    duplicate = replace(
        first,
        proposal_id=identifier("prp", "b"),
        proposal_revision_id=identifier("rev", "b"),
    )
    empty = BrainState.empty(BRAIN)

    with pytest.raises(ValueError, match="processor output"):
        apply_batch(empty, batch(source, first, duplicate), snapshot_for(empty))


def test_processor_output_identity_is_unique_across_revisions_and_effects() -> None:
    source = record("a")
    first_revision = revision(
        "b",
        artifact_token="b",
        source_records=(source.record_id,),
        processor=("derive", "1"),
    )
    second_revision = revision(
        "c",
        artifact_token="c",
        source_records=(source.record_id,),
        processor=("derive", "1"),
    )
    empty = BrainState.empty(BRAIN)
    with pytest.raises(ValueError, match="processor output"):
        apply_batch(
            empty,
            batch(source, first_revision, second_revision),
            snapshot_for(empty),
        )

    first_effect = effect_receipt(
        "b",
        effect_token="b",
        source_records=(source.record_id,),
        processor=("actuate", "1"),
    )
    second_effect = effect_receipt(
        "c",
        effect_token="c",
        source_records=(source.record_id,),
        processor=("actuate", "1"),
    )
    with pytest.raises(ValueError, match="processor output"):
        apply_batch(empty, batch(source, first_effect, second_effect), snapshot_for(empty))


def test_capacity_snapshot_cannot_raise_the_frozen_limit() -> None:
    with pytest.raises(ValueError, match="frozen"):
        ActiveLabelSetSnapshot(BRAIN, max_active_label_sets=999)


def test_capacity_snapshot_is_brain_bound_and_canonical() -> None:
    with pytest.raises(ValueError, match="canonical"):
        ActiveLabelSetSnapshot(BRAIN, {("work", "private"): 1})

    state = BrainState.empty(BRAIN)
    other = ActiveLabelSetSnapshot("brn_bbbbbbbbbbbbbbbbbbbbbbbbbb")
    with pytest.raises(LedgerTransitionError, match="share a Brain"):
        apply_batch(state, batch(record("a")), other)


def test_receipt_binding_rejects_global_cursor_and_digest_mismatch() -> None:
    with pytest.raises(ValueError, match="cursor"):
        ledger_receipt(cursor="GLOBAL:123")

    with pytest.raises(ValueError, match="binding"):
        delivery_binding(ledger_receipt(digest="b" * 64), digest="a" * 64)


def test_authoritative_state_binds_receipt_delivery_and_commit_metadata() -> None:
    value = stored_record(record("a"))
    committed = commit(item_refs=(RecordItemRef(value.record_id),))
    receipt = ledger_receipt()
    binding = delivery_binding(receipt)

    BrainState.empty(
        BRAIN,
        records={value.record_id: value},
        commits={committed.commit_id: committed},
        receipts={receipt.receipt_id: receipt},
        deliveries={receipt.delivery_id: binding},
    )

    mismatched_commit = replace(committed, digest="b" * 64)
    with pytest.raises(ValueError, match="receipt.*commit|commit.*receipt"):
        BrainState.empty(
            BRAIN,
            records={value.record_id: value},
            commits={mismatched_commit.commit_id: mismatched_commit},
            receipts={receipt.receipt_id: receipt},
            deliveries={receipt.delivery_id: binding},
        )

    with pytest.raises(ValueError, match="delivery.*receipt"):
        BrainState.empty(
            BRAIN,
            records={value.record_id: value},
            commits={committed.commit_id: committed},
            deliveries={receipt.delivery_id: binding},
        )


@pytest.mark.parametrize("kind", ["record", "revision", "proposal", "decision", "effect"])
def test_label_limit_is_typed_for_every_compartment_bearing_value(kind: str) -> None:
    labels = tuple(f"label-{index}" for index in range(17))

    with pytest.raises(ResourceLimitError) as error:
        if kind == "record":
            record("a", labels=labels)
        elif kind == "revision":
            revision("a", labels=labels)
        elif kind == "proposal":
            proposal("a", labels=labels)
        elif kind == "decision":
            decision("a", labels=labels)
        else:
            effect_receipt("a", labels=labels)

    assert error.value.code == "label_limit"


def test_proposal_revision_lifecycle_preserves_competing_bases_and_typed_conflicts() -> None:
    empty = BrainState.empty(BRAIN)
    genesis = revision("a")
    state = materialize(apply_batch(empty, batch(genesis), snapshot_for(empty)))
    first = proposal("b", proposal_token="b", base_revision_id=genesis.revision_id)
    state = materialize(apply_batch(state, batch(first), snapshot_for(state)))
    second = replace(first, proposal_revision_id=identifier("rev", "c"), body={"text": "new"})
    state = materialize(apply_batch(state, batch(second), snapshot_for(state)))

    stale_decision = decision(
        "b",
        proposal_token="b",
        proposal_revision_token="b",
    )
    result = evaluate_batch(state, batch(stale_decision), snapshot_for(state))
    assert isinstance(result, RevisionConflict)
    assert result.actual_revision_id == second.proposal_revision_id

    current_decision = decision(
        "c",
        proposal_token="b",
        proposal_revision_token="c",
    )
    state = materialize(apply_batch(state, batch(current_decision), snapshot_for(state)))
    duplicate = replace(current_decision, decision_id=identifier("dec", "d"))
    with pytest.raises(RevisionConflict):
        apply_batch(state, batch(duplicate), snapshot_for(state))


def test_revision_head_cannot_advance_without_an_accepted_matching_proposal() -> None:
    empty = BrainState.empty(BRAIN)
    genesis = revision("a")
    state = materialize(apply_batch(empty, batch(genesis), snapshot_for(empty)))
    unreviewed = revision("b", base_revision_id=genesis.revision_id)

    with pytest.raises(LedgerTransitionError, match="proposal and decision"):
        apply_batch(state, batch(unreviewed), snapshot_for(state))


def test_proposal_base_must_belong_to_its_artifact() -> None:
    empty = BrainState.empty(BRAIN)
    other = revision("a", artifact_token="b")
    state = materialize(apply_batch(empty, batch(other), snapshot_for(empty)))
    invalid = proposal("b", artifact_token="a", base_revision_id=other.revision_id)

    with pytest.raises(LedgerTransitionError, match="another artifact"):
        apply_batch(state, batch(invalid), snapshot_for(state))


def test_stale_proposal_base_remains_a_competing_proposal() -> None:
    empty = BrainState.empty(BRAIN)
    genesis = revision("a")
    state = materialize(apply_batch(empty, batch(genesis), snapshot_for(empty)))
    accepted_proposal = proposal(
        "b",
        proposal_token="b",
        base_revision_id=genesis.revision_id,
        body={"text": "accepted"},
    )
    accepted_decision = decision(
        "b",
        proposal_token="b",
        proposal_revision_token="b",
    )
    accepted_revision = revision(
        "c",
        base_revision_id=genesis.revision_id,
        proposal_id=accepted_proposal.proposal_id,
        decision_id=accepted_decision.decision_id,
        body={"text": "accepted"},
    )
    state = materialize(
        apply_batch(
            state,
            batch(accepted_proposal, accepted_decision, accepted_revision),
            snapshot_for(state),
        )
    )
    competing = proposal(
        "d",
        proposal_token="d",
        base_revision_id=genesis.revision_id,
    )

    application = apply_batch(state, batch(competing), snapshot_for(state))
    next_state = materialize(application)
    assert next_state.artifact_heads[genesis.artifact_id] == accepted_revision.revision_id
    assert next_state.proposal_heads[competing.proposal_id] == competing.proposal_revision_id


def test_purge_tracks_multiple_closure_members_and_rejects_out_of_scope_subjects() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    unrelated = record("c")
    empty = BrainState.empty(BRAIN)
    state = materialize(
        apply_batch(empty, batch(source, child, unrelated), snapshot_for(empty))
    )
    target_resolution = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    child_resolution = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=child.record_id,
    )
    application = apply_batch(
        state,
        batch(target_resolution, child_resolution),
        snapshot_for(state),
    )
    assert set(application.purges[target_resolution.purge_id].resolutions) == {
        source.record_id,
        child.record_id,
    }
    assert application.active_label_sets.counts == {("private",): 1}

    outside = purge_transition(
        "b",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=unrelated.record_id,
    )
    second_target = purge_transition(
        "b",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    with pytest.raises(LedgerTransitionError, match="outside"):
        apply_batch(state, batch(second_target, outside), snapshot_for(state))


def test_retain_resolution_requires_a_subject_bound_accepted_review_and_reactivates_it() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    review = proposal(
        "c",
        proposal_token="c",
        artifact_token="c",
        body={
            "kind": "purge_resolution_review",
            "purge_id": identifier("prg", "a"),
            "subject_kind": "record",
            "subject_id": child.record_id,
            "resolution": "retain",
            "replacement_record_id": None,
        },
    )
    approval = decision(
        "c",
        proposal_token="c",
        proposal_revision_token="c",
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(
        apply_batch(empty, batch(source, child, review, approval), snapshot_for(empty))
    )
    purge_target = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    retain_child = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=child.record_id,
        resolution="retain",
        review_decision_id=approval.decision_id,
    )

    application = apply_batch(
        state,
        batch(purge_target, retain_child),
        snapshot_for(state),
    )
    retained_state = materialize(application)
    assert child.record_id not in retained_state.suppressed_ids
    assert application.active_label_sets.counts == {("private",): 1}

    unrelated_review = proposal(
        "d",
        proposal_token="d",
        artifact_token="d",
        body={
            "kind": "purge_resolution_review",
            "purge_id": identifier("prg", "a"),
            "subject_kind": "record",
            "subject_id": source.record_id,
            "resolution": "retain",
            "replacement_record_id": None,
        },
    )
    unrelated_approval = decision(
        "d",
        proposal_token="d",
        proposal_revision_token="d",
    )
    unrelated_state = materialize(
        apply_batch(
            state,
            batch(unrelated_review, unrelated_approval),
            snapshot_for(state),
        )
    )
    invalid_retain = replace(retain_child, review_decision_id=unrelated_approval.decision_id)
    with pytest.raises(LedgerTransitionError, match="exact resolution intent"):
        apply_batch(
            unrelated_state,
            batch(purge_target, invalid_retain),
            snapshot_for(unrelated_state),
        )


def test_authoritative_capacity_decrements_purged_records() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(source, child), snapshot_for(empty)))
    purge_target = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    purge_child = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=child.record_id,
    )

    application = apply_batch(
        state,
        batch(purge_target, purge_child),
        snapshot_for(state),
    )
    assert application.active_label_sets.counts == {}


def test_reviewed_replacement_resolves_the_old_subject_and_activates_replacement() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    replacement = record(
        "c",
        record_sources=(child.record_id,),
        derivation="replacement",
    )
    review = proposal(
        "d",
        proposal_token="d",
        artifact_token="d",
        body={
            "kind": "purge_resolution_review",
            "purge_id": identifier("prg", "a"),
            "subject_kind": "record",
            "subject_id": child.record_id,
            "resolution": "replace",
            "replacement_record_id": replacement.record_id,
        },
    )
    approval = decision(
        "d",
        proposal_token="d",
        proposal_revision_token="d",
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(
        apply_batch(
            empty,
            batch(source, child, replacement, review, approval),
            snapshot_for(empty),
        )
    )
    purge_target = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    replace_child = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=child.record_id,
        resolution="replace",
        replacement_record_id=replacement.record_id,
        review_decision_id=approval.decision_id,
    )

    next_state = materialize(
        apply_batch(
            state,
            batch(purge_target, replace_child),
            snapshot_for(state),
        )
    )

    assert child.record_id in next_state.tombstoned_ids
    assert replacement.record_id not in next_state.suppressed_ids
    assert replacement.record_id not in next_state.purge_pending_ids


def test_state_rejects_artifact_mapping_that_does_not_match_its_identity() -> None:
    artifact = Artifact(BRAIN, identifier("art", "a"))
    with pytest.raises(ValueError, match="mapping key"):
        BrainState.empty(BRAIN, artifacts={identifier("art", "b"): artifact})


def test_loaded_state_rejects_unknown_historical_proposal_base() -> None:
    empty = BrainState.empty(BRAIN)
    genesis = revision("a")
    state = materialize(apply_batch(empty, batch(genesis), snapshot_for(empty)))
    forged = proposal(
        "b",
        proposal_token="b",
        base_revision_id=identifier("rev", "z"),
    )

    with pytest.raises(ValueError, match="proposal base"):
        replace(
            state,
            proposals={forged.proposal_revision_id: forged},
            proposal_heads={forged.proposal_id: forged.proposal_revision_id},
        )


def test_batch_rejects_revision_and_proposal_revision_identity_collision() -> None:
    colliding_revision = revision("b")
    colliding_proposal = proposal("b", proposal_token="b")
    empty = BrainState.empty(BRAIN)

    with pytest.raises(LedgerTransitionError, match="identities must be distinct"):
        apply_batch(
            empty,
            batch(colliding_revision, colliding_proposal),
            snapshot_for(empty),
        )


def test_loaded_state_rejects_duplicate_processor_proposal_revisions() -> None:
    source = stored_record(record("a"))
    first = proposal(
        "b",
        proposal_token="b",
        source_records=(source.record_id,),
        processor=("proposal", "1"),
    )
    second = replace(
        first,
        proposal_revision_id=identifier("rev", "c"),
        body={"text": "changed"},
    )
    artifact = Artifact(BRAIN, first.artifact_id)

    with pytest.raises(ValueError, match="processor output"):
        BrainState.empty(
            BRAIN,
            records={source.record_id: source},
            artifacts={artifact.artifact_id: artifact},
            proposals={
                first.proposal_revision_id: first,
                second.proposal_revision_id: second,
            },
            proposal_heads={first.proposal_id: second.proposal_revision_id},
        )


def test_loaded_state_rejects_provenance_label_narrowing() -> None:
    source = stored_record(record("a", labels=("private", "secret")))
    narrowed = stored_record(
        record(
            "b",
            labels=("private",),
            record_sources=(source.record_id,),
        )
    )

    with pytest.raises(ValueError, match="compartments"):
        BrainState.empty(
            BRAIN,
            records={source.record_id: source, narrowed.record_id: narrowed},
        )


@pytest.mark.parametrize("kind", ["revision", "proposal", "effect_receipt"])
def test_loaded_state_rechecks_label_union_for_every_derived_type(kind: str) -> None:
    source = stored_record(record("a", labels=("private", "secret")))
    if kind == "revision":
        revision_value = revision(
            "b",
            labels=("private",),
            source_records=(source.record_id,),
            processor=("derive", "1"),
        )
        artifact = Artifact(BRAIN, revision_value.artifact_id)
        with pytest.raises(ValueError, match="compartments"):
            BrainState.empty(
                BRAIN,
                records={source.record_id: source},
                artifacts={artifact.artifact_id: artifact},
                revisions={revision_value.revision_id: revision_value},
                artifact_heads={artifact.artifact_id: revision_value.revision_id},
            )
    elif kind == "proposal":
        proposal_value = proposal(
            "b",
            labels=("private",),
            source_records=(source.record_id,),
            processor=("derive", "1"),
        )
        artifact = Artifact(BRAIN, proposal_value.artifact_id)
        with pytest.raises(ValueError, match="compartments"):
            BrainState.empty(
                BRAIN,
                records={source.record_id: source},
                artifacts={artifact.artifact_id: artifact},
                proposals={proposal_value.proposal_revision_id: proposal_value},
                proposal_heads={
                    proposal_value.proposal_id: proposal_value.proposal_revision_id
                },
            )
    else:
        effect_value = effect_receipt(
            "b",
            labels=("private",),
            source_records=(source.record_id,),
            processor=("derive", "1"),
        )
        effect = Effect(
            BRAIN,
            effect_value.effect_id,
            effect_value.external_identity,
            (effect_value,),
        )
        with pytest.raises(ValueError, match="compartments"):
            BrainState.empty(
                BRAIN,
                records={source.record_id: source},
                effects={effect.effect_id: effect},
            )


def test_loaded_tombstone_rejects_an_active_provenance_descendant() -> None:
    source = record("a", labels=("private", "secret"))
    child = record(
        "b",
        labels=("private", "secret"),
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(source, child), snapshot_for(empty)))

    with pytest.raises(ValueError, match="active descendant"):
        replace(
            state,
            suppressed_ids=frozenset({source.record_id}),
            tombstoned_ids=frozenset({source.record_id}),
        )


def test_loaded_tombstone_cannot_borrow_an_unrelated_purge_retain() -> None:
    first_source = record("a")
    second_source = record("b")
    child = record(
        "c",
        record_sources=(first_source.record_id, second_source.record_id),
        processor=("merge", "1"),
    )
    review = proposal(
        "d",
        proposal_token="d",
        artifact_token="d",
        body={
            "kind": "purge_resolution_review",
            "purge_id": identifier("prg", "b"),
            "subject_kind": "record",
            "subject_id": child.record_id,
            "resolution": "retain",
            "replacement_record_id": None,
        },
    )
    approval = decision(
        "d",
        proposal_token="d",
        proposal_revision_token="d",
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(
        apply_batch(
            empty,
            batch(first_source, second_source, child, review, approval),
            snapshot_for(empty),
        )
    )
    target_second = purge_transition(
        "b",
        target_record_id=second_source.record_id,
        subject_kind="record",
        subject_id=second_source.record_id,
    )
    retain_child = purge_transition(
        "b",
        target_record_id=second_source.record_id,
        subject_kind="record",
        subject_id=child.record_id,
        resolution="retain",
        review_decision_id=approval.decision_id,
    )
    retained = materialize(
        apply_batch(
            state,
            batch(target_second, retain_child),
            snapshot_for(state),
        )
    )

    with pytest.raises(ValueError, match="active descendant"):
        replace(
            retained,
            suppressed_ids=frozenset(retained.suppressed_ids) | {first_source.record_id},
            tombstoned_ids=frozenset(retained.tombstoned_ids) | {first_source.record_id},
        )


def test_loaded_state_rejects_orphan_suppression_and_purge_pending_flags() -> None:
    value = stored_record(record("a"))
    state = BrainState.empty(BRAIN, records={value.record_id: value})

    with pytest.raises(ValueError, match="suppression lifecycle"):
        replace(state, suppressed_ids=frozenset({value.record_id}))
    with pytest.raises(ValueError, match="purge-pending lifecycle"):
        replace(
            state,
            suppressed_ids=frozenset({value.record_id}),
            purge_pending_ids=frozenset({value.record_id}),
        )


def test_accepted_revision_must_preserve_approved_proposal_provenance() -> None:
    source = record("a")
    genesis = revision("a")
    empty = BrainState.empty(BRAIN)
    state = materialize(
        apply_batch(empty, batch(source, genesis), snapshot_for(empty))
    )
    candidate = proposal(
        "b",
        proposal_token="b",
        base_revision_id=genesis.revision_id,
        source_records=(source.record_id,),
        body={"text": "accepted"},
    )
    approval = decision(
        "b",
        proposal_token="b",
        proposal_revision_token="b",
    )
    accepted = revision(
        "c",
        base_revision_id=genesis.revision_id,
        proposal_id=candidate.proposal_id,
        decision_id=approval.decision_id,
        body={"text": "accepted"},
    )

    with pytest.raises(LedgerTransitionError, match="accepted revision"):
        apply_batch(
            state,
            batch(candidate, approval, accepted),
            snapshot_for(state),
        )


def test_effect_receipt_is_an_explicit_purge_closure_subject() -> None:
    source = record("a")
    receipt = effect_receipt(
        "b",
        source_records=(source.record_id,),
        processor=("actuate", "1"),
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(source, receipt), snapshot_for(empty)))
    target = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    descendant = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="effect_receipt",
        subject_id=receipt.receipt_id,
    )

    application = apply_batch(state, batch(target, descendant), snapshot_for(state))
    next_state = materialize(application)

    assert set(next_state.purges[target.purge_id].resolutions) == {
        source.record_id,
        receipt.receipt_id,
    }
    assert receipt.receipt_id in next_state.tombstoned_ids
    assert receipt.receipt_id not in next_state.purge_pending_ids


def test_effect_reconciliation_rejects_tombstoned_ancestry_and_batch_races() -> None:
    source = record("a")
    unknown = effect_receipt(
        "b",
        source_records=(source.record_id,),
        processor=("actuate", "1"),
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(source, unknown), snapshot_for(empty)))
    poisoned = replace(
        state,
        suppressed_ids=frozenset({source.record_id, unknown.receipt_id}),
        tombstoned_ids=frozenset({source.record_id}),
    )
    first_terminal = effect_receipt(
        "c",
        outcome="succeeded",
        source_records=(source.record_id,),
        processor=("actuate", "1"),
        reconciles_receipt_id=unknown.receipt_id,
    )
    second_terminal = replace(first_terminal, receipt_id=identifier("rcp", "d"))

    with pytest.raises(LedgerTransitionError, match="tombstoned"):
        apply_batch(poisoned, batch(first_terminal), snapshot_for(poisoned))
    with pytest.raises(LedgerTransitionError, match="more than once"):
        apply_batch(
            state,
            batch(first_terminal, second_terminal),
            snapshot_for(state),
        )


def test_origin_supersession_requires_the_current_head_and_preserves_labels() -> None:
    first = record("a", labels=("private", "work"), origin_id="source:item:1")
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(first), snapshot_for(empty)))
    unbound = record("b", labels=("private", "work"), origin_id=first.origin_id)
    narrowed = record(
        "c",
        labels=("private",),
        origin_id=first.origin_id,
        record_sources=(first.record_id,),
    )
    valid = record(
        "d",
        labels=("private", "work"),
        origin_id=first.origin_id,
        record_sources=(first.record_id,),
    )

    with pytest.raises(LedgerTransitionError, match="current origin head"):
        apply_batch(state, batch(unbound), snapshot_for(state))
    with pytest.raises(LedgerTransitionError, match="compartments"):
        apply_batch(state, batch(narrowed), snapshot_for(state))

    next_state = materialize(apply_batch(state, batch(valid), snapshot_for(state)))
    assert first.origin_id is not None
    assert next_state.origin_heads[first.origin_id] == valid.record_id
    assert next_state.superseded_record_ids == frozenset({first.record_id})


def test_new_purge_requires_target_purge_and_rejects_target_retain() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(source, child), snapshot_for(empty)))
    child_only = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=child.record_id,
    )
    review = proposal(
        "c",
        proposal_token="c",
        artifact_token="c",
        body={
            "kind": "purge_resolution_review",
            "purge_id": identifier("prg", "b"),
            "subject_kind": "record",
            "subject_id": source.record_id,
            "resolution": "retain",
            "replacement_record_id": None,
        },
    )
    approval = decision(
        "c",
        proposal_token="c",
        proposal_revision_token="c",
    )
    retain_target = purge_transition(
        "b",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
        resolution="retain",
        review_decision_id=approval.decision_id,
    )

    with pytest.raises(LedgerTransitionError, match="initiating target"):
        apply_batch(state, batch(child_only), snapshot_for(state))
    with pytest.raises(LedgerTransitionError, match="initiating target"):
        apply_batch(
            state,
            batch(review, approval, retain_target),
            snapshot_for(state),
        )


def test_overlapping_purge_closures_are_rejected_in_both_item_orders() -> None:
    source = record("a")
    child = record(
        "b",
        record_sources=(source.record_id,),
        processor=("summary", "1"),
    )
    empty = BrainState.empty(BRAIN)
    state = materialize(apply_batch(empty, batch(source, child), snapshot_for(empty)))
    outer = purge_transition(
        "a",
        target_record_id=source.record_id,
        subject_kind="record",
        subject_id=source.record_id,
    )
    inner = purge_transition(
        "b",
        target_record_id=child.record_id,
        subject_kind="record",
        subject_id=child.record_id,
    )

    for items in ((outer, inner), (inner, outer)):
        with pytest.raises(LedgerTransitionError, match="closures overlap"):
            apply_batch(state, batch(*items), snapshot_for(state))


def test_discriminated_commit_refs_preserve_proposal_versions() -> None:
    first = proposal("a", proposal_token="a")
    second = replace(
        first,
        proposal_revision_id=identifier("rev", "b"),
        body={"text": "second"},
    )
    artifact = Artifact(BRAIN, first.artifact_id)
    committed = commit(
        item_refs=(
            ProposalItemRef(first.proposal_id, first.proposal_revision_id),
            ProposalItemRef(second.proposal_id, second.proposal_revision_id),
        )
    )
    receipt = ledger_receipt()
    binding = delivery_binding(receipt)

    state = BrainState.empty(
        BRAIN,
        artifacts={artifact.artifact_id: artifact},
        proposals={
            first.proposal_revision_id: first,
            second.proposal_revision_id: second,
        },
        proposal_heads={first.proposal_id: second.proposal_revision_id},
        commits={committed.commit_id: committed},
        receipts={receipt.receipt_id: receipt},
        deliveries={binding.delivery_id: binding},
    )

    assert tuple(state.commits[committed.commit_id].item_refs) == committed.item_refs


def test_batch_application_binds_exact_ordered_commit_references() -> None:
    first = record("a")
    second = record("b")
    empty = BrainState.empty(BRAIN)
    application = apply_batch(empty, batch(first, second), snapshot_for(empty))
    exact = commit(item_refs=application.batch.item_refs)

    assert application.bind_commit(exact) == exact

    unrelated = RecordItemRef(identifier("rec", "c"))
    invalid_references = (
        tuple(reversed(application.batch.item_refs)),
        application.batch.item_refs[:1],
        (*application.batch.item_refs, unrelated),
        (application.batch.item_refs[0], unrelated),
    )
    for item_refs in invalid_references:
        forged = commit(item_refs=item_refs)
        with pytest.raises(LedgerTransitionError, match="exactly bind"):
            application.bind_commit(forged)


def test_commit_evidence_rejects_duplicate_cursors_and_item_refs() -> None:
    first = stored_record(record("a"))
    second = stored_record(record("b"))
    first_commit = commit(item_refs=(RecordItemRef(first.record_id),))
    second_commit_same_cursor = commit(
        delivery_token="b",
        item_refs=(RecordItemRef(second.record_id),),
    )
    first_receipt = ledger_receipt()
    second_receipt_same_cursor = ledger_receipt(
        delivery_token="b",
        receipt_token="b",
    )
    first_binding = delivery_binding(first_receipt)
    second_binding_same_cursor = delivery_binding(second_receipt_same_cursor)

    with pytest.raises(ValueError, match="cursors"):
        BrainState.empty(
            BRAIN,
            records={first.record_id: first, second.record_id: second},
            commits={
                first_commit.commit_id: first_commit,
                second_commit_same_cursor.commit_id: second_commit_same_cursor,
            },
            receipts={
                first_receipt.receipt_id: first_receipt,
                second_receipt_same_cursor.receipt_id: second_receipt_same_cursor,
            },
            deliveries={
                first_binding.delivery_id: first_binding,
                second_binding_same_cursor.delivery_id: second_binding_same_cursor,
            },
        )

    second_cursor = "cur_v1_bbbbbbbbbbbbbbbb"
    second_commit = commit(
        delivery_token="b",
        cursor=second_cursor,
        item_refs=(RecordItemRef(first.record_id),),
    )
    second_receipt = ledger_receipt(
        delivery_token="b",
        receipt_token="b",
        cursor=second_cursor,
    )
    second_binding = delivery_binding(second_receipt)
    with pytest.raises(ValueError, match="more than one commit"):
        BrainState.empty(
            BRAIN,
            records={first.record_id: first},
            commits={
                first_commit.commit_id: first_commit,
                second_commit.commit_id: second_commit,
            },
            receipts={
                first_receipt.receipt_id: first_receipt,
                second_receipt.receipt_id: second_receipt,
            },
            deliveries={
                first_binding.delivery_id: first_binding,
                second_binding.delivery_id: second_binding,
            },
        )


def test_commit_receipt_and_delivery_evidence_is_bijective() -> None:
    value = stored_record(record("a"))
    committed = commit(item_refs=(RecordItemRef(value.record_id),))
    receipt = ledger_receipt()

    with pytest.raises(ValueError, match="one-to-one"):
        BrainState.empty(
            BRAIN,
            records={value.record_id: value},
            commits={committed.commit_id: committed},
            receipts={receipt.receipt_id: receipt},
        )
