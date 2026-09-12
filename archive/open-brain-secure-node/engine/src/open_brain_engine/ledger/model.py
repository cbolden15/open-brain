"""Immutable, transport-free values for the Brain Protocol v1 ledger kernel."""

from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Literal, cast

from open_brain_engine.protocol.freeze import RESOURCE_LIMITS
from open_brain_engine.protocol.identifiers import validate_identifier

from .policy import canonical_compartments, propagated_compartments

type Derivation = Literal["owner", "import", "processor", "replacement"]
type EffectOutcome = Literal["succeeded", "failed", "unknown"]
type PurgeResolution = Literal["purge", "retain", "replace"]
type PurgeSubjectKind = Literal["record", "revision", "proposal", "effect_receipt"]
type FrozenJson = (
    None
    | bool
    | int
    | float
    | str
    | tuple[FrozenJson, ...]
    | Mapping[str, FrozenJson]
)
type ResourceCode = Literal[
    "active_label_set_limit",
    "commit_batch_limit",
    "label_limit",
    "records_per_label_set_limit",
]

_DIGEST = re.compile(r"[0-9a-f]{64}")
_CURSOR = re.compile(r"cur_v1_[A-Za-z0-9_-]{16,512}")
_PROCESSOR_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")


class ResourceLimitError(ValueError):
    """A frozen Brain-scoped resource limit would be exceeded."""

    def __init__(
        self,
        code: ResourceCode,
        *,
        observed: int,
        limit: int,
        corrective_action: str,
    ) -> None:
        self.schema_version: Literal[1] = 1
        self.code = code
        self.message = f"{code}: observed {observed}, limit {limit}"
        self.retry_safe = False
        self.retry_after_ms: None = None
        self.corrective_action = corrective_action
        self.accounting_scope: Literal["brain"] = "brain"
        self.principal_id: None = None
        self.limit = limit
        self.observed = observed
        super().__init__(self.message)


def _frozen_mapping[K, V](value: Mapping[K, V] | None) -> Mapping[K, V]:
    return MappingProxyType(dict(value or {}))


def _freeze_json(value: object) -> FrozenJson:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("semantic JSON cannot contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJson] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("semantic JSON object names must be strings")
            frozen[key] = _freeze_json(child)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child) for child in value)
    raise ValueError("semantic JSON contains an unsupported value")


def _freeze_json_object(
    value: Mapping[str, object] | Mapping[str, FrozenJson],
) -> Mapping[str, FrozenJson]:
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - narrowed by the public type.
        raise ValueError("semantic JSON body must be an object")
    return frozen


def _nonempty(value: str, label: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value or (maximum is not None and len(value) > maximum):
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: str, label: str = "digest") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _cursor(value: str) -> str:
    if not isinstance(value, str) or _CURSOR.fullmatch(value) is None:
        raise ValueError("cursor must be opaque and Brain-scoped")
    return value


def _positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validate_scoped_id(value: str, role: str, brain_id: str) -> str:
    return validate_identifier(value, role=role, brain_id=brain_id)  # type: ignore[arg-type]


def _labels(value: Iterable[str]) -> tuple[str, ...]:
    labels = canonical_compartments(value)
    if len(labels) > RESOURCE_LIMITS.labels_per_record:
        raise ResourceLimitError(
            "label_limit",
            observed=len(labels),
            limit=RESOURCE_LIMITS.labels_per_record,
            corrective_action="reduce_compartments",
        )
    return labels


@dataclass(frozen=True, slots=True)
class SchemaReference:
    schema_id: str
    version: int
    uri: str
    sha256: str

    def __post_init__(self) -> None:
        _nonempty(self.schema_id, "schema_id", maximum=128)
        _positive(self.version, "schema version")
        _nonempty(self.uri, "schema URI")
        _digest(self.sha256, "schema digest")


@dataclass(frozen=True, slots=True)
class ProcessorIdentity:
    processor_id: str
    version: str

    def __post_init__(self) -> None:
        if _PROCESSOR_ID.fullmatch(self.processor_id) is None:
            raise ValueError("processor_id is invalid")
        _nonempty(self.version, "processor version", maximum=64)


@dataclass(frozen=True, slots=True)
class Provenance:
    brain_id: str
    source_record_ids: tuple[str, ...] | Iterable[str] = ()
    source_revision_ids: tuple[str, ...] | Iterable[str] = ()
    processor: ProcessorIdentity | tuple[str, str] | None = None
    derivation: Derivation = "owner"
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        if self.schema_version != 1:
            raise ValueError("unsupported provenance schema version")
        if self.derivation not in {"owner", "import", "processor", "replacement"}:
            raise ValueError("invalid provenance derivation")
        raw_records = tuple(self.source_record_ids)
        raw_revisions = tuple(self.source_revision_ids)
        if (
            len(set(raw_records)) != len(raw_records)
            or len(set(raw_revisions)) != len(raw_revisions)
        ):
            raise ValueError("provenance sources must be unique")
        records = tuple(sorted(raw_records))
        revisions = tuple(sorted(raw_revisions))
        object.__setattr__(self, "source_record_ids", records)
        object.__setattr__(self, "source_revision_ids", revisions)
        for record_id in records:
            _validate_scoped_id(record_id, "record", self.brain_id)
        for revision_id in revisions:
            _validate_scoped_id(revision_id, "revision", self.brain_id)
        processor = self.processor
        if processor is not None:
            if isinstance(processor, ProcessorIdentity):
                processor = replace(processor)
            else:
                values = tuple(processor)
                if len(values) != 2:
                    raise ValueError("processor identity must contain an id and version")
                processor = ProcessorIdentity(values[0], values[1])
            object.__setattr__(self, "processor", processor)
        sources = records + revisions
        if self.derivation == "processor":
            if processor is None or not sources:
                raise ValueError("processor provenance requires processor identity and sources")
        elif processor is not None:
            raise ValueError("only processor provenance may name a processor")
        if self.derivation == "replacement" and not sources:
            raise ValueError("replacement provenance requires sources")

    @property
    def source_ids(self) -> tuple[str, ...]:
        return cast(tuple[str, ...], self.source_record_ids) + cast(
            tuple[str, ...], self.source_revision_ids
        )


@dataclass(frozen=True, slots=True)
class Record:
    brain_id: str
    record_id: str
    record_type: str
    content_schema: SchemaReference
    producer_principal_id: str
    origin_id: str | None
    captured_at: str | None
    observed_at: str
    compartments: tuple[str, ...] | Iterable[str]
    provenance: Provenance
    body: Mapping[str, FrozenJson] | Mapping[str, object]
    ciphertext_state: Literal["pending", "verified", "unknown_historical"]
    ciphertext_digest: str | None
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.record_id, "record", self.brain_id)
        _nonempty(self.record_type, "record_type", maximum=64)
        if not isinstance(self.content_schema, SchemaReference):
            raise ValueError("record content_schema is invalid")
        object.__setattr__(self, "content_schema", replace(self.content_schema))
        _validate_scoped_id(self.producer_principal_id, "principal", self.brain_id)
        if self.origin_id is not None:
            _nonempty(self.origin_id, "origin_id", maximum=256)
        if self.captured_at is not None:
            _nonempty(self.captured_at, "captured_at")
        _nonempty(self.observed_at, "observed_at")
        object.__setattr__(self, "compartments", _labels(self.compartments))
        if not isinstance(self.provenance, Provenance):
            raise ValueError("record provenance is invalid")
        provenance = replace(self.provenance)
        if provenance.brain_id != self.brain_id:
            raise ValueError("record provenance crosses Brain boundary")
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "body", _freeze_json_object(self.body))
        if self.ciphertext_state not in {"pending", "verified", "unknown_historical"}:
            raise ValueError("record ciphertext state is invalid")
        if self.ciphertext_state == "verified":
            if self.ciphertext_digest is None:
                raise ValueError("verified record requires ciphertext digest")
            _digest(self.ciphertext_digest, "ciphertext digest")
        elif self.ciphertext_digest is not None:
            raise ValueError("pending or historical-unknown record cannot carry ciphertext digest")
        if self.schema_version != 1:
            raise ValueError("unsupported record schema version")


@dataclass(frozen=True, slots=True)
class Artifact:
    brain_id: str
    artifact_id: str

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.artifact_id, "artifact", self.brain_id)


@dataclass(frozen=True, slots=True)
class Revision:
    brain_id: str
    artifact_id: str
    revision_id: str
    base_revision_id: str | None
    accepted_from_proposal_id: str | None
    decision_id: str | None
    content_schema: SchemaReference
    compartments: tuple[str, ...] | Iterable[str]
    provenance: Provenance
    body: Mapping[str, FrozenJson] | Mapping[str, object]
    created_at: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.artifact_id, "artifact", self.brain_id)
        _validate_scoped_id(self.revision_id, "revision", self.brain_id)
        if self.base_revision_id is not None:
            _validate_scoped_id(self.base_revision_id, "revision", self.brain_id)
        if (self.accepted_from_proposal_id is None) != (self.decision_id is None):
            raise ValueError("revision proposal and decision links must appear together")
        if self.accepted_from_proposal_id is not None:
            _validate_scoped_id(self.accepted_from_proposal_id, "proposal", self.brain_id)
            _validate_scoped_id(cast(str, self.decision_id), "decision", self.brain_id)
        if not isinstance(self.content_schema, SchemaReference):
            raise ValueError("revision content_schema is invalid")
        object.__setattr__(self, "content_schema", replace(self.content_schema))
        object.__setattr__(self, "compartments", _labels(self.compartments))
        if not isinstance(self.provenance, Provenance):
            raise ValueError("revision provenance is invalid")
        provenance = replace(self.provenance)
        if provenance.brain_id != self.brain_id:
            raise ValueError("revision provenance crosses Brain boundary")
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "body", _freeze_json_object(self.body))
        _nonempty(self.created_at, "created_at")
        if self.schema_version != 1:
            raise ValueError("unsupported revision schema version")


@dataclass(frozen=True, slots=True)
class Proposal:
    brain_id: str
    proposal_id: str
    proposal_revision_id: str
    artifact_id: str
    base_revision_id: str | None
    content_schema: SchemaReference
    compartments: tuple[str, ...] | Iterable[str]
    provenance: Provenance
    body: Mapping[str, FrozenJson] | Mapping[str, object]
    proposed_at: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.proposal_id, "proposal", self.brain_id)
        _validate_scoped_id(self.proposal_revision_id, "revision", self.brain_id)
        _validate_scoped_id(self.artifact_id, "artifact", self.brain_id)
        if self.base_revision_id is not None:
            _validate_scoped_id(self.base_revision_id, "revision", self.brain_id)
        if not isinstance(self.content_schema, SchemaReference):
            raise ValueError("proposal content_schema is invalid")
        object.__setattr__(self, "content_schema", replace(self.content_schema))
        object.__setattr__(self, "compartments", _labels(self.compartments))
        if not isinstance(self.provenance, Provenance):
            raise ValueError("proposal provenance is invalid")
        provenance = replace(self.provenance)
        if provenance.brain_id != self.brain_id:
            raise ValueError("proposal provenance crosses Brain boundary")
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "body", _freeze_json_object(self.body))
        _nonempty(self.proposed_at, "proposed_at")
        if self.schema_version != 1:
            raise ValueError("unsupported proposal schema version")


@dataclass(frozen=True, slots=True)
class Decision:
    brain_id: str
    decision_id: str
    proposal_id: str
    expected_proposal_revision_id: str
    outcome: Literal["accepted", "rejected"]
    decided_by: str
    compartments: tuple[str, ...] | Iterable[str]
    decided_at: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.decision_id, "decision", self.brain_id)
        _validate_scoped_id(self.proposal_id, "proposal", self.brain_id)
        _validate_scoped_id(self.expected_proposal_revision_id, "revision", self.brain_id)
        if self.outcome not in {"accepted", "rejected"}:
            raise ValueError("decision outcome is invalid")
        _validate_scoped_id(self.decided_by, "principal", self.brain_id)
        object.__setattr__(self, "compartments", _labels(self.compartments))
        _nonempty(self.decided_at, "decided_at")
        if self.schema_version != 1:
            raise ValueError("unsupported decision schema version")


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    brain_id: str
    receipt_id: str
    effect_id: str
    reconciles_receipt_id: str | None
    external_identity: str
    outcome: EffectOutcome
    compartments: tuple[str, ...] | Iterable[str]
    provenance: Provenance
    observed_at: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.receipt_id, "receipt", self.brain_id)
        _validate_scoped_id(self.effect_id, "effect", self.brain_id)
        if self.reconciles_receipt_id is not None:
            _validate_scoped_id(self.reconciles_receipt_id, "receipt", self.brain_id)
            if self.reconciles_receipt_id == self.receipt_id:
                raise ValueError("an effect receipt cannot reconcile itself")
        _nonempty(self.external_identity, "external_identity", maximum=256)
        if self.outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("effect outcome is invalid")
        if self.outcome == "unknown" and self.reconciles_receipt_id is not None:
            raise ValueError("an unknown effect receipt cannot be a reconciliation")
        object.__setattr__(self, "compartments", _labels(self.compartments))
        if not isinstance(self.provenance, Provenance):
            raise ValueError("effect provenance is invalid")
        provenance = replace(self.provenance)
        if provenance.brain_id != self.brain_id:
            raise ValueError("effect provenance crosses Brain boundary")
        object.__setattr__(self, "provenance", provenance)
        _nonempty(self.observed_at, "observed_at")
        if self.schema_version != 1:
            raise ValueError("unsupported effect receipt schema version")


@dataclass(frozen=True, slots=True)
class Effect:
    brain_id: str
    effect_id: str
    external_identity: str
    receipts: tuple[EffectReceipt, ...] | Iterable[EffectReceipt]

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.effect_id, "effect", self.brain_id)
        _nonempty(self.external_identity, "external_identity", maximum=256)
        receipts: list[EffectReceipt] = []
        for supplied in self.receipts:
            if not isinstance(supplied, EffectReceipt):
                raise ValueError("effect receipts are invalid")
            receipts.append(replace(supplied))
        typed_receipts = tuple(receipts)
        if not typed_receipts:
            raise ValueError("effect requires at least one receipt")
        if len({receipt.receipt_id for receipt in typed_receipts}) != len(typed_receipts):
            raise ValueError("effect receipt identities must be unique")
        if any(
            receipt.brain_id != self.brain_id
            or receipt.effect_id != self.effect_id
            or receipt.external_identity != self.external_identity
            for receipt in typed_receipts
        ):
            raise ValueError("effect receipt binding is invalid")
        first = typed_receipts[0]
        if first.reconciles_receipt_id is not None:
            raise ValueError("an initial effect receipt cannot reconcile another receipt")
        for previous, current in zip(typed_receipts, typed_receipts[1:], strict=False):
            if (
                previous.outcome != "unknown"
                or current.outcome == "unknown"
                or current.reconciles_receipt_id != previous.receipt_id
                or current.compartments != previous.compartments
                or current.provenance != previous.provenance
            ):
                raise ValueError("effect reconciliation history is invalid")
        object.__setattr__(self, "receipts", typed_receipts)

    @property
    def current(self) -> EffectReceipt:
        return cast(tuple[EffectReceipt, ...], self.receipts)[-1]


@dataclass(frozen=True, slots=True)
class PurgeTransition:
    brain_id: str
    purge_id: str
    target_record_id: str
    subject_kind: PurgeSubjectKind
    subject_id: str
    resolution: PurgeResolution
    replacement_record_id: str | None
    review_decision_id: str | None
    reason: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.purge_id, "purge", self.brain_id)
        _validate_scoped_id(self.target_record_id, "record", self.brain_id)
        if self.subject_kind not in {"record", "revision", "proposal", "effect_receipt"}:
            raise ValueError("purge subject kind is invalid")
        subject_role = "receipt" if self.subject_kind == "effect_receipt" else self.subject_kind
        _validate_scoped_id(self.subject_id, subject_role, self.brain_id)
        if self.resolution not in {"purge", "retain", "replace"}:
            raise ValueError("purge resolution is invalid")
        if self.replacement_record_id is not None:
            _validate_scoped_id(self.replacement_record_id, "record", self.brain_id)
        if self.review_decision_id is not None:
            _validate_scoped_id(self.review_decision_id, "decision", self.brain_id)
        if self.resolution == "purge":
            if self.replacement_record_id is not None or self.review_decision_id is not None:
                raise ValueError("purge resolution cannot carry review or replacement")
        elif self.resolution == "retain":
            if self.replacement_record_id is not None or self.review_decision_id is None:
                raise ValueError("retain resolution requires review and no replacement")
        elif self.replacement_record_id is None or self.review_decision_id is None:
            raise ValueError("replace resolution requires review and replacement")
        if self.subject_kind == "effect_receipt" and self.resolution == "replace":
            raise ValueError("an effect receipt cannot be replaced by a record")
        _nonempty(self.reason, "purge reason", maximum=256)
        if self.schema_version != 1:
            raise ValueError("unsupported purge-transition schema version")


type LedgerItem = Record | Revision | Proposal | Decision | EffectReceipt | PurgeTransition


@dataclass(frozen=True, slots=True)
class RecordItemRef:
    record_id: str
    item_kind: Literal["record"] = field(default="record", init=False)


@dataclass(frozen=True, slots=True)
class ProposalItemRef:
    proposal_id: str
    proposal_revision_id: str
    item_kind: Literal["proposal"] = field(default="proposal", init=False)


@dataclass(frozen=True, slots=True)
class RevisionItemRef:
    revision_id: str
    item_kind: Literal["revision"] = field(default="revision", init=False)


@dataclass(frozen=True, slots=True)
class DecisionItemRef:
    decision_id: str
    item_kind: Literal["decision"] = field(default="decision", init=False)


@dataclass(frozen=True, slots=True)
class PurgeTransitionItemRef:
    purge_id: str
    subject_kind: PurgeSubjectKind
    subject_id: str
    item_kind: Literal["purge_transition"] = field(default="purge_transition", init=False)


@dataclass(frozen=True, slots=True)
class EffectReceiptItemRef:
    receipt_id: str
    item_kind: Literal["effect_receipt"] = field(default="effect_receipt", init=False)


type LedgerItemRef = (
    RecordItemRef
    | ProposalItemRef
    | RevisionItemRef
    | DecisionItemRef
    | PurgeTransitionItemRef
    | EffectReceiptItemRef
)


def item_reference(item: LedgerItem) -> LedgerItemRef:
    if isinstance(item, Record):
        return RecordItemRef(item.record_id)
    if isinstance(item, Revision):
        return RevisionItemRef(item.revision_id)
    if isinstance(item, Proposal):
        return ProposalItemRef(item.proposal_id, item.proposal_revision_id)
    if isinstance(item, Decision):
        return DecisionItemRef(item.decision_id)
    if isinstance(item, EffectReceipt):
        return EffectReceiptItemRef(item.receipt_id)
    return PurgeTransitionItemRef(item.purge_id, item.subject_kind, item.subject_id)


def lineage_identifier(item: Record | Revision | Proposal | EffectReceipt) -> str:
    if isinstance(item, Record):
        return item.record_id
    if isinstance(item, Revision):
        return item.revision_id
    if isinstance(item, Proposal):
        return item.proposal_revision_id
    return item.receipt_id


@dataclass(frozen=True, slots=True)
class CommitBatch:
    brain_id: str
    delivery_id: str
    digest: str
    sequencer_epoch: int
    issuer_epoch: int
    policy_digest: str
    items: tuple[LedgerItem, ...] | Iterable[LedgerItem]
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.delivery_id, "delivery", self.brain_id)
        _digest(self.digest, "commit digest")
        _positive(self.sequencer_epoch, "sequencer_epoch")
        _positive(self.issuer_epoch, "issuer_epoch")
        _digest(self.policy_digest, "policy digest")
        items = tuple(self.items)
        object.__setattr__(self, "items", items)
        if not items:
            raise ValueError("commit batch must contain items")
        if len(items) > RESOURCE_LIMITS.commit_batch_items:
            raise ResourceLimitError(
                "commit_batch_limit",
                observed=len(items),
                limit=RESOURCE_LIMITS.commit_batch_items,
                corrective_action="split_commit_batch",
            )
        item_types = (Record, Revision, Proposal, Decision, EffectReceipt, PurgeTransition)
        if any(not isinstance(item, item_types) for item in items):
            raise ValueError("commit batch contains an unknown item")
        if any(item.brain_id != self.brain_id for item in items):
            raise ValueError("commit batch crosses Brain boundary")
        if self.schema_version != 1:
            raise ValueError("unsupported commit-batch schema version")

    @property
    def item_refs(self) -> tuple[LedgerItemRef, ...]:
        return tuple(item_reference(item) for item in self.items)


@dataclass(frozen=True, slots=True)
class Commit:
    brain_id: str
    commit_id: str
    delivery_id: str
    cursor: str
    digest: str
    issuer_epoch: int
    policy_digest: str
    sequencer_epoch: int
    item_refs: tuple[LedgerItemRef, ...] | Iterable[LedgerItemRef]

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.commit_id, "commit", self.brain_id)
        _validate_scoped_id(self.delivery_id, "delivery", self.brain_id)
        _cursor(self.cursor)
        _digest(self.digest, "commit digest")
        _positive(self.issuer_epoch, "issuer_epoch")
        _digest(self.policy_digest, "policy digest")
        _positive(self.sequencer_epoch, "sequencer_epoch")
        references = tuple(
            _validated_item_reference(reference, self.brain_id) for reference in self.item_refs
        )
        if not references or len(set(references)) != len(references):
            raise ValueError("commit item references must be nonempty and unique")
        object.__setattr__(self, "item_refs", references)


def _validated_item_reference(reference: LedgerItemRef, brain_id: str) -> LedgerItemRef:
    if isinstance(reference, RecordItemRef):
        _validate_scoped_id(reference.record_id, "record", brain_id)
    elif isinstance(reference, ProposalItemRef):
        _validate_scoped_id(reference.proposal_id, "proposal", brain_id)
        _validate_scoped_id(reference.proposal_revision_id, "revision", brain_id)
    elif isinstance(reference, RevisionItemRef):
        _validate_scoped_id(reference.revision_id, "revision", brain_id)
    elif isinstance(reference, DecisionItemRef):
        _validate_scoped_id(reference.decision_id, "decision", brain_id)
    elif isinstance(reference, PurgeTransitionItemRef):
        _validate_scoped_id(reference.purge_id, "purge", brain_id)
        if reference.subject_kind not in {
            "record",
            "revision",
            "proposal",
            "effect_receipt",
        }:
            raise ValueError("purge item reference subject kind is invalid")
        subject_role = (
            "receipt" if reference.subject_kind == "effect_receipt" else reference.subject_kind
        )
        _validate_scoped_id(reference.subject_id, subject_role, brain_id)
    elif isinstance(reference, EffectReceiptItemRef):
        _validate_scoped_id(reference.receipt_id, "receipt", brain_id)
    else:
        raise ValueError("commit contains an unknown item reference")
    return replace(reference)


@dataclass(frozen=True, slots=True)
class LedgerReceipt:
    brain_id: str
    receipt_id: str
    commit_id: str
    delivery_id: str
    cursor: str
    commit_digest: str
    issuer_epoch: int
    policy_digest: str
    sequencer_epoch: int
    node_key_id: str
    node_epoch_certificate: Mapping[str, FrozenJson] | Mapping[str, object]
    owner_key_history: tuple[Mapping[str, FrozenJson], ...] | Iterable[Mapping[str, object]]
    issued_at: str
    node_signature: str
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.receipt_id, "receipt", self.brain_id)
        _validate_scoped_id(self.commit_id, "commit", self.brain_id)
        _validate_scoped_id(self.delivery_id, "delivery", self.brain_id)
        _cursor(self.cursor)
        _digest(self.commit_digest, "commit digest")
        _positive(self.issuer_epoch, "issuer_epoch")
        _digest(self.policy_digest, "policy digest")
        _positive(self.sequencer_epoch, "sequencer_epoch")
        _validate_scoped_id(self.node_key_id, "key", self.brain_id)
        certificate = _freeze_json_object(self.node_epoch_certificate)
        history = tuple(_freeze_json_object(entry) for entry in self.owner_key_history)
        if not history:
            raise ValueError("owner key history must not be empty")
        object.__setattr__(self, "node_epoch_certificate", certificate)
        object.__setattr__(self, "owner_key_history", history)
        if certificate.get("brain_id") != self.brain_id:
            raise ValueError("receipt certificate crosses Brain boundary")
        if certificate.get("node_key_id") != self.node_key_id:
            raise ValueError("receipt certificate key binding is invalid")
        if certificate.get("sequencer_epoch") != self.sequencer_epoch:
            raise ValueError("receipt certificate epoch binding is invalid")
        _nonempty(self.issued_at, "receipt issued_at")
        _nonempty(self.node_signature, "node signature")
        if self.schema_version != 1:
            raise ValueError("unsupported receipt schema version")

    @property
    def digest(self) -> str:
        return self.commit_digest


@dataclass(frozen=True, slots=True)
class DeliveryBinding:
    brain_id: str
    delivery_id: str
    digest: str
    receipt: LedgerReceipt

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.delivery_id, "delivery", self.brain_id)
        _digest(self.digest, "delivery digest")
        if not isinstance(self.receipt, LedgerReceipt):
            raise ValueError("delivery receipt is invalid")
        receipt = replace(self.receipt)
        object.__setattr__(self, "receipt", receipt)
        if (
            receipt.brain_id != self.brain_id
            or receipt.delivery_id != self.delivery_id
            or receipt.commit_digest != self.digest
        ):
            raise ValueError("delivery receipt binding is invalid")


@dataclass(frozen=True, slots=True)
class ActiveLabelSetSnapshot:
    brain_id: str
    counts: Mapping[tuple[str, ...], int] = field(default_factory=dict)
    max_active_label_sets: int = RESOURCE_LIMITS.active_label_sets_per_brain

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        if (
            isinstance(self.max_active_label_sets, bool)
            or not isinstance(self.max_active_label_sets, int)
            or self.max_active_label_sets < 1
            or self.max_active_label_sets > RESOURCE_LIMITS.active_label_sets_per_brain
        ):
            raise ValueError("active-label-set limit cannot exceed the frozen limit")
        canonical: dict[tuple[str, ...], int] = {}
        for raw_labels, count in self.counts.items():
            labels = canonical_compartments(tuple(raw_labels))
            if tuple(raw_labels) != labels:
                raise ValueError("active-label-set keys must already be canonical")
            if labels in canonical:
                raise ValueError("active-label-set snapshot contains an ambiguous duplicate")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("active-label-set counts must be nonnegative integers")
            canonical[labels] = count
        object.__setattr__(self, "counts", _frozen_mapping(canonical))

    @classmethod
    def empty(
        cls,
        brain_id: str,
        *,
        max_active_label_sets: int = RESOURCE_LIMITS.active_label_sets_per_brain,
    ) -> ActiveLabelSetSnapshot:
        return cls(brain_id, {}, max_active_label_sets)


@dataclass(frozen=True, slots=True)
class Purge:
    brain_id: str
    purge_id: str
    target_record_id: str
    resolutions: Mapping[str, PurgeTransition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        _validate_scoped_id(self.purge_id, "purge", self.brain_id)
        _validate_scoped_id(self.target_record_id, "record", self.brain_id)
        resolutions: dict[str, PurgeTransition] = {}
        for subject_id, supplied in self.resolutions.items():
            if not isinstance(supplied, PurgeTransition):
                raise ValueError("purge resolution is invalid")
            resolution = replace(supplied)
            if (
                subject_id != resolution.subject_id
                or resolution.brain_id != self.brain_id
                or resolution.purge_id != self.purge_id
                or resolution.target_record_id != self.target_record_id
            ):
                raise ValueError("purge resolution binding is invalid")
            resolutions[subject_id] = resolution
        object.__setattr__(self, "resolutions", _frozen_mapping(resolutions))


@dataclass(frozen=True, slots=True)
class BrainState:
    brain_id: str
    records: Mapping[str, Record] = field(default_factory=dict)
    artifacts: Mapping[str, Artifact] = field(default_factory=dict)
    revisions: Mapping[str, Revision] = field(default_factory=dict)
    proposals: Mapping[str, Proposal] = field(default_factory=dict)
    proposal_heads: Mapping[str, str] = field(default_factory=dict)
    decisions: Mapping[str, Decision] = field(default_factory=dict)
    effects: Mapping[str, Effect] = field(default_factory=dict)
    purges: Mapping[str, Purge] = field(default_factory=dict)
    commits: Mapping[str, Commit] = field(default_factory=dict)
    receipts: Mapping[str, LedgerReceipt] = field(default_factory=dict)
    deliveries: Mapping[str, DeliveryBinding] = field(default_factory=dict)
    artifact_heads: Mapping[str, str] = field(default_factory=dict)
    origin_heads: Mapping[str, str] = field(default_factory=dict)
    superseded_record_ids: frozenset[str] | Iterable[str] = frozenset()
    suppressed_ids: frozenset[str] | Iterable[str] = frozenset()
    tombstoned_ids: frozenset[str] | Iterable[str] = frozenset()
    purge_pending_ids: frozenset[str] | Iterable[str] = frozenset()
    purged_deliveries: frozenset[str] | Iterable[str] = frozenset()

    def __post_init__(self) -> None:
        validate_identifier(self.brain_id, role="brain")
        object.__setattr__(
            self,
            "records",
            _frozen_mapping({key: replace(value) for key, value in self.records.items()}),
        )
        object.__setattr__(
            self,
            "artifacts",
            _frozen_mapping({key: replace(value) for key, value in self.artifacts.items()}),
        )
        object.__setattr__(
            self,
            "revisions",
            _frozen_mapping({key: replace(value) for key, value in self.revisions.items()}),
        )
        object.__setattr__(
            self,
            "proposals",
            _frozen_mapping({key: replace(value) for key, value in self.proposals.items()}),
        )
        object.__setattr__(self, "proposal_heads", _frozen_mapping(self.proposal_heads))
        object.__setattr__(
            self,
            "decisions",
            _frozen_mapping({key: replace(value) for key, value in self.decisions.items()}),
        )
        object.__setattr__(
            self,
            "effects",
            _frozen_mapping({key: replace(value) for key, value in self.effects.items()}),
        )
        object.__setattr__(
            self,
            "purges",
            _frozen_mapping({key: replace(value) for key, value in self.purges.items()}),
        )
        object.__setattr__(
            self,
            "commits",
            _frozen_mapping({key: replace(value) for key, value in self.commits.items()}),
        )
        object.__setattr__(
            self,
            "receipts",
            _frozen_mapping({key: replace(value) for key, value in self.receipts.items()}),
        )
        object.__setattr__(
            self,
            "deliveries",
            _frozen_mapping({key: replace(value) for key, value in self.deliveries.items()}),
        )
        object.__setattr__(self, "artifact_heads", _frozen_mapping(self.artifact_heads))
        object.__setattr__(self, "origin_heads", _frozen_mapping(self.origin_heads))
        for name in (
            "superseded_record_ids",
            "suppressed_ids",
            "tombstoned_ids",
            "purge_pending_ids",
            "purged_deliveries",
        ):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        self.validate()

    @classmethod
    def empty(cls, brain_id: str, **changes: object) -> BrainState:
        return cls(brain_id=brain_id, **changes)  # type: ignore[arg-type]

    def validate(self) -> None:
        """Revalidate a complete authoritative state at every transition boundary."""
        keyed = (
            (self.records, "record_id"),
            (self.artifacts, "artifact_id"),
            (self.revisions, "revision_id"),
            (self.proposals, "proposal_revision_id"),
            (self.decisions, "decision_id"),
            (self.effects, "effect_id"),
            (self.purges, "purge_id"),
            (self.commits, "commit_id"),
            (self.receipts, "receipt_id"),
        )
        for values, identity_field in keyed:
            for key, value in values.items():
                if getattr(value, "brain_id", None) != self.brain_id:
                    raise ValueError("Brain state contains a cross-Brain value")
                if key != getattr(value, identity_field):
                    raise ValueError("Brain state mapping key does not match its value identity")
        for record in self.records.values():
            _labels(record.compartments)
            if record.ciphertext_state == "pending":
                raise ValueError("authoritative Brain state cannot contain pending record input")
        for revision in self.revisions.values():
            _labels(revision.compartments)
        for proposal in self.proposals.values():
            _labels(proposal.compartments)
        for decision in self.decisions.values():
            _labels(decision.compartments)
        for effect in self.effects.values():
            for effect_receipt in effect.receipts:
                _labels(effect_receipt.compartments)
        for artifact_id, revision_id in self.artifact_heads.items():
            head_revision = self.revisions.get(revision_id)
            if head_revision is None or head_revision.artifact_id != artifact_id:
                raise ValueError("artifact head binding is invalid")
        for proposal_id, revision_id in self.proposal_heads.items():
            head_proposal = self.proposals.get(revision_id)
            if head_proposal is None or head_proposal.proposal_id != proposal_id:
                raise ValueError("proposal head binding is invalid")
        for origin_id, record_id in self.origin_heads.items():
            origin_record = self.records.get(record_id)
            if origin_record is None or origin_record.origin_id != origin_id:
                raise ValueError("record origin-head binding is invalid")
        for delivery_id, binding in self.deliveries.items():
            stored_receipt = self.receipts.get(binding.receipt.receipt_id)
            if (
                delivery_id != binding.delivery_id
                or binding.brain_id != self.brain_id
                or stored_receipt is None
                or stored_receipt != binding.receipt
            ):
                raise ValueError("delivery binding does not name its stored receipt")
        for ledger_receipt in self.receipts.values():
            commit = self.commits.get(ledger_receipt.commit_id)
            if (
                commit is None
                or ledger_receipt.brain_id != commit.brain_id
                or ledger_receipt.delivery_id != commit.delivery_id
                or ledger_receipt.cursor != commit.cursor
                or ledger_receipt.commit_digest != commit.digest
                or ledger_receipt.issuer_epoch != commit.issuer_epoch
                or ledger_receipt.policy_digest != commit.policy_digest
                or ledger_receipt.sequencer_epoch != commit.sequencer_epoch
            ):
                raise ValueError("receipt and commit binding is invalid")
        known = self.semantic_entity_ids()
        for entity_id in (
            frozenset(self.superseded_record_ids)
            | frozenset(self.suppressed_ids)
            | frozenset(self.tombstoned_ids)
            | frozenset(self.purge_pending_ids)
        ):
            if entity_id not in known:
                raise ValueError("Brain state lifecycle set names an unknown entity")
        for delivery_id in self.purged_deliveries:
            _validate_scoped_id(delivery_id, "delivery", self.brain_id)
        _validate_provenance_graph(self.provenance_items(), self.records, self.revisions)
        _validate_state_relationships(self)

    def provenance_items(self) -> tuple[Record | Revision | Proposal | EffectReceipt, ...]:
        effect_receipts = tuple(
            receipt for effect in self.effects.values() for receipt in effect.receipts
        )
        return (
            *self.records.values(),
            *self.revisions.values(),
            *self.proposals.values(),
            *effect_receipts,
        )

    def processor_output_items(
        self,
    ) -> tuple[Record | Revision | Proposal | EffectReceipt, ...]:
        return (
            *self.records.values(),
            *self.revisions.values(),
            *self.proposals.values(),
            *(
                cast(tuple[EffectReceipt, ...], effect.receipts)[0]
                for effect in self.effects.values()
            ),
        )

    def semantic_entity_ids(self) -> frozenset[str]:
        return frozenset(
            {
                *self.records,
                *self.revisions,
                *(proposal.proposal_id for proposal in self.proposals.values()),
                *(proposal.proposal_revision_id for proposal in self.proposals.values()),
                *self.decisions,
                *(
                    receipt.receipt_id
                    for effect in self.effects.values()
                    for receipt in effect.receipts
                ),
            }
        )

    def purge_subjects_for(self, target_record_id: str) -> frozenset[str]:
        nodes = {target_record_id, *self.descendants_of(target_record_id)}
        proposal_subjects = {
            proposal.proposal_id
            for proposal in self.proposals.values()
            if proposal.proposal_revision_id in nodes
        }
        effect_receipt_ids = {
            receipt.receipt_id
            for effect in self.effects.values()
            for receipt in effect.receipts
            if receipt.receipt_id in nodes
        }
        return frozenset(
            {
                *(node for node in nodes if node in self.records or node in self.revisions),
                *proposal_subjects,
                *effect_receipt_ids,
            }
        )

    def descendants_of(self, entity_id: str) -> tuple[str, ...]:
        """Return the deterministic breadth-first transitive provenance closure."""
        children: dict[str, set[str]] = {}
        for item in self.provenance_items():
            child_id = lineage_identifier(item)
            for source_id in item.provenance.source_ids:
                children.setdefault(source_id, set()).add(child_id)
        queue = deque(sorted(children.get(entity_id, ())))
        seen = {entity_id}
        result: list[str] = []
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            queue.extend(sorted(children.get(current, ())))
        return tuple(result)

    def ancestors_of(self, entity_id: str) -> tuple[str, ...]:
        parents = {
            lineage_identifier(item): item.provenance.source_ids
            for item in self.provenance_items()
        }
        queue = deque(sorted(parents.get(entity_id, ())))
        seen = {entity_id}
        result: list[str] = []
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            queue.extend(sorted(parents.get(current, ())))
        return tuple(result)


def _validate_state_relationships(state: BrainState) -> None:
    effect_receipts = _effect_receipts_by_id(state)
    _validate_loaded_compartments(state)
    _validate_artifact_state(state)
    _validate_origin_state(state)
    _validate_processor_output_state(state)
    _validate_purge_state(state, effect_receipts)
    _validate_tombstone_closures(state, effect_receipts)
    _validate_commit_evidence(state, effect_receipts)


def _effect_receipts_by_id(state: BrainState) -> dict[str, EffectReceipt]:
    receipts: dict[str, EffectReceipt] = {}
    for effect in state.effects.values():
        for receipt in effect.receipts:
            if receipt.receipt_id in receipts:
                raise ValueError("effect receipt identity is duplicated")
            receipts[receipt.receipt_id] = receipt
    if set(receipts).intersection(state.receipts):
        raise ValueError("effect and ledger receipt identities must be distinct")
    return receipts


def _validate_loaded_compartments(state: BrainState) -> None:
    for item in state.provenance_items():
        if not item.provenance.source_ids:
            continue
        source_labels = [
            tuple(state.records[source_id].compartments)
            for source_id in item.provenance.source_record_ids
        ]
        source_labels.extend(
            tuple(state.revisions[source_id].compartments)
            for source_id in item.provenance.source_revision_ids
        )
        if tuple(item.compartments) != propagated_compartments(*source_labels):
            raise ValueError("derived output compartments must equal provenance union")


def _validate_artifact_state(state: BrainState) -> None:
    if set(state.revisions).intersection(state.proposals):
        raise ValueError("revision and proposal-revision identities must be distinct")

    proposal_groups: dict[str, list[Proposal]] = {}
    for proposal in state.proposals.values():
        if proposal.artifact_id not in state.artifacts:
            raise ValueError("proposal artifact is unknown")
        if proposal.base_revision_id is not None:
            base = state.revisions.get(proposal.base_revision_id)
            if base is None or base.artifact_id != proposal.artifact_id:
                raise ValueError("proposal base belongs to another artifact or is unknown")
        proposal_groups.setdefault(proposal.proposal_id, []).append(proposal)

    if set(state.proposal_heads) != set(proposal_groups):
        raise ValueError("proposal heads do not exactly cover proposal histories")
    for proposal_id, versions in proposal_groups.items():
        first = versions[0]
        if any(
            version.artifact_id != first.artifact_id
            or version.base_revision_id != first.base_revision_id
            or version.content_schema != first.content_schema
            or version.compartments != first.compartments
            or version.provenance != first.provenance
            for version in versions[1:]
        ):
            raise ValueError("proposal history changed immutable proposal identity")
        if state.proposal_heads[proposal_id] not in {
            version.proposal_revision_id for version in versions
        }:
            raise ValueError("proposal head is not a member of its history")

    decided: set[tuple[str, str]] = set()
    for decision in state.decisions.values():
        decision_proposal = state.proposals.get(decision.expected_proposal_revision_id)
        if (
            decision_proposal is None
            or decision_proposal.proposal_id != decision.proposal_id
            or decision_proposal.compartments != decision.compartments
        ):
            raise ValueError("decision does not name its exact proposal revision")
        key = (decision.proposal_id, decision.expected_proposal_revision_id)
        if key in decided:
            raise ValueError("proposal revision has more than one decision")
        decided.add(key)

    revision_groups: dict[str, list[Revision]] = {}
    for revision in state.revisions.values():
        if revision.artifact_id not in state.artifacts:
            raise ValueError("revision artifact is unknown")
        if revision.base_revision_id is not None:
            base = state.revisions.get(revision.base_revision_id)
            if base is None or base.artifact_id != revision.artifact_id:
                raise ValueError("revision base belongs to another artifact or is unknown")
        revision_groups.setdefault(revision.artifact_id, []).append(revision)

    if set(state.artifact_heads) != set(revision_groups):
        raise ValueError("artifact heads do not exactly cover revision histories")
    for artifact_id, revisions in revision_groups.items():
        by_id = {revision.revision_id: revision for revision in revisions}
        roots = [revision for revision in revisions if revision.base_revision_id is None]
        if len(roots) != 1:
            raise ValueError("artifact revision history must have exactly one root")
        successors: dict[str, list[Revision]] = {}
        for revision in revisions:
            if revision.base_revision_id is not None:
                successors.setdefault(revision.base_revision_id, []).append(revision)
        if any(len(values) != 1 for values in successors.values()):
            raise ValueError("artifact revision history must be linear")

        ordered: list[Revision] = []
        current = roots[0]
        while True:
            ordered.append(current)
            next_values = successors.get(current.revision_id, [])
            if not next_values:
                break
            current = next_values[0]
        if len(ordered) != len(by_id):
            raise ValueError("artifact revision history is disconnected")
        if state.artifact_heads[artifact_id] != ordered[-1].revision_id:
            raise ValueError("artifact head is not the terminal revision")
        for index, revision in enumerate(ordered):
            _validate_loaded_revision_approval(
                revision,
                is_genesis=index == 0,
                proposals=state.proposals,
                decisions=state.decisions,
            )


def _validate_loaded_revision_approval(
    revision: Revision,
    *,
    is_genesis: bool,
    proposals: Mapping[str, Proposal],
    decisions: Mapping[str, Decision],
) -> None:
    proposal_id = revision.accepted_from_proposal_id
    decision_id = revision.decision_id
    if is_genesis and proposal_id is None and decision_id is None:
        return
    if proposal_id is None or decision_id is None:
        raise ValueError("non-genesis revision requires proposal and decision links")
    decision = decisions.get(decision_id)
    if decision is None or decision.proposal_id != proposal_id or decision.outcome != "accepted":
        raise ValueError("revision decision link is not an accepted decision")
    proposal = proposals.get(decision.expected_proposal_revision_id)
    if (
        proposal is None
        or proposal.proposal_id != proposal_id
        or proposal.artifact_id != revision.artifact_id
        or proposal.base_revision_id != revision.base_revision_id
        or proposal.content_schema != revision.content_schema
        or proposal.compartments != revision.compartments
        or proposal.provenance != revision.provenance
        or proposal.body != revision.body
    ):
        raise ValueError("accepted revision does not exactly match its proposal")


def _validate_origin_state(state: BrainState) -> None:
    by_origin: dict[str, list[Record]] = {}
    for record in state.records.values():
        if record.origin_id is not None:
            by_origin.setdefault(record.origin_id, []).append(record)
    if set(state.origin_heads) != set(by_origin):
        raise ValueError("origin heads do not exactly cover record origins")

    expected_superseded: set[str] = set()
    for origin_id, records in by_origin.items():
        by_id = {record.record_id: record for record in records}
        parents: dict[str, str] = {}
        successors: dict[str, list[str]] = {}
        for record in records:
            same_origin_sources = set(record.provenance.source_record_ids).intersection(by_id)
            if len(same_origin_sources) > 1:
                raise ValueError("an origin revision must cite exactly one prior origin head")
            if same_origin_sources:
                parent = next(iter(same_origin_sources))
                parents[record.record_id] = parent
                successors.setdefault(parent, []).append(record.record_id)
        roots = [record_id for record_id in by_id if record_id not in parents]
        if len(roots) != 1 or any(len(values) != 1 for values in successors.values()):
            raise ValueError("record origin history must be one linear chain")
        ordered: list[str] = []
        current = roots[0]
        while True:
            ordered.append(current)
            next_values = successors.get(current, [])
            if not next_values:
                break
            current = next_values[0]
        if len(ordered) != len(by_id):
            raise ValueError("record origin history is disconnected")
        if state.origin_heads[origin_id] != ordered[-1]:
            raise ValueError("record origin head is not the terminal record")
        expected_superseded.update(ordered[:-1])
    if frozenset(expected_superseded) != state.superseded_record_ids:
        raise ValueError("superseded record state does not match origin history")


def _validate_processor_output_state(state: BrainState) -> None:
    seen: set[str] = set()
    for item in state.processor_output_items():
        if item.provenance.derivation != "processor":
            continue
        identity = processor_output_identity(item.provenance)
        if identity in seen:
            raise ValueError("authoritative state repeats a processor output")
        seen.add(identity)


def _purge_subject_kind(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
    subject_id: str,
) -> PurgeSubjectKind:
    matches: list[PurgeSubjectKind] = []
    if subject_id in state.records:
        matches.append("record")
    if subject_id in state.revisions:
        matches.append("revision")
    if any(proposal.proposal_id == subject_id for proposal in state.proposals.values()):
        matches.append("proposal")
    if subject_id in effect_receipts:
        matches.append("effect_receipt")
    if len(matches) != 1:
        raise ValueError("purge subject identity is unknown or ambiguous")
    return matches[0]


def _purge_subject_compartments(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
    subject_kind: PurgeSubjectKind,
    subject_id: str,
) -> tuple[str, ...]:
    if subject_kind == "record":
        return tuple(cast(Record, state.records.get(subject_id)).compartments)
    if subject_kind == "revision":
        return tuple(cast(Revision, state.revisions.get(subject_id)).compartments)
    if subject_kind == "effect_receipt":
        return tuple(cast(EffectReceipt, effect_receipts.get(subject_id)).compartments)
    return next(
        tuple(proposal.compartments)
        for proposal in state.proposals.values()
        if proposal.proposal_id == subject_id
    )


def _purge_subject_lineage_ids(state: BrainState, subject_id: str) -> frozenset[str]:
    proposal_revisions = {
        proposal.proposal_revision_id
        for proposal in state.proposals.values()
        if proposal.proposal_id == subject_id
    }
    return frozenset({subject_id, *proposal_revisions})


def _validate_purge_review(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
    transition: PurgeTransition,
) -> None:
    decision = state.decisions.get(cast(str, transition.review_decision_id))
    if decision is None or decision.outcome != "accepted":
        raise ValueError("purge resolution requires an accepted review decision")
    proposal = state.proposals.get(decision.expected_proposal_revision_id)
    if proposal is None or proposal.proposal_id != decision.proposal_id:
        raise ValueError("purge review decision does not bind an exact proposal revision")
    expected_body: dict[str, object] = {
        "kind": "purge_resolution_review",
        "purge_id": transition.purge_id,
        "subject_kind": transition.subject_kind,
        "subject_id": transition.subject_id,
        "resolution": transition.resolution,
        "replacement_record_id": transition.replacement_record_id,
    }
    if dict(proposal.body) != expected_body:
        raise ValueError("purge review proposal does not name the exact resolution intent")
    if proposal.provenance.derivation != "owner" or proposal.provenance.source_ids:
        raise ValueError("purge review proposal must use owner provenance without sources")
    subject_labels = _purge_subject_compartments(
        state,
        effect_receipts,
        transition.subject_kind,
        transition.subject_id,
    )
    expected_labels = subject_labels
    if transition.resolution == "replace":
        replacement = state.records.get(cast(str, transition.replacement_record_id))
        if replacement is None:
            raise ValueError("purge replacement record is invalid")
        expected_labels = tuple(sorted(set(subject_labels) | set(replacement.compartments)))
    if proposal.compartments != expected_labels:
        raise ValueError("purge review compartments do not match the reviewed values")


def _validate_purge_state(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
) -> None:
    closures: dict[str, frozenset[str]] = {}
    for purge_id, purge in state.purges.items():
        if purge.target_record_id not in state.records:
            raise ValueError("purge target is unknown")
        closure = state.purge_subjects_for(purge.target_record_id)
        for other_id, other_closure in closures.items():
            if closure.intersection(other_closure):
                raise ValueError(
                    f"purge closures for {other_id} and {purge_id} overlap"
                )
        closures[purge_id] = closure

        target_resolution = purge.resolutions.get(purge.target_record_id)
        if target_resolution is None or target_resolution.resolution != "purge":
            raise ValueError("purge target requires an initiating purge resolution")
        activated_replacements = {
            cast(str, resolution.replacement_record_id)
            for resolution in purge.resolutions.values()
            if resolution.resolution == "replace"
        }
        if activated_replacements.intersection(purge.resolutions):
            raise ValueError(
                "an approved replacement cannot also be a purge resolution subject"
            )
        for subject_id, transition in purge.resolutions.items():
            if subject_id not in closure:
                raise ValueError("purge resolution is outside the target provenance closure")
            actual_kind = _purge_subject_kind(state, effect_receipts, subject_id)
            if transition.subject_kind != actual_kind:
                raise ValueError("purge subject kind does not match its identity")
            if subject_id == purge.target_record_id and transition.resolution != "purge":
                raise ValueError("purge target cannot be retained or replaced")
            if transition.resolution in {"retain", "replace"}:
                _validate_purge_review(state, effect_receipts, transition)
            if transition.resolution == "replace":
                replacement_id = cast(str, transition.replacement_record_id)
                replacement = state.records.get(replacement_id)
                if replacement is None or replacement_id == subject_id:
                    raise ValueError("purge replacement record is invalid")
                ancestry = set(state.ancestors_of(replacement_id))
                if (
                    replacement.provenance.derivation != "replacement"
                    or not ancestry.intersection(_purge_subject_lineage_ids(state, subject_id))
                ):
                    raise ValueError("replacement is not provenance-bound to its subject")

        for subject_id in closure:
            stored_transition = purge.resolutions.get(subject_id)
            if stored_transition is None:
                if subject_id in activated_replacements:
                    if (
                        subject_id in state.suppressed_ids
                        or subject_id in state.purge_pending_ids
                        or subject_id in state.tombstoned_ids
                    ):
                        raise ValueError("approved purge replacement remains inactive")
                elif (
                    subject_id not in state.suppressed_ids
                    or subject_id not in state.purge_pending_ids
                    or subject_id in state.tombstoned_ids
                ):
                    raise ValueError("unresolved purge subject is not suppressed and pending")
                continue
            if stored_transition.resolution == "retain":
                if (
                    subject_id in state.suppressed_ids
                    or subject_id in state.purge_pending_ids
                    or subject_id in state.tombstoned_ids
                ):
                    raise ValueError("retained purge subject remains inactive")
            elif (
                subject_id not in state.suppressed_ids
                or subject_id not in state.tombstoned_ids
                or subject_id in state.purge_pending_ids
            ):
                raise ValueError("purged or replaced subject has inconsistent lifecycle state")


def _normalized_lineage_closure(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
    subject_id: str,
) -> frozenset[str]:
    nodes = set(_purge_subject_lineage_ids(state, subject_id))
    for lineage_id in tuple(nodes):
        nodes.update(state.descendants_of(lineage_id))
    return frozenset(
        {
            *(node for node in nodes if node in state.records or node in state.revisions),
            *(
                proposal.proposal_id
                for proposal in state.proposals.values()
                if proposal.proposal_id in nodes or proposal.proposal_revision_id in nodes
            ),
            *(receipt_id for receipt_id in effect_receipts if receipt_id in nodes),
        }
    )


def _validate_tombstone_closures(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
) -> None:
    tombstoned_ids = frozenset(state.tombstoned_ids)
    suppressed_ids = frozenset(state.suppressed_ids)
    purge_pending_ids = frozenset(state.purge_pending_ids)
    if not tombstoned_ids.issubset(suppressed_ids):
        raise ValueError("every tombstoned entity must remain suppressed")
    if not purge_pending_ids.issubset(suppressed_ids):
        raise ValueError("every purge-pending entity must remain suppressed")

    expected_suppressed: set[str] = set()
    expected_purge_pending: set[str] = set()
    tombstone_owners: dict[str, Purge] = {}
    for purge in state.purges.values():
        closure = state.purge_subjects_for(purge.target_record_id)
        activated_replacements = {
            cast(str, transition.replacement_record_id)
            for transition in purge.resolutions.values()
            if transition.resolution == "replace"
        }
        for transition in purge.resolutions.values():
            if transition.resolution in {"purge", "replace"}:
                tombstone_owners[transition.subject_id] = purge
        for subject_id in closure:
            stored_transition = purge.resolutions.get(subject_id)
            if stored_transition is None:
                if subject_id not in activated_replacements:
                    expected_suppressed.add(subject_id)
                    expected_purge_pending.add(subject_id)
            elif stored_transition.resolution != "retain":
                expected_suppressed.add(subject_id)

    for tombstoned_id in tombstoned_ids:
        closure = _normalized_lineage_closure(state, effect_receipts, tombstoned_id)
        if tombstoned_id not in closure:
            raise ValueError("tombstone names an entity that cannot participate in purge")
        owner = tombstone_owners.get(tombstoned_id)
        visible_exceptions: set[str] = set()
        if owner is not None:
            visible_exceptions.update(
                transition.subject_id
                for transition in owner.resolutions.values()
                if transition.resolution == "retain"
            )
            visible_exceptions.update(
                cast(str, transition.replacement_record_id)
                for transition in owner.resolutions.values()
                if transition.resolution == "replace"
            )
        required_suppression = closure - visible_exceptions
        expected_suppressed.update(required_suppression)

    if purge_pending_ids != frozenset(expected_purge_pending):
        raise ValueError("purge-pending lifecycle set does not match stored purge state")
    if suppressed_ids != frozenset(expected_suppressed):
        if expected_suppressed - suppressed_ids:
            raise ValueError("tombstoned provenance closure contains an active descendant")
        raise ValueError("suppression lifecycle set does not match tombstones and purges")


def _resolve_item_reference(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
    reference: LedgerItemRef,
) -> bool:
    if isinstance(reference, RecordItemRef):
        return reference.record_id in state.records
    if isinstance(reference, ProposalItemRef):
        proposal = state.proposals.get(reference.proposal_revision_id)
        return proposal is not None and proposal.proposal_id == reference.proposal_id
    if isinstance(reference, RevisionItemRef):
        return reference.revision_id in state.revisions
    if isinstance(reference, DecisionItemRef):
        return reference.decision_id in state.decisions
    if isinstance(reference, EffectReceiptItemRef):
        return reference.receipt_id in effect_receipts
    purge = state.purges.get(reference.purge_id)
    resolution = None if purge is None else purge.resolutions.get(reference.subject_id)
    return resolution is not None and resolution.subject_kind == reference.subject_kind


def _validate_commit_evidence(
    state: BrainState,
    effect_receipts: Mapping[str, EffectReceipt],
) -> None:
    commits = tuple(state.commits.values())
    receipts = tuple(state.receipts.values())
    if len({commit.cursor for commit in commits}) != len(commits):
        raise ValueError("commit cursors must be unique within a Brain")
    if len({commit.delivery_id for commit in commits}) != len(commits):
        raise ValueError("commit delivery identities must be unique within a Brain")
    if len({receipt.commit_id for receipt in receipts}) != len(receipts):
        raise ValueError("each commit may have only one ledger receipt")
    if len({receipt.delivery_id for receipt in receipts}) != len(receipts):
        raise ValueError("each delivery may have only one ledger receipt")
    if set(state.commits) != {receipt.commit_id for receipt in receipts}:
        raise ValueError("commit and receipt evidence must be one-to-one")
    commit_deliveries = {commit.delivery_id for commit in commits}
    receipt_deliveries = {receipt.delivery_id for receipt in receipts}
    if commit_deliveries != receipt_deliveries or commit_deliveries != set(state.deliveries):
        raise ValueError("commit, receipt, and delivery evidence must be one-to-one")
    if commit_deliveries.intersection(state.purged_deliveries):
        raise ValueError("purged delivery cannot retain commit evidence")

    committed_items: set[LedgerItemRef] = set()
    for commit in commits:
        for reference in commit.item_refs:
            if not _resolve_item_reference(state, effect_receipts, reference):
                raise ValueError("commit item reference does not resolve")
            if reference in committed_items:
                raise ValueError("ledger item reference appears in more than one commit")
            committed_items.add(reference)


def _validate_provenance_graph(
    items: Iterable[Record | Revision | Proposal | EffectReceipt],
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
) -> None:
    parents: dict[str, set[str]] = {}
    for item in items:
        child_id = lineage_identifier(item)
        parents.setdefault(child_id, set()).update(item.provenance.source_ids)
        for source_id in item.provenance.source_record_ids:
            if source_id not in records:
                raise ValueError("provenance source record is unknown")
        for source_id in item.provenance.source_revision_ids:
            if source_id not in revisions:
                raise ValueError("provenance source revision is unknown")
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(node: str) -> None:
        if node in complete:
            return
        if node in visiting:
            raise ValueError("provenance graph contains a cycle")
        visiting.add(node)
        for parent in parents.get(node, ()):
            visit(parent)
        visiting.remove(node)
        complete.add(node)

    for node in sorted(parents):
        visit(node)


def processor_output_identity(provenance: Provenance) -> str:
    """Bind processor output to one Brain, every source, and processor version."""
    if provenance.derivation != "processor" or not isinstance(
        provenance.processor, ProcessorIdentity
    ):
        raise ValueError("processor output identity requires processor provenance")
    payload = json.dumps(
        {
            "brain_id": provenance.brain_id,
            "processor_id": provenance.processor.processor_id,
            "processor_version": provenance.processor.version,
            "source_record_ids": provenance.source_record_ids,
            "source_revision_ids": provenance.source_revision_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "proc_v1_" + sha256(payload).hexdigest()


Brain = BrainState
Receipt = LedgerReceipt
