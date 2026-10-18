"""Daemonless bootstrap for one default Open Brain."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from open_brain_engine.engine import (
    PHASE1_STATE_SCHEMA_VERSION,
    EngineTaskSet,
    LocalEngineContext,
    ReceiptProtectionPort,
    StateSchemaUnavailableError,
    coordinate_local_migration,
    inspect_phase1_state,
    open_local_engine,
)
from open_brain_engine.engine.embedding_backend import EmbeddingBackend
from open_brain_engine.engine.runtime_admission import (
    HeldRuntimeAdmission,
    exclusive_runtime_admission,
)
from open_brain_engine.storage.locks import FileLease, HeldWriterLease

from open_brain.local_data import (
    FilesystemTypeProbe,
    LocalRootSelection,
    PreparedLocalRoot,
    prepare_local_root,
)
from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.local_runtime_session import (
    LocalRuntimeCompatibilityError,
    LocalRuntimeSession,
    hold_local_runtime_session,
)

_STATE_DATABASE = ".open-brain/state/phase1.sqlite3"
_ACTIVE_TRANSACTION: ContextVar[LocalBrainTransaction | None] = ContextVar(
    "local_brain_transaction", default=None
)


class LocalBrainTransaction:
    """One existing Brain's registry and writer held around synchronous SDK operations."""

    def __init__(
        self, profile: LocalEngineContext, admission: HeldRuntimeAdmission, writer: HeldWriterLease
    ) -> None:
        self.profile = profile
        self._admission = admission
        self._writer = writer
        self._running = False

    def validate(self) -> None:
        """Reject expired ownership, another process/thread, or replaced controls."""
        try:
            self._admission.validate(self.profile)
            self._writer.validate()
        except Exception:
            raise RuntimeError("local Brain transaction ownership is unavailable") from None

    def run[T](self, operation: Callable[[], T]) -> T:
        """Borrow the held barriers only during a single synchronous SDK callback."""
        self.validate()
        if self._running or _ACTIVE_TRANSACTION.get() is not None:
            raise RuntimeError("local Brain transaction operation is already active")
        self._running = True
        token = _ACTIVE_TRANSACTION.set(self)
        try:
            with self._writer.borrow():
                result = operation()
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise RuntimeError("local Brain transaction requires a synchronous operation")
                self.validate()
                return result
        finally:
            _ACTIVE_TRANSACTION.reset(token)
            self._running = False


@contextmanager
def hold_existing_local_brain_transaction(root: Path) -> Iterator[LocalBrainTransaction]:
    """Hold canonical barriers before bootstrap, through caller-owned proof and release.

    Requires an initialized existing root; does not create a Brain or initialize its
    lock/registry control layout. Engine/session startup and cleanup occur in ``run``.
    The caller owns protection and result-release authorization outside the callback.
    """
    if _ACTIVE_TRANSACTION.get() is not None:
        raise RuntimeError("nested local Brain transaction")
    profile = open_existing_single_user_local(root)
    for relative in (
        _STATE_DATABASE,
        ".open-brain/runtime-sessions/registry.lock",
        ".open-brain/runtime-sessions/registry-version",
        ".open-brain/.open-brain-locks/lease.shared-writer",
    ):
        if not (profile.root / relative).is_file():
            raise RuntimeError("existing local Brain transaction controls are unavailable")
    with exclusive_runtime_admission(profile) as admission:
        lease = FileLease(
            profile.root / ".open-brain",
            "local-transaction",
            parent_root_identity=profile.root_identity,
        )
        with lease.hold_writer_transaction() as writer:
            transaction = LocalBrainTransaction(profile, admission, writer)
            transaction.validate()
            yield transaction


def run_existing_local_brain_transaction[T](root: Path, operation: Callable[[], T]) -> T:
    """Run under existing barriers, or borrow the active same-root callback.

    Borrowing preserves the original transaction's PID/thread and inode checks
    without nesting ``run`` or opening another writer lock descriptor.
    """
    transaction = _ACTIVE_TRANSACTION.get()
    if transaction is None:
        with hold_existing_local_brain_transaction(root) as transaction:
            return transaction.run(operation)
    transaction.validate()
    if root.resolve() != transaction.profile.root:
        raise RuntimeError("local Brain transaction root mismatch")
    result = operation()
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise RuntimeError("local Brain transaction requires a synchronous operation")
    transaction.validate()
    return result


@dataclass(frozen=True, slots=True)
class LocalInitReceipt:
    status: str
    profile: str
    brain_count: int
    storage: str
    daemon_running: bool
    application_encryption: bool
    state_schema_version: int

    def to_dict(self) -> dict[str, object]:
        return {
            "application_encryption": self.application_encryption,
            "brain_count": self.brain_count,
            "daemon_running": self.daemon_running,
            "profile": self.profile,
            "state_schema_version": self.state_schema_version,
            "status": self.status,
            "storage": self.storage,
        }


@dataclass(frozen=True, slots=True)
class LocalBrainSession:
    """One directly opened Brain while its selected root identity remains pinned."""

    initialized_before: bool
    prepared: PreparedLocalRoot
    profile: LocalEngineContext
    tasks: EngineTaskSet


def installed_search_backend(brain_root: Path) -> Callable[[], EmbeddingBackend]:
    """Lazy installed-helper factory; the engine calls it only for a non-lexical search."""

    def construct() -> EmbeddingBackend:
        from .embedding_helper import HelperEmbeddingBackend, discover_embedding_resource

        return HelperEmbeddingBackend(discover_embedding_resource(), brain_root)

    return construct


@contextmanager
def open_local_brain(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
    receipt_protection_port: ReceiptProtectionPort | None = None,
    receipt_protection_timeout_seconds: float = 5.0,
    existing_only: bool = False,
) -> Iterator[LocalBrainSession]:
    """Bootstrap and hold one default Brain through a direct local operation."""
    existing_profile = (
        open_existing_single_user_local(selection.brain_root) if existing_only else None
    )
    transaction = _ACTIVE_TRANSACTION.get()
    if transaction is not None:
        transaction.validate()
        if selection.brain_root.resolve() != transaction.profile.root:
            raise RuntimeError("local Brain transaction root mismatch")
    with prepare_local_root(selection, filesystem_type_probe=filesystem_type_probe) as prepared:
        identity_exists = prepared.private_file_exists("brain.toml")
        initialized_before = identity_exists and prepared.private_file_exists(_STATE_DATABASE)
        prepared.revalidate()
        profile = (
            existing_profile
            if existing_profile is not None
            else compile_single_user_local(
                selection.brain_root,
                validate_before_identity_write=prepared.revalidate,
            )
        )
        initial_schema = inspect_phase1_state(profile)
        if existing_only and (
            initial_schema.state != "current" or profile.root_identity != prepared.root_identity
        ):
            raise StateSchemaUnavailableError("existing current Brain required")
        if transaction is not None:
            transaction.validate()
            if (
                transaction.profile.root != profile.root
                or transaction.profile.root_identity != profile.root_identity
            ):
                raise RuntimeError("local Brain transaction root mismatch")
        from open_brain_engine.engine.source_migration import migration_pending

        if initial_schema.state in {"invalid", "newer"} and not migration_pending(profile):
            raise StateSchemaUnavailableError(f"local state schema is {initial_schema.state}")

        def validate_direct_write() -> None:
            prepared.revalidate()

        tasks: EngineTaskSet | None = None

        def admit(runtime_session: LocalRuntimeSession) -> None:
            nonlocal tasks
            schema = inspect_phase1_state(profile)
            if schema.state != "current" and runtime_session.live_peer_count > 0:
                raise LocalRuntimeCompatibilityError(
                    "state migration requires exclusive runtime admission"
                )
            # The owner product bootstrap owns the explicit chained migration;
            # hold_local_runtime_session's registry admission spans both phases.
            if existing_only:
                if schema.state != "current":
                    raise StateSchemaUnavailableError("existing current Brain required")
            else:
                coordinate_local_migration(profile)
            from open_brain_engine.engine.historical_recovery import require_historical_settled

            if inspect_phase1_state(profile).state != "absent":
                require_historical_settled(profile)
            if existing_only:
                tasks = open_local_engine(
                    profile,
                    validate_before_write=validate_direct_write,
                    recover_abandoned_sessions=False,
                    recover_on_open=False,
                    embedding_backend=installed_search_backend(profile.root),
                )
            elif receipt_protection_port is None:
                tasks = open_local_engine(
                    profile,
                    validate_before_write=validate_direct_write,
                    recover_abandoned_sessions=False,
                    embedding_backend=installed_search_backend(profile.root),
                )
            else:
                tasks = open_local_engine(
                    profile,
                    validate_before_write=validate_direct_write,
                    recover_abandoned_sessions=False,
                    receipt_protection_port=receipt_protection_port,
                    receipt_protection_timeout_seconds=receipt_protection_timeout_seconds,
                    embedding_backend=installed_search_backend(profile.root),
                )
            prepared.revalidate()
            _require_current_local_state(prepared, profile)

        def recover_abandoned_sessions() -> object:
            if existing_only:
                return 0
            if tasks is None:
                raise RuntimeError("local engine admission did not complete")
            return tasks.managed_inference.recover_abandoned_sessions()

        with hold_local_runtime_session(
            profile.root,
            profile.root_identity,
            legacy_state_exists=initialized_before,
            recover_abandoned_sessions=recover_abandoned_sessions,
            admit_session=admit,
            admission_profile=profile,
            held_admission=None if transaction is None else transaction._admission,
        ):
            if tasks is None:
                raise RuntimeError("local engine admission did not complete")
            yield LocalBrainSession(
                initialized_before=initialized_before,
                prepared=prepared,
                profile=profile,
                tasks=tasks,
            )
        prepared.revalidate()


def initialize_local_brain(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> LocalInitReceipt:
    """Create or reopen one owner, one Brain, and its direct SQLite state."""
    with open_local_brain(selection, filesystem_type_probe=filesystem_type_probe) as session:
        initialized_before = session.initialized_before
    return LocalInitReceipt(
        status="already_initialized" if initialized_before else "initialized",
        profile="local",
        brain_count=1,
        storage="sqlite",
        daemon_running=False,
        application_encryption=False,
        state_schema_version=PHASE1_STATE_SCHEMA_VERSION,
    )


def _require_current_local_state(
    prepared: PreparedLocalRoot,
    profile: LocalEngineContext,
) -> None:
    if not prepared.private_file_exists("brain.toml") or not prepared.private_file_exists(
        _STATE_DATABASE
    ):
        raise RuntimeError("local bootstrap did not create private state")
    schema = inspect_phase1_state(profile)
    if schema.state != "current" or schema.version != PHASE1_STATE_SCHEMA_VERSION:
        raise RuntimeError("local bootstrap did not create the current SQLite schema")


__all__ = [
    "LocalBrainSession",
    "LocalBrainTransaction",
    "LocalInitReceipt",
    "initialize_local_brain",
    "hold_existing_local_brain_transaction",
    "open_local_brain",
    "run_existing_local_brain_transaction",
]
