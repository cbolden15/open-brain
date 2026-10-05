"""Owner-exclusive forward completion of a retained historical transition."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING

from open_brain_engine.storage.locks import FileLease
from open_brain_engine.storage.sqlite import SchemaError, begin_immediate, connect_database

from .contracts import LocalEngineContext
from .historical_admission import verify_historical_source_cas
from .historical_contracts import HistoricalDestination
from .historical_dispatch import (
    BASELINE_TYPES,
    CLAIM_TYPES,
    RELATION_TYPES,
    Receipt,
    RelationRequest,
    verify_historical_baseline_evidence,
    verify_historical_relation_evidence,
    verify_retained_capture,
)
from .historical_fence import HistoricalPendingFence
from .historical_projection import (
    _append_historical_projection,
    verify_versioned_historical_projection,
)
from .historical_registry import HistoricalRegistryStore
from .local_schema import PHASE1_STATE_DATABASE, classify_local_schema
from .normalization import _utc_now
from .runtime_admission import HeldRuntimeAdmission
from .sharing import _owner_local
from .sharing_contracts import SharingError
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


def open_historical_recovery_database_read_only(
    profile: LocalEngineContext,
) -> sqlite3.Connection:
    """Accept only validated history schemas, without migration or bootstrap."""
    from .local_schema import open_local_database_read_only

    connection = open_local_database_read_only(profile, allow_old=True)
    state = classify_local_schema(connection)
    if state.state not in {"current", "supported_old"} or state.version not in {13, 14}:
        connection.close()
        raise SchemaError("historical recovery requires schema13 or schema14")
    return connection


def require_historical_settled(profile: LocalEngineContext) -> None:
    """Read-only startup preflight before any ordinary engine writer runs."""
    from .local_schema import open_local_database_read_only

    connection = open_local_database_read_only(profile, allow_old=True)
    try:
        require_historical_snapshot_settled(connection, profile)
    finally:
        connection.close()


def require_historical_snapshot_settled(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> None:
    """Ordinary writes require exact independent authority in their SQL snapshot."""
    if connection.execute("PRAGMA user_version").fetchone()[0] < 13:
        return
    identity = connection.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise SharingError("binding_mismatch")
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(destination)
    HistoricalPendingFence(profile.root, profile.root_identity).assert_settled(registry)
    verify_versioned_historical_projection(connection, profile, registry)


@contextmanager
def _historical_transaction(
    profile: LocalEngineContext, validate_before_write: Callable[[], None]
) -> Iterator[sqlite3.Connection]:
    """Open existing state without bootstrap or ordinary registration hooks."""

    # Refuse absent or unsupported state in the connection preparation callback before
    # storage can initialize WAL or adjust files. This path never migrates.
    def prepare(connection: sqlite3.Connection, created: bool, setup_required: bool) -> None:
        state = classify_local_schema(connection)
        if (created or state.state not in {"current", "supported_old"}
                or state.version not in {13, 14}):
            raise SchemaError("historical recovery requires schema13 or schema14")

    validate_before_write()
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
        prepare=prepare,
    )
    try:
        begin_immediate(connection)
        state = classify_local_schema(connection)
        if state.state not in {"current", "supported_old"} or state.version not in {13, 14}:
            raise SchemaError("local state schema changed before recovery")
        yield connection
        validate_before_write()
        connection.execute("COMMIT")
    except BaseException:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def recover_historical_profile(
    profile: LocalEngineContext,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    validate_before_write: Callable[[], None],
    clock: Callable[[], datetime] = _utc_now,
    checkpoint: Callable[[str], None] = lambda _stage: None,
    validate_relation_consent: Callable[[RelationRequest], None] | None = None,
) -> Receipt | None:
    """Owner maintenance before engine startup, without ordinary writer hooks."""
    _owner_local(authority)
    admission.validate(profile)
    if admission.live_peer_count:
        raise SharingError("operation_pending")
    validate_before_write()
    # Read-only classification also prevents creating a missing database.
    connection = open_historical_recovery_database_read_only(profile)
    connection.close()

    def validate() -> None:
        admission.validate(profile)
        validate_before_write()

    lease = FileLease(
        profile.root / ".open-brain",
        "engine-" + sha256(profile.owner_actor_id.encode("utf-8")).hexdigest()[:32],
        clock=clock,
        validate_acquire=validate,
        parent_root_identity=profile.root_identity,
    )
    with lease.acquire_shared_writer():
        return _recover_historical(
            profile,
            validate_before_write=validate,
            checkpoint=checkpoint,
            validate_relation_consent=validate_relation_consent,
        )


def recover_historical_pending(
    engine: BrainEngine,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Receipt | None:
    """Complete only the exact on-disk pending intent, never caller JSON.

    This returns historical operation truth, not a current-eligibility or
    independently protected custody receipt. It cannot capture, approve new
    content, retarget, replace evidence or roll back the independent registry.
    """
    _owner_local(authority)
    profile = engine.profile
    admission.validate(profile)
    if admission.live_peer_count:
        raise SharingError("operation_pending")

    def validate() -> None:
        if engine._validate_mutation_authority is not None:
            engine._validate_mutation_authority()

    return recover_historical_profile(
        profile,
        authority=authority,
        admission=admission,
        validate_before_write=validate,
        clock=engine._clock,
        checkpoint=checkpoint,
    )


def _recover_historical(
    profile: LocalEngineContext,
    *,
    validate_before_write: Callable[[], None],
    checkpoint: Callable[[str], None],
    validate_relation_consent: Callable[[RelationRequest], None] | None = None,
) -> Receipt | None:
    fence = HistoricalPendingFence(profile.root, profile.root_identity)
    registry_store = HistoricalRegistryStore(profile.root, profile.root_identity)
    # Both entrypoints hold the same root-bound engine writer lease.
    validate_before_write()
    record = fence.pending()
    pending_relation: RelationRequest | None = None

    def validate_pending() -> None:
        validate_before_write()
        if pending_relation is not None:
            if validate_relation_consent is None:
                raise SharingError("unsupported_capability")
            validate_relation_consent(pending_relation)

    with _historical_transaction(profile, validate_pending) as connection:
        if (record is not None and connection.execute("PRAGMA user_version").fetchone()[0] == 13
                and record.request.dto_version != 1):
            raise SharingError("binding_mismatch")
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        if identity is None:
            raise SharingError("binding_mismatch")
        destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
        registry = registry_store.read(destination)
        if record is None:
            fence.assert_settled(registry)
            verify_versioned_historical_projection(connection, profile, registry)
            return None
        if record.request.destination != destination or registry not in (
            record.previous,
            record.proposed,
        ):
            raise SharingError("binding_mismatch")
        state = connection.execute(
            "SELECT generation FROM historical_registry_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise SharingError("binding_mismatch")
        if state[0] == record.previous.generation:
            prior = verify_versioned_historical_projection(connection, profile, record.previous)
            request = record.request
            verify_historical_source_cas(connection, request.source_cas)
            if isinstance(request, BASELINE_TYPES):
                verify_historical_baseline_evidence(connection, profile, request)
            elif isinstance(request, CLAIM_TYPES):
                verify_historical_source_cas(connection, request.capture_source_cas)
                verify_retained_capture(
                    connection,
                    profile,
                    request.retained_capture,
                    request.capture_source_cas.source_id,
                )
            elif isinstance(request, RELATION_TYPES):
                baseline = next(
                    (
                        item.request
                        for item in prior
                        if item.request.operation_id == request.baseline_operation_id
                    ),
                    None,
                )
                if not isinstance(baseline, BASELINE_TYPES):
                    raise SharingError("binding_mismatch")
                verify_historical_relation_evidence(connection, profile, request, baseline)
                pending_relation = request
                validate_pending()
            checkpoint("historical_recovery_preflight")
            validate_pending()
            if registry == record.previous:
                registry_store.advance(record.previous, record.proposed)
            checkpoint("historical_registry_advanced")
            validate_pending()
            _append_historical_projection(connection, profile, record)
            checkpoint("historical_sql_projected")
        elif state[0] == record.proposed.generation:
            if registry != record.proposed:
                raise SharingError("binding_mismatch")
            verify_versioned_historical_projection(connection, profile, record.proposed)
        else:
            raise SharingError("binding_mismatch")
    checkpoint("historical_sql_committed")
    # A second exact snapshot protects the completion marker from claiming
    # success after a changed or incomplete projection.
    validate_before_write()
    with _historical_transaction(profile, validate_before_write) as connection:
        verify_versioned_historical_projection(connection, profile, record.proposed)
        validate_before_write()
        fence.mark_complete(record, record.proposed)
    checkpoint("historical_recovery_complete")
    return record.receipt
