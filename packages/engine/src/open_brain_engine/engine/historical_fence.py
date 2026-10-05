"""Persistent pending-operation read fence for historical forward recovery.

These filesystem primitives require the caller's existing exclusive admission.
The owner task must commit and verify SQL before marking complete; external reads
must verify both this fence and independent registry/SQL projection equality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    RootIdentity,
    atomic_replace,
    atomic_write_new,
    read_confined,
)

from .historical_contracts import HistoricalDestination, _ClosedComponent, _unique_object
from .historical_dispatch import TRANSITION_TYPES, Transition, VersionedHistoricalTransitionStore
from .historical_registry import HistoricalClaimRegistry, HistoricalRegistryStore
from .sharing_contracts import _OPERATION, SharingError, _digest, _text, _version

_PATH = ".open-brain/historical-authority/historical-fence.v1.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class _FenceHead(_ClosedComponent):
    destination: HistoricalDestination
    state: str
    operation_id: str | None
    transition_sha256: str | None
    registry_generation: int
    registry_sha256: str
    head_sha256: str
    dto_version: int = 1

    def value(self) -> dict[str, object]:
        value = super().value()
        value["destination"] = self.destination.value()
        return value

    def canonical_bytes(self) -> bytes:
        return portable_canonical_json_bytes(self.value())

    def __post_init__(self) -> None:
        if (
            _version(self.dto_version, minimum=1) != 1
            or type(self.destination) is not HistoricalDestination
        ):
            raise SharingError("invalid_arguments")
        _version(self.registry_generation)
        _digest(self.registry_sha256)
        _digest(self.head_sha256)
        _text(self.state)
        if self.state == "idle":
            if (
                self.registry_generation != 0
                or self.operation_id is not None
                or self.transition_sha256 is not None
            ):
                raise SharingError("invalid_arguments")
        elif self.state in ("pending", "complete"):
            _text(self.operation_id, pattern=_OPERATION)
            _digest(self.transition_sha256)
            _version(self.registry_generation, minimum=1)
        else:
            raise SharingError("invalid_arguments")
        body = {key: item for key, item in self.value().items() if key != "head_sha256"}
        if _head_digest(body) != self.head_sha256:
            raise SharingError("invalid_arguments")

    @classmethod
    def create(
        cls, *, registry: HistoricalClaimRegistry, state: str, record: Transition | None
    ) -> _FenceHead:
        body: dict[str, object] = {
            "dto_version": 1,
            "destination": registry.destination.value(),
            "state": state,
            "operation_id": None if record is None else record.request.operation_id,
            "transition_sha256": None if record is None else record.transition_sha256,
            "registry_generation": registry.generation,
            "registry_sha256": registry.registry_sha256,
        }
        return cls(
            destination=registry.destination,
            state=state,
            operation_id=None if record is None else record.request.operation_id,
            transition_sha256=None if record is None else record.transition_sha256,
            registry_generation=registry.generation,
            registry_sha256=registry.registry_sha256,
            head_sha256=_head_digest(body),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> _FenceHead:
        if type(raw) is not bytes or not raw or len(raw) > 4096:
            raise SharingError("binding_mismatch")
        try:
            value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
            if type(value) is not dict or set(value) != {
                "dto_version",
                "destination",
                "state",
                "operation_id",
                "transition_sha256",
                "registry_generation",
                "registry_sha256",
                "head_sha256",
            }:
                raise SharingError("binding_mismatch")
            value["destination"] = HistoricalDestination.from_value(value["destination"])
            head = cls(**value)
            if head.canonical_bytes() != raw:
                raise SharingError("binding_mismatch")
            return head
        except TypeError, ValueError, UnicodeError:
            raise SharingError("binding_mismatch") from None


def _head_digest(body: dict[str, object]) -> str:
    return sha256(
        b"open-brain-historical-fence.v1\0" + portable_canonical_json_bytes(body)
    ).hexdigest()


class HistoricalPendingFence:
    def __init__(self, root: Path, root_identity: RootIdentity) -> None:
        self._root, self._root_identity = root, root_identity
        self._records = VersionedHistoricalTransitionStore(root, root_identity)
        self._registry = HistoricalRegistryStore(root, root_identity)

    def _read(self) -> _FenceHead:
        raw = read_confined(
            root=self._root,
            relative=_PATH,
            maximum_bytes=4096,
            expected_root_identity=self._root_identity,
        )
        if raw is None:
            raise SharingError("binding_mismatch")
        return _FenceHead.from_bytes(raw)

    def _replace(self, previous: _FenceHead, proposed: _FenceHead) -> None:
        atomic_replace(
            root=self._root,
            relative=_PATH,
            data=proposed.canonical_bytes(),
            require_existing=True,
            expected_existing_sha256=sha256(previous.canonical_bytes()).hexdigest(),
            expected_root_identity=self._root_identity,
        )

    def initialize_empty(self, registry: HistoricalClaimRegistry) -> None:
        if (
            type(registry) is not HistoricalClaimRegistry
            or registry.generation != 0
            or registry.memberships
        ):
            raise SharingError("invalid_arguments")
        if self._registry.read(registry.destination) != registry:
            raise SharingError("binding_mismatch")
        head = _FenceHead.create(registry=registry, state="idle", record=None)
        atomic_write_new(
            root=self._root,
            relative=_PATH,
            data=head.canonical_bytes(),
            expected_root_identity=self._root_identity,
        )

    def _bound_record(self, head: _FenceHead) -> Transition:
        if head.operation_id is None:
            raise SharingError("binding_mismatch")
        record = self._records.read(head.operation_id)
        if (
            record.transition_sha256 != head.transition_sha256
            or record.request.destination != head.destination
            or record.proposed.generation != head.registry_generation
            or record.proposed.registry_sha256 != head.registry_sha256
        ):
            raise SharingError("binding_mismatch")
        return record

    def pending(self) -> Transition | None:
        head = self._read()
        if head.state == "idle":
            return None
        record = self._bound_record(head)
        return record if head.state == "pending" else None

    def prepare(self, record: Transition) -> None:
        if type(record) not in TRANSITION_TYPES:
            raise SharingError("invalid_arguments")
        head = self._read()
        if head.state != "idle" and head.operation_id == record.request.operation_id:
            if self._bound_record(head) != record:
                raise SharingError("binding_mismatch")
            return
        if head.state == "pending":
            raise SharingError("operation_pending")
        if (
            head.destination != record.request.destination
            or head.registry_generation != record.previous.generation
            or head.registry_sha256 != record.previous.registry_sha256
            or self._registry.read(head.destination) != record.previous
        ):
            raise SharingError("binding_mismatch")
        if head.state == "complete":
            self._bound_record(head)
        self._records.persist(record)
        self._replace(
            head, _FenceHead.create(registry=record.proposed, state="pending", record=record)
        )

    def mark_complete(self, record: Transition, registry: HistoricalClaimRegistry) -> None:
        if type(record) not in TRANSITION_TYPES or type(registry) is not HistoricalClaimRegistry:
            raise SharingError("invalid_arguments")
        head = self._read()
        if (
            head.state == "idle"
            or self._bound_record(head) != record
            or registry != record.proposed
        ):
            raise SharingError("binding_mismatch")
        if self._registry.read(registry.destination) != registry:
            raise SharingError("binding_mismatch")
        if head.state == "pending":
            self._replace(
                head, _FenceHead.create(registry=registry, state="complete", record=record)
            )

    def assert_settled(self, registry: HistoricalClaimRegistry) -> None:
        if type(registry) is not HistoricalClaimRegistry:
            raise SharingError("invalid_arguments")
        head = self._read()
        if head.state == "pending":
            raise SharingError("operation_pending")
        if (
            head.destination != registry.destination
            or head.registry_generation != registry.generation
            or head.registry_sha256 != registry.registry_sha256
            or self._registry.read(registry.destination) != registry
        ):
            raise SharingError("binding_mismatch")
        if head.state == "complete":
            self._bound_record(head)
