"""Owner-authorized historical checkpoints without ordinary startup recovery.

The trusted caller authenticates retained old-to-current root continuity on every
use. This module accepts only device renumbering at an unchanged path/inode and
logical destination. Original observations remain byte-for-byte historical;
checkpoint witnesses bind the currently admitted physical root.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import RootIdentity, StorageError, assert_root_identity
from open_brain_engine.storage.locks import FileLease

from .contracts import LocalEngineContext, PublicJobCaptureSink
from .historical_checkpoint import (
    HistoricalBaselineDuplicate,
    HistoricalBaselineTemplate,
    historical_baseline_template,
    lookup_historical_baseline,
)
from .historical_contracts import HistoricalDestination
from .historical_recovery import require_historical_snapshot_settled
from .historical_source import historical_baseline_for_namespace
from .local_schema import open_local_database_read_only
from .normalization import _utc_now
from .sharing import _owner_local
from .sharing_contracts import SharingError
from .source_intake import SourceRevisionBinding, SourceRevisionObservedDelivery
from .t03_contracts import EffectiveAuthority, T03Error


@dataclass(frozen=True, slots=True)
class HistoricalRootContinuity:
    """Authenticated caller coordinates, not a self-authenticating grant.

    An owner-local caller must verify its durable continuity evidence through the
    required validator. A hash or matching inode alone never supplies authorization.
    """

    root: Path
    previous_root_identity: RootIdentity
    current_root_identity: RootIdentity
    tenant_id: str
    destination: HistoricalDestination

    def validate(self, profile: LocalEngineContext) -> None:
        identities = (self.previous_root_identity, self.current_root_identity)
        if (
            type(self.root) is not type(profile.root)
            or self.root != profile.root
            or not self.root.is_absolute()
            or self.root.resolve(strict=True) != self.root
            or any(
                type(identity) is not tuple
                or len(identity) != 2
                or any(type(part) is not int or part < 0 for part in identity)
                for identity in identities
            )
            or self.previous_root_identity[1] != self.current_root_identity[1]
            or self.current_root_identity != profile.root_identity
            or self.tenant_id != profile.tenant_id
            or type(self.destination) is not HistoricalDestination
        ):
            raise T03Error("invalid_arguments")
        assert_root_identity(profile.root, profile.root_identity)

    def fingerprint(self, identity: RootIdentity) -> str:
        return PublicJobCaptureSink.fingerprint_for(str(self.root), identity, self.tenant_id)


class HistoricalRootCheckpointHost:
    """Read-only history lookup plus the existing canonical checkpoint fence.

    No BrainEngine is constructed. No ingestion/source/workspace replay, migration,
    capture, ordinary revision, or historical rewrite capability is exposed.
    """

    def __init__(
        self,
        profile: LocalEngineContext,
        continuity: HistoricalRootContinuity,
        *,
        authority: EffectiveAuthority,
        validate_continuity: Callable[[], None],
    ) -> None:
        if type(continuity) is not HistoricalRootContinuity or not callable(validate_continuity):
            raise T03Error("invalid_arguments")
        _owner_local(authority)
        if authority.principal_id != profile.owner_actor_id:
            raise SharingError("unsupported_capability")
        self.profile = profile
        self.continuity = continuity
        self._validate_continuity = validate_continuity
        self._validate()
        self._lease = FileLease(
            profile.root / ".open-brain",
            "historical-checkpoint-" + sha256(profile.owner_actor_id.encode()).hexdigest()[:32],
            clock=_utc_now,
            validate_acquire=self._validate,
            parent_root_identity=profile.root_identity,
        )
        with self._snapshot() as connection:
            require_historical_snapshot_settled(connection, self.profile)

    def _validate(self) -> None:
        self.continuity.validate(self.profile)
        self._validate_continuity()
        self.continuity.validate(self.profile)

    def _connect(self) -> sqlite3.Connection:
        self._validate()
        return open_local_database_read_only(self.profile)

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(self._connect()) as connection:
                self._verify_snapshot(connection)
                yield connection
                self._validate()
        except SharingError, StorageError:
            raise T03Error("revision_changed") from None

    def _verify_snapshot(self, connection: sqlite3.Connection) -> None:
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        destination = self.continuity.destination
        if identity is None or tuple(identity) != (destination.brain_id, destination.issuer_epoch):
            raise SharingError("binding_mismatch")

    def capability(self, binding: SourceRevisionBinding) -> HistoricalRootCheckpointCapability:
        """Translate only an authenticated baseline's historical coordinates."""
        destination = self.continuity.destination
        if (
            type(binding) is not SourceRevisionBinding
            or binding.destination_brain_id != destination.brain_id
            or binding.issuer_epoch != destination.issuer_epoch
            or binding.root_fingerprint != self.continuity.fingerprint(self.profile.root_identity)
        ):
            raise T03Error("invalid_arguments")
        namespace_sha = sha256(portable_canonical_json_bytes(dict(binding.namespace))).hexdigest()
        with self._snapshot() as connection:
            baseline = historical_baseline_for_namespace(connection, self.profile, namespace_sha)
            if baseline is None:
                raise T03Error("revision_changed")
            original = baseline.observed_delivery.binding
            expected = SourceRevisionBinding(
                destination_brain_id=binding.destination_brain_id,
                issuer_epoch=binding.issuer_epoch,
                root_fingerprint=self.continuity.fingerprint(
                    self.continuity.previous_root_identity
                ),
                accepted_source_id=binding.accepted_source_id,
                namespace=binding.namespace,
            )
            if original != expected:
                raise T03Error("revision_changed")
            return HistoricalRootCheckpointCapability(self, original)


class HistoricalRootCheckpointCapability:
    """Historical lookup/checkpoint only; no normal submission methods."""

    def __init__(self, host: HistoricalRootCheckpointHost, binding: SourceRevisionBinding) -> None:
        self._host = host
        self.binding = binding

    def baseline_template(self) -> HistoricalBaselineTemplate | None:
        with self._host._snapshot() as connection:
            return historical_baseline_template(connection, self._host.profile, self.binding)

    def lookup_baseline(
        self, delivery: SourceRevisionObservedDelivery, *, selection_generation: str
    ) -> HistoricalBaselineDuplicate | None:
        if type(delivery) is not SourceRevisionObservedDelivery or delivery.binding != self.binding:
            raise T03Error("invalid_arguments")
        with self._host._snapshot() as connection:
            return lookup_historical_baseline(
                connection, self._host.profile, delivery, selection_generation=selection_generation
            )

    @contextmanager
    def revision_page_checkpoint(
        self,
        entries: tuple[tuple[SourceRevisionObservedDelivery, HistoricalBaselineDuplicate], ...],
        *,
        selection_generation: str,
    ) -> Iterator[None]:
        if type(entries) is not tuple or not 1 <= len(entries) <= 25:
            raise T03Error("invalid_arguments")
        seen: set[bytes] = set()
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise T03Error("invalid_arguments")
            delivery, witness = entry
            if (
                type(delivery) is not SourceRevisionObservedDelivery
                or type(witness) is not HistoricalBaselineDuplicate
                or delivery.binding.root_fingerprint != self.binding.root_fingerprint
                or delivery.binding.destination_brain_id != self.binding.destination_brain_id
                or delivery.binding.issuer_epoch != self.binding.issuer_epoch
                or delivery.binding.accepted_source_id != self.binding.accepted_source_id
                or any(
                    delivery.binding.namespace[key] != self.binding.namespace[key]
                    for key in ("connector_name", "connection_id", "resource_id")
                )
            ):
                raise T03Error("invalid_arguments")
            namespace = delivery.submission.namespace_bytes()
            if namespace in seen:
                raise T03Error("invalid_arguments")
            seen.add(namespace)
        with self._host._lease.acquire_shared_writer(), self._host._snapshot() as connection:
            for delivery, witness in entries:
                current = lookup_historical_baseline(
                    connection,
                    self._host.profile,
                    delivery,
                    selection_generation=selection_generation,
                )
                if current is None or current != witness:
                    raise T03Error("revision_changed")
            self._host._validate()
            yield
            self._host._validate()
