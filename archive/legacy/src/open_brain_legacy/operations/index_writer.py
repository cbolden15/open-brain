from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.engine import LockScope
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    atomic_write_new,
    read_confined,
)

from .index import (
    INDEX_SCHEMA_VERSION,
    EmbeddingPort,
    IndexRoots,
    build_index,
    check_index,
)
from .models import DeploymentTarget, HostRole
from .writer_jobs import (
    EffectCommand,
    EffectReceipt,
    EffectRecord,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
)

_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "replay_key",
        "request_digest_sha256",
        "effect",
        "effect_digest_sha256",
        "records",
        "review_item_ids",
        "approval_bindings",
    }
)
_RECORD_FIELDS = frozenset({"record_id", "digest_sha256", "approval"})
_POINTER_FIELDS = frozenset({"version", "effect_digest_sha256", "generation_id"})
_MAX_RECEIPT_BYTES = 16 * 1024
_MAX_POINTER_BYTES = 4 * 1024


@dataclass(frozen=True, slots=True)
class HeldScopeLease:
    """No-op domain lease proving the matching outer writer lease is already held."""

    held: LockScope

    def __post_init__(self) -> None:
        if not isinstance(self.held, LockScope) or self.held is LockScope.NONE:
            raise WriterJobError("invalid held lease scope")

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        if scope is not self.held:
            raise WriterJobError("domain lease scope conflicts with held writer lease")
        yield


@dataclass(frozen=True, slots=True)
class IndexWriterApplication:
    database_name: str
    embedding_model_id: str
    schema_version: int = INDEX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.database_name, str)
            or not self.database_name
            or "/" in self.database_name
            or "\\" in self.database_name
            or not isinstance(self.embedding_model_id, str)
            or not self.embedding_model_id
            or type(self.schema_version) is not int
            or self.schema_version != INDEX_SCHEMA_VERSION
        ):
            raise WriterJobError("invalid index writer application")

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        if (
            not isinstance(invocation, WriterJobInvocation)
            or invocation.job_id != "JOB-016"
            or invocation.effect is not ScheduledEffect.INDEX_REBUILD
            or invocation.approved_records
            or invocation.approval_bindings
        ):
            raise WriterJobError("invalid index writer invocation")
        return _prepared_index_effect(
            job_id=invocation.job_id,
            replay_key=invocation.replay_key,
            database_name=self.database_name,
            embedding_model_id=self.embedding_model_id,
            schema_version=self.schema_version,
        )


class IndexEffectCapability:
    effect = ScheduledEffect.INDEX_REBUILD
    local_only = True
    dry_run = False

    def __init__(
        self,
        *,
        root: Path,
        roots: IndexRoots,
        embedder: EmbeddingPort,
        privacy: PrivacyDecision,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not isinstance(roots, IndexRoots)
            or roots.output_root != root
            or not isinstance(privacy, PrivacyDecision)
            or not isinstance(getattr(embedder, "model_id", None), str)
        ):
            raise WriterJobError("invalid index effect capability")
        self.root = root
        self._roots = roots
        self._embedder = embedder
        self._embedding_model_id = embedder.model_id
        self._privacy = privacy

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        reservation, _pointer = _paths(job_id, replay_key)
        payload = read_confined(root=self.root, relative=reservation)
        if payload is None:
            return None
        return _receipt_from_bytes(payload)

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        reservation, _pointer = _paths(command.job_id, command.replay_key)
        try:
            atomic_write_new(
                root=self.root,
                relative=reservation,
                data=_receipt_bytes(receipt),
            )
        except DuplicateConflictError:
            raise WriterJobError("index effect reservation conflict") from None
        recovered = self.recover(command.job_id, command.replay_key)
        if recovered != receipt:
            raise WriterJobError("index effect reservation conflict")
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("index effect reservation conflict")
        result = build_index(
            target=DeploymentTarget.CANONICAL_WRITER,
            host_role=HostRole.WRITER,
            roots=self._roots,
            lease=HeldScopeLease(LockScope.INDEX),
            embedder=self._embedder,
            privacy=self._privacy,
        )
        _reservation, pointer = _paths(command.job_id, command.replay_key)
        pointer_payload = canonical_json_bytes(
            {
                "version": 1,
                "effect_digest_sha256": receipt.effect_digest_sha256,
                "generation_id": result.generation_id,
            }
        )
        try:
            atomic_write_new(root=self.root, relative=pointer, data=pointer_payload)
        except DuplicateConflictError:
            raise WriterJobError("index applied pointer conflict") from None

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        if not isinstance(receipt, EffectReceipt) or receipt.effect is not self.effect:
            raise WriterJobError("invalid index effect receipt")
        _reservation, pointer = _paths(receipt.job_id, receipt.replay_key)
        payload = read_confined(root=self.root, relative=pointer)
        if payload is None:
            return None
        generation_id, effect_digest = _pointer_from_bytes(payload)
        if effect_digest != receipt.effect_digest_sha256:
            raise WriterJobError("index applied pointer conflict")
        checked = check_index(target=DeploymentTarget.CANONICAL_WRITER, roots=self._roots)
        if not checked.available or checked.generation_id != generation_id:
            raise WriterJobError("index durable read-back conflict")
        return PreparedEffect(
            receipt.effect,
            receipt.records,
            receipt.review_item_ids,
            receipt.parameters,
        )

    def _validate_command(self, command: EffectCommand) -> None:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id != "JOB-016"
            or command.prepared
            != _prepared_index_effect(
                job_id=command.job_id,
                replay_key=command.replay_key,
                database_name=self._roots.database_name,
                embedding_model_id=self._embedding_model_id,
                schema_version=INDEX_SCHEMA_VERSION,
            )
        ):
            raise WriterJobError("invalid index effect command")


def _prepared_index_effect(
    *,
    job_id: str,
    replay_key: str,
    database_name: str,
    embedding_model_id: str,
    schema_version: int,
) -> PreparedEffect:
    payload = {
        "job_id": job_id,
        "replay_key": replay_key,
        "scope": LockScope.INDEX.value,
        "database_name": database_name,
        "embedding_model_id": embedding_model_id,
        "schema_version": schema_version,
    }
    digest = sha256(canonical_json_bytes(payload)).hexdigest()
    return PreparedEffect(
        effect=ScheduledEffect.INDEX_REBUILD,
        records=(EffectRecord(record_id="index_" + digest, digest_sha256=digest),),
    )


def _paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    identity = sha256(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    ).hexdigest()
    base = PurePosixPath("operations/effects/index") / identity
    return base.with_suffix(".json"), base.with_suffix(".applied.json")


def _receipt_bytes(receipt: EffectReceipt) -> bytes:
    return canonical_json_bytes(
        {
            "version": 1,
            "job_id": receipt.job_id,
            "replay_key": receipt.replay_key,
            "request_digest_sha256": receipt.request_digest_sha256,
            "effect": receipt.effect.value,
            "effect_digest_sha256": receipt.effect_digest_sha256,
            "records": [record.to_dict() for record in receipt.records],
            "review_item_ids": list(receipt.review_item_ids),
            "approval_bindings": [binding.to_dict() for binding in receipt.approval_bindings],
        }
    )


def _receipt_from_bytes(payload: bytes) -> EffectReceipt:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_RECEIPT_BYTES:
        raise WriterJobError("invalid index effect receipt")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _RECEIPT_FIELDS or value["version"] != 1:
            raise WriterJobError("invalid index effect receipt")
        raw_records = value["records"]
        if type(raw_records) is not list:
            raise WriterJobError("invalid index effect receipt")
        records: list[EffectRecord] = []
        for raw_record in raw_records:
            if (
                type(raw_record) is not dict
                or frozenset(raw_record) != _RECORD_FIELDS
                or raw_record["approval"] is not None
            ):
                raise WriterJobError("invalid index effect receipt")
            records.append(
                EffectRecord(
                    record_id=raw_record["record_id"],
                    digest_sha256=raw_record["digest_sha256"],
                )
            )
        if value["approval_bindings"] != [] or type(value["review_item_ids"]) is not list:
            raise WriterJobError("invalid index effect receipt")
        receipt = EffectReceipt(
            job_id=value["job_id"],
            replay_key=value["replay_key"],
            request_digest_sha256=value["request_digest_sha256"],
            effect=ScheduledEffect(value["effect"]),
            effect_digest_sha256=value["effect_digest_sha256"],
            records=tuple(records),
            review_item_ids=tuple(value["review_item_ids"]),
            approval_bindings=(),
        )
    except WriterJobError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid index effect receipt") from None
    if receipt.effect is not ScheduledEffect.INDEX_REBUILD or _receipt_bytes(receipt) != payload:
        raise WriterJobError("invalid index effect receipt")
    return receipt


def _pointer_from_bytes(payload: bytes) -> tuple[str, str]:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_POINTER_BYTES:
        raise WriterJobError("invalid index applied pointer")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _POINTER_FIELDS:
            raise WriterJobError("invalid index applied pointer")
        if value["version"] != 1:
            raise WriterJobError("invalid index applied pointer")
        generation_id = value["generation_id"]
        effect_digest = value["effect_digest_sha256"]
        if not isinstance(generation_id, str) or not isinstance(effect_digest, str):
            raise WriterJobError("invalid index applied pointer")
    except WriterJobError:
        raise
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid index applied pointer") from None
    if canonical_json_bytes(value) != payload:
        raise WriterJobError("invalid index applied pointer")
    return generation_id, effect_digest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
