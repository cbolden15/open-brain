"""Root-bound independent denial membership for historical reconciliation.

Storage primitives do not authorize admission. The engine's fenced owner task
must durably record its forward transition before changing this registry or its
SQL projection. Reads must validate both projections before external output.
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
from .sharing_contracts import SharingError, _digest, _identity, _text, _version

_REGISTRY_PATH = ".open-brain/historical-authority/historical-claims.v1.json"
_MAX_BYTES = 4 * 1024 * 1024
_MAX_MEMBERSHIPS = 16_384


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalClaimMembership(_ClosedComponent):
    capture_id: str
    source_id: str
    capture_source_id: str
    claim_role: str

    def __post_init__(self) -> None:
        _identity(self.capture_id, "capture_")
        _identity(self.source_id, "source_")
        _identity(self.capture_source_id, "source_")
        _text(self.claim_role)
        if self.claim_role not in ("baseline_original", "historical_copy"):
            raise SharingError("invalid_arguments")
        if self.claim_role == "baseline_original" and self.source_id != self.capture_source_id:
            raise SharingError("invalid_arguments")


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalClaimRegistry:
    destination: HistoricalDestination
    generation: int
    memberships: tuple[HistoricalClaimMembership, ...]
    registry_sha256: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _version(self.generation)
        _digest(self.registry_sha256)
        if (
            type(self.destination) is not HistoricalDestination
            or type(self.memberships) is not tuple
            or len(self.memberships) > _MAX_MEMBERSHIPS
            or any(type(item) is not HistoricalClaimMembership for item in self.memberships)
        ):
            raise SharingError("invalid_arguments")
        identities = tuple(item.capture_id for item in self.memberships)
        if identities != tuple(sorted(set(identities))) or self.generation < len(identities):
            raise SharingError("invalid_arguments")
        if _registry_digest(self._body()) != self.registry_sha256:
            raise SharingError("invalid_arguments")
        if len(self.canonical_bytes()) > _MAX_BYTES:
            raise SharingError("invalid_arguments")

    def _body(self) -> dict[str, object]:
        return {
            "dto_version": self.dto_version,
            "destination": self.destination.value(),
            "generation": self.generation,
            "memberships": [item.value() for item in self.memberships],
        }

    def canonical_bytes(self) -> bytes:
        return portable_canonical_json_bytes(
            dict(self._body(), registry_sha256=self.registry_sha256)
        )

    @classmethod
    def _create(
        cls,
        destination: HistoricalDestination,
        generation: int,
        memberships: tuple[HistoricalClaimMembership, ...],
    ) -> HistoricalClaimRegistry:
        body = {
            "dto_version": 1,
            "destination": destination.value(),
            "generation": generation,
            "memberships": [item.value() for item in memberships],
        }
        return cls(
            destination=destination,
            generation=generation,
            memberships=memberships,
            registry_sha256=_registry_digest(body),
        )

    @classmethod
    def empty(cls, destination: HistoricalDestination) -> HistoricalClaimRegistry:
        if type(destination) is not HistoricalDestination:
            raise SharingError("invalid_arguments")
        return cls._create(destination, 0, ())

    def register(self, membership: HistoricalClaimMembership) -> HistoricalClaimRegistry:
        if type(membership) is not HistoricalClaimMembership:
            raise SharingError("invalid_arguments")
        for current in self.memberships:
            if current.capture_id == membership.capture_id:
                if current != membership:
                    raise SharingError("invalid_arguments")
                return self
        memberships = tuple(
            sorted((*self.memberships, membership), key=lambda item: item.capture_id)
        )
        return self._create(self.destination, self.generation + 1, memberships)

    def verify_projection(
        self,
        *,
        generation: int,
        registry_sha256: str,
        memberships: tuple[HistoricalClaimMembership, ...],
    ) -> None:
        """Fence on any SQL projection discrepancy, before candidate ranking."""
        if (
            type(generation) is not int
            or generation != self.generation
            or type(registry_sha256) is not str
            or registry_sha256 != self.registry_sha256
            or type(memberships) is not tuple
            or any(type(item) is not HistoricalClaimMembership for item in memberships)
        ):
            raise SharingError("binding_mismatch")
        if tuple(sorted(memberships, key=lambda item: item.capture_id)) != self.memberships:
            raise SharingError("binding_mismatch")

    @classmethod
    def from_bytes(cls, raw: bytes) -> HistoricalClaimRegistry:
        if type(raw) is not bytes or not raw or len(raw) > _MAX_BYTES:
            raise SharingError("invalid_arguments")
        try:
            value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
            if (
                type(value) is not dict
                or set(value)
                != {
                    "dto_version",
                    "destination",
                    "generation",
                    "memberships",
                    "registry_sha256",
                }
                or type(value["memberships"]) is not list
            ):
                raise SharingError("invalid_arguments")
            registry = cls(
                destination=HistoricalDestination.from_value(value["destination"]),
                generation=value["generation"],
                memberships=tuple(
                    HistoricalClaimMembership.from_value(item) for item in value["memberships"]
                ),
                registry_sha256=value["registry_sha256"],
                dto_version=value["dto_version"],
            )
            if registry.canonical_bytes() != raw:
                raise SharingError("invalid_arguments")
            return registry
        except TypeError, ValueError, UnicodeError:
            raise SharingError("invalid_arguments") from None


def _registry_digest(body: dict[str, object]) -> str:
    return sha256(
        b"open-brain-historical-registry.v1\0" + portable_canonical_json_bytes(body)
    ).hexdigest()


class HistoricalRegistryStore:
    """Confined CAS primitive; caller owns exclusive admission and recovery."""

    def __init__(self, root: Path, root_identity: RootIdentity) -> None:
        self._root = root
        self._root_identity = root_identity

    def read(self, destination: HistoricalDestination) -> HistoricalClaimRegistry:
        raw = read_confined(
            root=self._root,
            relative=_REGISTRY_PATH,
            expected_root_identity=self._root_identity,
            maximum_bytes=_MAX_BYTES,
        )
        if raw is None:
            raise SharingError("binding_mismatch")
        registry = HistoricalClaimRegistry.from_bytes(raw)
        if registry.destination != destination:
            raise SharingError("binding_mismatch")
        return registry

    def initialize_empty(self, destination: HistoricalDestination) -> HistoricalClaimRegistry:
        registry = HistoricalClaimRegistry.empty(destination)
        atomic_write_new(
            root=self._root,
            relative=_REGISTRY_PATH,
            data=registry.canonical_bytes(),
            expected_root_identity=self._root_identity,
        )
        return self.read(destination)

    def advance(self, current: HistoricalClaimRegistry, proposed: HistoricalClaimRegistry) -> None:
        if (
            type(current) is not HistoricalClaimRegistry
            or type(proposed) is not HistoricalClaimRegistry
        ):
            raise SharingError("invalid_arguments")
        if (
            proposed.destination != current.destination
            or proposed.generation != current.generation + 1
            or not set(current.memberships).issubset(proposed.memberships)
        ):
            raise SharingError("invalid_arguments")
        atomic_replace(
            root=self._root,
            relative=_REGISTRY_PATH,
            data=proposed.canonical_bytes(),
            require_existing=True,
            expected_existing_sha256=sha256(current.canonical_bytes()).hexdigest(),
            expected_root_identity=self._root_identity,
        )
