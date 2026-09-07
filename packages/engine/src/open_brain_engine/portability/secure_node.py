"""Pure Secure Node envelope planning for product-neutral shared records."""

from __future__ import annotations

import base64
import heapq
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from open_brain_engine.ledger.model import (
    ActiveLabelSetSnapshot,
    BrainState,
    CommitBatch,
    ProcessorIdentity,
    Provenance,
    Record,
    SchemaReference,
)
from open_brain_engine.ledger.policy import canonical_compartments
from open_brain_engine.ledger.transitions import apply_batch
from open_brain_engine.protocol import (
    RESOURCE_LIMITS,
    canonical_sha256,
    validate_identifier,
    validate_protocol_semantics,
)

from .model import (
    FAMILY_ID_PREFIX,
    PortabilityMappingError,
    SharedBrain,
    SharedFamily,
    SharedRecord,
    validate_portable_identifier,
)
from .resources import shared_envelope_schema_bytes

_RECORD_ID_DOMAIN = b"open-brain:portable-to-secure-node:v1\x00"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROTOCOL_TIMESTAMP = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12][0-9]|3[01])T"
    r"(?P<hour>[01][0-9]|2[0-3]):(?P<minute>[0-5][0-9]):(?P<second>[0-5][0-9])"
    r"(?:\.(?P<millisecond>[0-9]{3}))?Z"
)
_SHARED_ENVELOPE_URI = "urn:open-brain:shared-portability:v1:record-envelope"


def _base32_identifier(prefix: str, payload: bytes) -> str:
    token = base64.b32encode(payload).decode("ascii").rstrip("=").lower()
    return f"{prefix}_{token}"


def reencode_portable_identifier(
    source_id: str, expected_prefix: str, target_prefix: str
) -> str:
    """Re-encode the same UUID bits as one role-distinct protocol identifier."""
    validate_portable_identifier(source_id, expected_prefix)
    if (expected_prefix, target_prefix) not in {("tenant", "brn"), ("actor", "pri")}:
        raise PortabilityMappingError("unsupported Portable identifier re-encoding")
    source_uuid = uuid.UUID(source_id.split("_", 1)[1])
    return _base32_identifier(target_prefix, source_uuid.bytes)


def derive_import_record_id(
    source_brain_id: str, family: SharedFamily, semantic_id: str
) -> str:
    """Derive a role-distinct record envelope ID without hashing record content."""
    validate_portable_identifier(source_brain_id, "tenant")
    if family not in FAMILY_ID_PREFIX:
        raise PortabilityMappingError("unsupported shared record family")
    validate_portable_identifier(semantic_id, FAMILY_ID_PREFIX[family])
    if "\x00" in source_brain_id or "\x00" in family or "\x00" in semantic_id:
        raise PortabilityMappingError("Portable import identity contains NUL")
    framed = (
        _RECORD_ID_DOMAIN
        + source_brain_id.encode("utf-8")
        + b"\x00"
        + family.encode("utf-8")
        + b"\x00"
        + semantic_id.encode("utf-8")
        + b"\x00"
    )
    return _base32_identifier("rec", sha256(framed).digest()[:16])


@dataclass(frozen=True, slots=True)
class ImportEnvelopeContext:
    observed_at: str
    compartments: tuple[str, ...] | Iterable[str]
    policy_digest: str
    issuer_epoch: int
    sequencer_epoch: int
    delivery_ids: tuple[str, ...] | Iterable[str]

    def __post_init__(self) -> None:
        _validate_protocol_timestamp(self.observed_at, "observed_at")
        labels = canonical_compartments(self.compartments)
        if len(labels) > RESOURCE_LIMITS.labels_per_record:
            raise ValueError("compartments exceed the frozen record limit")
        if not isinstance(self.policy_digest, str) or _DIGEST.fullmatch(self.policy_digest) is None:
            raise ValueError("policy_digest must be lowercase SHA-256")
        for name, value in (
            ("issuer_epoch", self.issuer_epoch),
            ("sequencer_epoch", self.sequencer_epoch),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        delivery_ids = tuple(self.delivery_ids)
        if (
            not delivery_ids
            or len(delivery_ids) != len(set(delivery_ids))
            or any(not isinstance(delivery_id, str) for delivery_id in delivery_ids)
        ):
            raise ValueError("delivery IDs must be non-empty, unique strings")
        object.__setattr__(self, "compartments", labels)
        object.__setattr__(self, "delivery_ids", delivery_ids)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_value(child) for child in value]
    return value


def record_document(record: Record) -> dict[str, object]:
    """Serialize one shared import record as its exact Brain Protocol v1 document."""
    processor = record.provenance.processor
    processor_document: dict[str, object] | None = None
    if processor is not None:
        if not isinstance(processor, ProcessorIdentity):
            raise PortabilityMappingError("record processor identity is not materialized")
        processor_document = {
            "processor_id": processor.processor_id,
            "version": processor.version,
        }
    return {
        "schema_version": record.schema_version,
        "kind": "record",
        "brain_id": record.brain_id,
        "record_id": record.record_id,
        "record_type": record.record_type,
        "content_schema": {
            "schema_id": record.content_schema.schema_id,
            "version": record.content_schema.version,
            "uri": record.content_schema.uri,
            "sha256": record.content_schema.sha256,
        },
        "producer_principal_id": record.producer_principal_id,
        "origin_id": record.origin_id,
        "captured_at": record.captured_at,
        "observed_at": record.observed_at,
        "compartments": list(record.compartments),
        "provenance": {
            "schema_version": record.provenance.schema_version,
            "brain_id": record.provenance.brain_id,
            "source_record_ids": list(record.provenance.source_record_ids),
            "source_revision_ids": list(record.provenance.source_revision_ids),
            "processor": processor_document,
            "derivation": record.provenance.derivation,
        },
        "body": cast(dict[str, object], _json_value(record.body)),
        "ciphertext_state": record.ciphertext_state,
        "ciphertext_digest": record.ciphertext_digest,
    }


def batch_document(batch: CommitBatch) -> dict[str, object]:
    """Serialize the digest-bound commit-batch body; the digest is deliberately external."""
    if any(not isinstance(item, Record) for item in batch.items):
        raise PortabilityMappingError("shared import batches may contain only record items")
    return {
        "schema_version": batch.schema_version,
        "brain_id": batch.brain_id,
        "delivery_id": batch.delivery_id,
        "sequencer_epoch": batch.sequencer_epoch,
        "issuer_epoch": batch.issuer_epoch,
        "policy_digest": batch.policy_digest,
        "items": [record_document(cast(Record, item)) for item in batch.items],
    }


def _protocol_captured_at(source_timestamp: str | None) -> str | None:
    if source_timestamp is None:
        return None
    try:
        _validate_protocol_timestamp(source_timestamp, "captured_at")
    except ValueError:
        return None
    return source_timestamp


def _validate_protocol_timestamp(value: str, label: str) -> None:
    if not isinstance(value, str) or (matched := _PROTOCOL_TIMESTAMP.fullmatch(value)) is None:
        raise ValueError(f"{label} must be a canonical protocol timestamp")
    try:
        datetime(
            year=int(matched["year"]),
            month=int(matched["month"]),
            day=int(matched["day"]),
            hour=int(matched["hour"]),
            minute=int(matched["minute"]),
            second=int(matched["second"]),
            microsecond=int(matched["millisecond"] or "0") * 1_000,
            tzinfo=UTC,
        )
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical protocol timestamp") from error


def _sort_key(record: SharedRecord) -> tuple[str, int, str, str]:
    return (
        record.source_path,
        -1 if record.source_ordinal is None else record.source_ordinal,
        record.family,
        record.semantic_id,
    )


def _topological_records(shared: SharedBrain) -> tuple[SharedRecord, ...]:
    by_id = {record.semantic_id: record for record in shared.records}
    indegree = {record.semantic_id: len(record.provenance_ids) for record in shared.records}
    children: dict[str, list[str]] = {}
    for record in shared.records:
        for source_id in record.provenance_ids:
            children.setdefault(source_id, []).append(record.semantic_id)
    ready: list[tuple[tuple[str, int, str, str], str]] = [
        (_sort_key(record), record.semantic_id)
        for record in shared.records
        if indegree[record.semantic_id] == 0
    ]
    heapq.heapify(ready)
    ordered: list[SharedRecord] = []
    while ready:
        _, semantic_id = heapq.heappop(ready)
        ordered.append(by_id[semantic_id])
        for child_id in children.get(semantic_id, ()):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                child = by_id[child_id]
                heapq.heappush(ready, (_sort_key(child), child_id))
    if len(ordered) != len(shared.records):
        raise PortabilityMappingError("shared provenance cannot be topologically ordered")
    return tuple(ordered)


def _shared_body(shared: SharedBrain, record: SharedRecord) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "schema_version": 1,
            "source_contract": "portable-brain-v1",
            "source_brain_id": shared.source_brain_id,
            "semantic_family": record.family,
            "semantic_id": record.semantic_id,
            "source_schema_uri": record.schema_uri,
            "source_path": record.source_path,
            "source_ordinal": record.source_ordinal,
            "source_sha256": record.source_sha256,
            "source_bytes_base64": base64.b64encode(record.source_bytes).decode("ascii"),
            "actor_id": record.actor_id,
            "source_timestamp": record.source_timestamp,
            "space_id": record.space_id,
            "provenance_ids": list(record.provenance_ids),
        }
    )


def _full_plan_document(
    shared: SharedBrain,
    brain_id: str,
    records: tuple[Record, ...],
    batches: tuple[CommitBatch, ...],
) -> dict[str, object]:
    source_records = {record.semantic_id: record for record in shared.records}
    record_inventory: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record.origin_id, str) or record.origin_id not in source_records:
            raise PortabilityMappingError("import record identity map is incomplete")
        source = source_records[record.origin_id]
        record_inventory.append(
            {
                "family": source.family,
                "semantic_id": record.origin_id,
                "source_path": source.source_path,
                "source_ordinal": source.source_ordinal,
                "source_sha256": source.source_sha256,
                "record_id": record.record_id,
            }
        )
    return {
        "schema_version": 1,
        "source_brain_id": shared.source_brain_id,
        "target_brain_id": brain_id,
        "source_manifest": {
            "export_id": shared.evidence.export_id,
            "sha256": shared.evidence.manifest_sha256,
            "bytes_base64": base64.b64encode(shared.evidence.manifest_bytes).decode("ascii"),
            "files": [
                {"path": path, "sha256": digest}
                for path, digest in shared.evidence.declared_files
            ],
        },
        "records": record_inventory,
        "attachments": [
            {
                "kind": "source_blob",
                "path": blob.path,
                "sha256": blob.sha256,
                "size": len(blob.data),
            }
            for blob in shared.blobs
        ]
        + [
            {
                "kind": "owner_markdown",
                "path": attachment.path,
                "sha256": attachment.sha256,
                "size": len(attachment.data),
            }
            for attachment in shared.attachments
        ],
        "batches": [
            {"delivery_id": batch.delivery_id, "digest": batch.digest} for batch in batches
        ],
    }


@dataclass(frozen=True, slots=True)
class SecureNodeImportPlan:
    shared_brain: SharedBrain
    brain_id: str
    records: tuple[Record, ...]
    batches: tuple[CommitBatch, ...]
    full_plan_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.shared_brain, SharedBrain):
            raise PortabilityMappingError("import plan shared Brain is invalid")
        shared = replace(self.shared_brain)
        records = tuple(replace(record) for record in self.records)
        batches = tuple(replace(batch) for batch in self.batches)
        validate_identifier(self.brain_id, role="brain")
        ordered_sources = _topological_records(shared)
        expected_brain_id = reencode_portable_identifier(
            shared.source_brain_id, "tenant", "brn"
        )
        expected_origins = tuple(source.semantic_id for source in ordered_sources)
        actual_origins = tuple(record.origin_id for record in records)
        expected_record_ids = tuple(
            derive_import_record_id(
                shared.source_brain_id, source.family, source.semantic_id
            )
            for source in ordered_sources
        )
        if (
            self.brain_id != expected_brain_id
            or actual_origins != expected_origins
            or tuple(record.record_id for record in records) != expected_record_ids
        ):
            raise PortabilityMappingError("import record identity map is incomplete")
        all_items = tuple(item for batch in batches for item in tuple(batch.items))
        expected = tuple(item for item in all_items if isinstance(item, Record))
        if (
            not batches
            or len(expected) != len(all_items)
            or expected != records
            or any(
                len(tuple(batch.items)) > RESOURCE_LIMITS.commit_batch_items
                for batch in batches
            )
            or any(batch.brain_id != self.brain_id for batch in batches)
        ):
            raise PortabilityMappingError("import plan batches do not exactly bind its records")
        _validate_plan_digest(self.full_plan_digest)
        expected_digest = canonical_sha256(
            _full_plan_document(shared, self.brain_id, records, batches)
        )
        if self.full_plan_digest != expected_digest:
            raise PortabilityMappingError("full import plan digest mismatch")
        object.__setattr__(self, "shared_brain", shared)
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "batches", batches)

    def reconstruct_file_set(self) -> Mapping[str, bytes]:
        """Trusted test inverse for exact Portable conformance; not a Secure Node export."""
        return self.shared_brain.reconstruct_file_set()


def _validate_plan_digest(value: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PortabilityMappingError("full import plan digest is invalid")


def _validate_batches_purely(brain_id: str, batches: tuple[CommitBatch, ...]) -> None:
    state = BrainState.empty(brain_id)
    snapshot = ActiveLabelSetSnapshot.empty(brain_id)
    for batch in batches:
        application = apply_batch(state, batch, snapshot)
        staged = tuple(
            replace(record, ciphertext_state="unknown_historical", ciphertext_digest=None)
            for record in application.new_records
        )
        state = application.to_state(staged)
        snapshot = application.active_label_sets


def plan_secure_node_import(
    shared: SharedBrain, context: ImportEnvelopeContext
) -> SecureNodeImportPlan:
    """Build and validate a pure, bounded Secure Node import plan."""
    if not isinstance(shared, SharedBrain) or not isinstance(context, ImportEnvelopeContext):
        raise PortabilityMappingError("shared Brain and import context are required")
    shared = replace(shared)
    context = replace(context)
    context_compartments = tuple(context.compartments)
    delivery_ids = tuple(context.delivery_ids)
    brain_id = reencode_portable_identifier(shared.source_brain_id, "tenant", "brn")
    validate_identifier(brain_id, role="brain")
    expected_batches = (
        len(shared.records) + RESOURCE_LIMITS.commit_batch_items - 1
    ) // RESOURCE_LIMITS.commit_batch_items
    if len(delivery_ids) != expected_batches:
        raise PortabilityMappingError("exactly one delivery ID is required per import batch")
    for delivery_id in delivery_ids:
        try:
            validate_identifier(delivery_id, role="delivery", brain_id=brain_id)
        except ValueError as error:
            raise PortabilityMappingError("import delivery ID is invalid") from error

    ordered = _topological_records(shared)
    record_ids: dict[str, str] = {}
    seen_envelopes: set[str] = set()
    for source in ordered:
        record_id = derive_import_record_id(
            shared.source_brain_id, source.family, source.semantic_id
        )
        if record_id in seen_envelopes:
            raise PortabilityMappingError("derived record ID collision")
        validate_identifier(record_id, role="record", brain_id=brain_id)
        record_ids[source.semantic_id] = record_id
        seen_envelopes.add(record_id)

    schema = SchemaReference(
        schema_id="portable-record-envelope",
        version=1,
        uri=_SHARED_ENVELOPE_URI,
        sha256=sha256(shared_envelope_schema_bytes()).hexdigest(),
    )
    records = tuple(
        Record(
            brain_id=brain_id,
            record_id=record_ids[source.semantic_id],
            record_type=f"portable_brain_v1.{source.family}",
            content_schema=schema,
            producer_principal_id=reencode_portable_identifier(
                source.actor_id, "actor", "pri"
            ),
            origin_id=source.semantic_id,
            captured_at=_protocol_captured_at(source.source_timestamp),
            observed_at=context.observed_at,
            compartments=context_compartments,
            provenance=Provenance(
                brain_id=brain_id,
                source_record_ids=tuple(record_ids[item] for item in source.provenance_ids),
                derivation="import",
            ),
            body=_shared_body(shared, source),
            ciphertext_state="pending",
            ciphertext_digest=None,
        )
        for source in ordered
    )
    for record in records:
        validate_protocol_semantics("record", record_document(record))

    batches: list[CommitBatch] = []
    for index, offset in enumerate(
        range(0, len(records), RESOURCE_LIMITS.commit_batch_items)
    ):
        items = records[offset : offset + RESOURCE_LIMITS.commit_batch_items]
        batch_body = {
            "schema_version": 1,
            "brain_id": brain_id,
            "delivery_id": delivery_ids[index],
            "sequencer_epoch": context.sequencer_epoch,
            "issuer_epoch": context.issuer_epoch,
            "policy_digest": context.policy_digest,
            "items": [record_document(record) for record in items],
        }
        batch = CommitBatch(
            brain_id=brain_id,
            delivery_id=delivery_ids[index],
            digest=canonical_sha256(batch_body),
            sequencer_epoch=context.sequencer_epoch,
            issuer_epoch=context.issuer_epoch,
            policy_digest=context.policy_digest,
            items=items,
        )
        validate_protocol_semantics("commit-batch", batch_document(batch))
        batches.append(batch)
    frozen_batches = tuple(batches)
    try:
        _validate_batches_purely(brain_id, frozen_batches)
    except ValueError as error:
        raise PortabilityMappingError(
            "Secure Node semantic kernel rejected the import plan"
        ) from error
    full_plan_digest = canonical_sha256(
        _full_plan_document(shared, brain_id, records, frozen_batches)
    )
    return SecureNodeImportPlan(
        shared_brain=shared,
        brain_id=brain_id,
        records=records,
        batches=frozen_batches,
        full_plan_digest=full_plan_digest,
    )


__all__ = [
    "ImportEnvelopeContext",
    "SecureNodeImportPlan",
    "batch_document",
    "derive_import_record_id",
    "plan_secure_node_import",
    "record_document",
    "reencode_portable_identifier",
]
