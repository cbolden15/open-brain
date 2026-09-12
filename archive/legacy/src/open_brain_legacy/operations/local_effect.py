"""Durable local no-work execution for production jobs with an empty input batch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    WriteState,
    atomic_write_new,
    read_confined,
)

from .writer_jobs import (
    ApprovalBinding,
    EffectCommand,
    EffectParameter,
    EffectReceipt,
    EffectRecord,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
    WriterJobSpec,
)

_MAXIMUM_RECEIPT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class EmptyBatchApplication:
    """Prepare an explicit empty batch for one already-validated writer spec."""

    spec: WriterJobSpec

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        if (
            not isinstance(invocation, WriterJobInvocation)
            or invocation.job_id != self.spec.job_id
            or invocation.effect is not self.spec.effect
            or invocation.approved_records
            or invocation.approval_bindings
        ):
            raise WriterJobError("invalid empty scheduled batch")
        return PreparedEffect(effect=self.spec.effect)


class FilesystemEmptyEffectCapability:
    """Persist no-work replay evidence without claiming an application effect."""

    local_only = True

    def __init__(self, *, root: Path, spec: WriterJobSpec) -> None:
        if not isinstance(root, Path) or not isinstance(spec, WriterJobSpec):
            raise WriterJobError("invalid empty scheduled capability")
        self.root = root
        self.effect = spec.effect
        self.dry_run = spec.dry_run
        self._job_id = spec.job_id

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        receipt_path, _pointer_path = _paths(job_id, replay_key)
        payload = read_confined(root=self.root, relative=receipt_path)
        if payload is None:
            return None
        return _receipt_from_bytes(payload)

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        receipt_path, _pointer_path = _paths(command.job_id, command.replay_key)
        try:
            state = atomic_write_new(
                root=self.root,
                relative=receipt_path,
                data=_receipt_bytes(receipt),
            )
        except DuplicateConflictError:
            raise WriterJobError("empty scheduled reservation conflict") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
            raise WriterJobError("empty scheduled reservation conflict")
        if self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("empty scheduled reservation conflict")
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("empty scheduled reservation conflict")
        _receipt_path, pointer_path = _paths(command.job_id, command.replay_key)
        try:
            state = atomic_write_new(
                root=self.root,
                relative=pointer_path,
                data=canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "effect_digest_sha256": receipt.effect_digest_sha256,
                    }
                ),
            )
        except DuplicateConflictError:
            raise WriterJobError("empty scheduled pointer conflict") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
            raise WriterJobError("empty scheduled pointer conflict")

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        if (
            not isinstance(receipt, EffectReceipt)
            or receipt.job_id != self._job_id
            or receipt.effect is not self.effect
        ):
            raise WriterJobError("invalid empty scheduled receipt")
        receipt_path, pointer_path = _paths(receipt.job_id, receipt.replay_key)
        if _receipt_from_bytes(_required_payload(self.root, receipt_path)) != receipt:
            raise WriterJobError("empty scheduled reservation conflict")
        pointer = _pointer_from_bytes(_required_payload(self.root, pointer_path))
        if pointer != receipt.effect_digest_sha256:
            raise WriterJobError("empty scheduled pointer conflict")
        return PreparedEffect(effect=receipt.effect)

    def _validate_command(self, command: EffectCommand) -> None:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id != self._job_id
            or command.prepared != PreparedEffect(effect=self.effect)
        ):
            raise WriterJobError("invalid empty scheduled command")


class FilesystemPreparedEffectCapability:
    """Persist one local metadata effect with exact replay read-back."""

    local_only = True

    def __init__(self, *, root: Path, spec: WriterJobSpec) -> None:
        if not isinstance(root, Path) or not isinstance(spec, WriterJobSpec):
            raise WriterJobError("invalid prepared scheduled capability")
        self.root = root
        self.effect = spec.effect
        self.dry_run = spec.dry_run
        self._job_id = spec.job_id

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        receipt_path, _effect_path = _prepared_paths(job_id, replay_key)
        payload = read_confined(root=self.root, relative=receipt_path)
        return None if payload is None else _receipt_from_bytes(payload)

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        receipt_path, _effect_path = _prepared_paths(
            command.job_id,
            command.replay_key,
        )
        try:
            state = atomic_write_new(
                root=self.root,
                relative=receipt_path,
                data=_receipt_bytes(receipt),
            )
        except DuplicateConflictError:
            raise WriterJobError("prepared scheduled reservation conflict") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
            raise WriterJobError("prepared scheduled reservation conflict")
        if self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("prepared scheduled reservation conflict")
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("prepared scheduled reservation conflict")
        _receipt_path, effect_path = _prepared_paths(command.job_id, command.replay_key)
        try:
            state = atomic_write_new(
                root=self.root,
                relative=effect_path,
                data=_prepared_bytes(command.prepared),
            )
        except DuplicateConflictError:
            raise WriterJobError("prepared scheduled effect conflict") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
            raise WriterJobError("prepared scheduled effect conflict")

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        if (
            not isinstance(receipt, EffectReceipt)
            or receipt.job_id != self._job_id
            or receipt.effect is not self.effect
        ):
            raise WriterJobError("invalid prepared scheduled receipt")
        receipt_path, effect_path = _prepared_paths(receipt.job_id, receipt.replay_key)
        if _receipt_from_bytes(_required_payload(self.root, receipt_path)) != receipt:
            raise WriterJobError("prepared scheduled reservation conflict")
        payload = read_confined(root=self.root, relative=effect_path)
        if payload is None:
            return None
        prepared = _prepared_from_bytes(payload)
        if prepared.digest_sha256() != receipt.effect_digest_sha256:
            raise WriterJobError("prepared scheduled effect conflict")
        return prepared

    def _validate_command(self, command: EffectCommand) -> None:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id != self._job_id
            or command.prepared.effect is not self.effect
        ):
            raise WriterJobError("invalid prepared scheduled command")


def _paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    identity = sha256(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    ).hexdigest()
    base = PurePosixPath("operations/effects/empty") / identity
    return base.with_suffix(".json"), base.with_suffix(".applied.json")


def _prepared_paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    identity = sha256(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    ).hexdigest()
    base = PurePosixPath("operations/effects/prepared") / identity
    return base.with_suffix(".receipt.json"), base.with_suffix(".effect.json")


def _receipt_bytes(receipt: EffectReceipt) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "job_id": receipt.job_id,
            "replay_key": receipt.replay_key,
            "request_digest_sha256": receipt.request_digest_sha256,
            "effect": receipt.effect.value,
            "effect_digest_sha256": receipt.effect_digest_sha256,
            "records": [record.to_dict() for record in receipt.records],
            "review_item_ids": list(receipt.review_item_ids),
            "approval_bindings": [
                binding.to_dict() for binding in receipt.approval_bindings
            ],
            "parameters": [parameter.to_dict() for parameter in receipt.parameters],
        }
    )


def _receipt_from_bytes(payload: bytes) -> EffectReceipt:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAXIMUM_RECEIPT_BYTES:
        raise WriterJobError("invalid empty scheduled receipt")
    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError
        records = tuple(
            EffectRecord(
                record_id=item["record_id"],
                digest_sha256=item["digest_sha256"],
                approval=(
                    None
                    if item["approval"] is None
                    else ApprovalBinding(**item["approval"])
                ),
            )
            for item in value["records"]
        )
        receipt = EffectReceipt(
            job_id=value["job_id"],
            replay_key=value["replay_key"],
            request_digest_sha256=value["request_digest_sha256"],
            effect=ScheduledEffect(value["effect"]),
            effect_digest_sha256=value["effect_digest_sha256"],
            records=records,
            review_item_ids=tuple(value["review_item_ids"]),
            approval_bindings=tuple(
                ApprovalBinding(**item) for item in value["approval_bindings"]
            ),
            parameters=tuple(
                EffectParameter(name=item["name"], value=item["value"])
                for item in value["parameters"]
            ),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid empty scheduled receipt") from None
    if _receipt_bytes(receipt) != payload:
        raise WriterJobError("invalid empty scheduled receipt")
    return receipt


def _prepared_bytes(prepared: PreparedEffect) -> bytes:
    return canonical_json_bytes({"schema_version": 1, **prepared.to_dict()})


def _prepared_from_bytes(payload: bytes) -> PreparedEffect:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAXIMUM_RECEIPT_BYTES:
        raise WriterJobError("invalid prepared scheduled effect")
    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError
        prepared = PreparedEffect(
            effect=ScheduledEffect(value["effect"]),
            records=tuple(
                EffectRecord(
                    record_id=item["record_id"],
                    digest_sha256=item["digest_sha256"],
                    approval=(
                        None
                        if item["approval"] is None
                        else ApprovalBinding(**item["approval"])
                    ),
                )
                for item in value["records"]
            ),
            review_item_ids=tuple(value["review_item_ids"]),
            parameters=tuple(
                EffectParameter(name=item["name"], value=item["value"])
                for item in value.get("parameters", [])
            ),
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid prepared scheduled effect") from None
    if _prepared_bytes(prepared) != payload:
        raise WriterJobError("invalid prepared scheduled effect")
    return prepared


def _pointer_from_bytes(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"))
        digest = value["effect_digest_sha256"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid empty scheduled pointer") from None
    expected = canonical_json_bytes(
        {"schema_version": 1, "effect_digest_sha256": digest}
    )
    if not isinstance(digest, str) or expected != payload:
        raise WriterJobError("invalid empty scheduled pointer")
    return digest


def _required_payload(root: Path, relative: PurePosixPath) -> bytes:
    payload = read_confined(root=root, relative=relative)
    if payload is None:
        raise WriterJobError("empty scheduled evidence missing")
    return payload
