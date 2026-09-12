from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Literal

from open_brain_engine.ledger.model import (
    ActiveLabelSetSnapshot,
    BrainState,
    Commit,
    CommitBatch,
    Decision,
    DeliveryBinding,
    EffectReceipt,
    FrozenJson,
    LedgerItem,
    LedgerItemRef,
    LedgerReceipt,
    ProcessorIdentity,
    Proposal,
    Provenance,
    PurgeTransition,
    Record,
    Revision,
    SchemaReference,
)
from open_brain_engine.ledger.transitions import BatchApplication

BRAIN = "brn_aaaaaaaaaaaaaaaaaaaaaaaaaa"
PRINCIPAL = "pri_aaaaaaaaaaaaaaaaaaaaaaaaaa"
POLICY_DIGEST = "a" * 64
TIMESTAMP = "2026-09-06T12:00:00Z"
SCHEMA = SchemaReference(
    schema_id="note",
    version=1,
    uri="urn:open-brain:schema:note:v1",
    sha256="a" * 64,
)


def identifier(prefix: str, token: str) -> str:
    return f"{prefix}_{token * 26}"


def provenance(
    *,
    record_sources: Iterable[str] = (),
    revision_sources: Iterable[str] = (),
    processor: ProcessorIdentity | tuple[str, str] | None = None,
    derivation: Literal["owner", "import", "processor", "replacement"] | None = None,
) -> Provenance:
    return Provenance(
        brain_id=BRAIN,
        source_record_ids=record_sources,
        source_revision_ids=revision_sources,
        processor=processor,
        derivation=derivation or ("processor" if processor is not None else "owner"),
    )


def record(
    token: str,
    *,
    labels: Iterable[str] = ("private",),
    record_sources: Iterable[str] = (),
    revision_sources: Iterable[str] = (),
    processor: ProcessorIdentity | tuple[str, str] | None = None,
    derivation: Literal["owner", "import", "processor", "replacement"] | None = None,
    origin_id: str | None = None,
    body: Mapping[str, object] | None = None,
    ciphertext_state: Literal["pending", "verified", "unknown_historical"] = "pending",
    ciphertext_digest: str | None = None,
) -> Record:
    return Record(
        brain_id=BRAIN,
        record_id=identifier("rec", token),
        record_type="note",
        content_schema=SCHEMA,
        producer_principal_id=PRINCIPAL,
        origin_id=origin_id,
        captured_at=TIMESTAMP,
        observed_at=TIMESTAMP,
        compartments=labels,
        provenance=provenance(
            record_sources=record_sources,
            revision_sources=revision_sources,
            processor=processor,
            derivation=derivation,
        ),
        body={"text": token} if body is None else body,
        ciphertext_state=ciphertext_state,
        ciphertext_digest=ciphertext_digest,
    )


def stored_record(value: Record, *, digest: str = "b" * 64) -> Record:
    return replace(value, ciphertext_state="verified", ciphertext_digest=digest)


def revision(
    token: str,
    *,
    artifact_token: str = "a",
    base_revision_id: str | None = None,
    proposal_id: str | None = None,
    decision_id: str | None = None,
    labels: Iterable[str] = ("private",),
    source_records: Iterable[str] = (),
    source_revisions: Iterable[str] = (),
    processor: ProcessorIdentity | tuple[str, str] | None = None,
    derivation: Literal["owner", "import", "processor", "replacement"] | None = None,
    body: Mapping[str, object] | None = None,
) -> Revision:
    return Revision(
        brain_id=BRAIN,
        artifact_id=identifier("art", artifact_token),
        revision_id=identifier("rev", token),
        base_revision_id=base_revision_id,
        accepted_from_proposal_id=proposal_id,
        decision_id=decision_id,
        content_schema=SCHEMA,
        compartments=labels,
        provenance=provenance(
            record_sources=source_records,
            revision_sources=source_revisions,
            processor=processor,
            derivation=derivation,
        ),
        body={"text": token} if body is None else body,
        created_at=TIMESTAMP,
    )


def proposal(
    token: str,
    *,
    proposal_token: str | None = None,
    artifact_token: str = "a",
    base_revision_id: str | None = None,
    labels: Iterable[str] = ("private",),
    source_records: Iterable[str] = (),
    source_revisions: Iterable[str] = (),
    processor: ProcessorIdentity | tuple[str, str] | None = None,
    derivation: Literal["owner", "import", "processor", "replacement"] | None = None,
    body: Mapping[str, object] | None = None,
) -> Proposal:
    proposal_token = proposal_token or token
    return Proposal(
        brain_id=BRAIN,
        proposal_id=identifier("prp", proposal_token),
        proposal_revision_id=identifier("rev", token),
        artifact_id=identifier("art", artifact_token),
        base_revision_id=base_revision_id,
        content_schema=SCHEMA,
        compartments=labels,
        provenance=provenance(
            record_sources=source_records,
            revision_sources=source_revisions,
            processor=processor,
            derivation=derivation,
        ),
        body={"text": token} if body is None else body,
        proposed_at=TIMESTAMP,
    )


def decision(
    token: str,
    *,
    proposal_token: str = "a",
    proposal_revision_token: str = "a",
    outcome: Literal["accepted", "rejected"] = "accepted",
    labels: Iterable[str] = ("private",),
) -> Decision:
    return Decision(
        brain_id=BRAIN,
        decision_id=identifier("dec", token),
        proposal_id=identifier("prp", proposal_token),
        expected_proposal_revision_id=identifier("rev", proposal_revision_token),
        outcome=outcome,
        decided_by=PRINCIPAL,
        compartments=labels,
        decided_at=TIMESTAMP,
    )


def effect_receipt(
    receipt_token: str,
    *,
    effect_token: str = "a",
    reconciles_receipt_id: str | None = None,
    external_identity: str = "synthetic-effect",
    outcome: Literal["succeeded", "failed", "unknown"] = "unknown",
    labels: Iterable[str] = ("private",),
    source_records: Iterable[str] = (),
    source_revisions: Iterable[str] = (),
    processor: ProcessorIdentity | tuple[str, str] | None = None,
    derivation: Literal["owner", "import", "processor", "replacement"] | None = None,
) -> EffectReceipt:
    return EffectReceipt(
        brain_id=BRAIN,
        receipt_id=identifier("rcp", receipt_token),
        effect_id=identifier("eff", effect_token),
        reconciles_receipt_id=reconciles_receipt_id,
        external_identity=external_identity,
        outcome=outcome,
        compartments=labels,
        provenance=provenance(
            record_sources=source_records,
            revision_sources=source_revisions,
            processor=processor,
            derivation=derivation,
        ),
        observed_at=TIMESTAMP,
    )


def purge_transition(
    token: str,
    *,
    target_record_id: str,
    subject_kind: Literal["record", "revision", "proposal", "effect_receipt"],
    subject_id: str,
    resolution: Literal["purge", "retain", "replace"] = "purge",
    replacement_record_id: str | None = None,
    review_decision_id: str | None = None,
) -> PurgeTransition:
    return PurgeTransition(
        brain_id=BRAIN,
        purge_id=identifier("prg", token),
        target_record_id=target_record_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        resolution=resolution,
        replacement_record_id=replacement_record_id,
        review_decision_id=review_decision_id,
        reason="owner-requested synthetic purge",
    )


def batch(
    *items: LedgerItem,
    digest: str = "a" * 64,
    delivery_token: str = "a",
) -> CommitBatch:
    return CommitBatch(
        brain_id=BRAIN,
        delivery_id=identifier("dlv", delivery_token),
        digest=digest,
        sequencer_epoch=1,
        issuer_epoch=1,
        policy_digest=POLICY_DIGEST,
        items=items,
    )


def commit(
    *,
    digest: str = "a" * 64,
    delivery_token: str = "a",
    cursor: str = "cur_v1_aaaaaaaaaaaaaaaa",
    item_refs: Iterable[LedgerItemRef],
) -> Commit:
    return Commit(
        brain_id=BRAIN,
        commit_id=identifier("cmt", delivery_token),
        delivery_id=identifier("dlv", delivery_token),
        cursor=cursor,
        digest=digest,
        issuer_epoch=1,
        policy_digest=POLICY_DIGEST,
        sequencer_epoch=1,
        item_refs=item_refs,
    )


def ledger_receipt(
    *,
    digest: str = "a" * 64,
    delivery_token: str = "a",
    receipt_token: str = "a",
    cursor: str = "cur_v1_aaaaaaaaaaaaaaaa",
) -> LedgerReceipt:
    node_key_id = identifier("key", "b")
    owner_key_id = identifier("key", "a")
    return LedgerReceipt(
        brain_id=BRAIN,
        receipt_id=identifier("rcp", receipt_token),
        commit_id=identifier("cmt", delivery_token),
        delivery_id=identifier("dlv", delivery_token),
        cursor=cursor,
        commit_digest=digest,
        issuer_epoch=1,
        policy_digest=POLICY_DIGEST,
        sequencer_epoch=1,
        node_key_id=node_key_id,
        node_epoch_certificate={
            "schema_version": 1,
            "brain_id": BRAIN,
            "node_id": identifier("nod", "a"),
            "node_key_id": node_key_id,
            "node_public_key": "5_FioQvsVZr-oZXk3OhLaVaNXSywlj60RsBoXisX8vA",
            "sequencer_epoch": 1,
            "prior_ledger_head": None,
            "owner_key_id": owner_key_id,
            "issued_at": TIMESTAMP,
            "owner_signature": (
                "36ogpGPe6vCLXEKCnpGK1sZHIVv6nsUeMHI78t-6h4QzIn3ki8VnWGKQMioXyl1jqPvk4oyYf22pJExFRxg3BQ"
            ),
        },
        owner_key_history=(
            {
                "schema_version": 1,
                "brain_id": BRAIN,
                "owner_key_id": owner_key_id,
                "owner_public_key": "ebVWLo_mVPlAeLES6KmLp5AfhTrmlb7X4OORC60ElmQ",
                "owner_epoch": 1,
                "previous_owner_key_id": None,
                "valid_from": TIMESTAMP,
                "retired_at": None,
                "certifying_owner_key_id": owner_key_id,
                "owner_signature": (
                    "edEbtXNcyvOW0IoU-9BfcRPoNVVY5WKfLQ05i2mxbyU6vxBnOF7ATCw-IN1P48FLeXe7DD5Tu-l6--D1NB1ZBg"
                ),
            },
        ),
        issued_at=TIMESTAMP,
        node_signature=(
            "lSW19I1K8AfbWiE8I4qW_9Z22DwaxcXxmspM_27v9bq5E_OsoGQ3MeE1hZcJVPdhMrBTpjpGWUHUKn5nP0eXAg"
        ),
    )


def delivery_binding(receipt: LedgerReceipt, *, digest: str | None = None) -> DeliveryBinding:
    return DeliveryBinding(
        brain_id=BRAIN,
        delivery_id=receipt.delivery_id,
        digest=receipt.commit_digest if digest is None else digest,
        receipt=receipt,
    )


def snapshot_for(state: BrainState) -> ActiveLabelSetSnapshot:
    inactive = (
        frozenset(state.superseded_record_ids)
        | frozenset(state.suppressed_ids)
        | frozenset(state.tombstoned_ids)
        | frozenset(state.purge_pending_ids)
    )
    counts: dict[tuple[str, ...], int] = {}
    for record_id, value in state.records.items():
        if record_id in inactive:
            continue
        labels = tuple(value.compartments)
        counts[labels] = counts.get(labels, 0) + 1
    return ActiveLabelSetSnapshot(state.brain_id, counts)


def materialize(application: BatchApplication) -> BrainState:
    accepted = (stored_record(value) for value in application.new_records)
    return application.to_state(accepted)


def json_body(value: Mapping[str, FrozenJson]) -> Mapping[str, FrozenJson]:
    return value
