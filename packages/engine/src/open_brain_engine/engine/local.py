"""Local engine composition and legacy compatibility re-exports."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from datetime import datetime
from hashlib import sha256

from open_brain_engine.providers.base import ProviderMode
from open_brain_engine.storage.filesystem import assert_root_identity
from open_brain_engine.storage.locks import FileLease
from open_brain_engine.storage.sqlite import SchemaError

from .authority import require_daemon_authority
from .backup import BackupTasks
from .capture import CaptureOperations, CaptureTasks
from .contracts import (
    BackupFault,
    CaptureAction,
    CaptureFault,
    CaptureReceipt,
    CaptureSubmission,
    CaptureSubmissionPath,
    DaemonMutationPath,
    DecisionOutcome,
    DecisionRecord,
    EngineTaskSet,
    EnrichmentProvider,
    EnrichmentRequest,
    EnrichmentUnavailable,
    EventPayload,
    FilePayload,
    InboxItem,
    InjectedFault,
    LocalEngineContext,
    MeasurementPayload,
    PageResult,
    Payload,
    Phase1TaskSet,
    PortabilityFault,
    ProposalDraft,
    ProposalRecord,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    PublicProvenance,
    ReferencePayload,
    RetrievalResult,
    RoutedCapture,
    SpaceRecord,
    TextPayload,
)
from .local_schema import open_local_database_read_only
from .local_store import _LocalStore, live_search_schema_is_available
from .maintenance import inspect_phase1_state
from .markdown_import import MarkdownImportTasks
from .normalization import _done, _utc_now
from .portability import PortabilityTasks
from .reconciliation import ReconciliationTasks, rederive_live_search_projection
from .retrieval import RetrievalOperations, RetrievalTasks, ScopedRetrieval
from .review import ReviewOperations, ReviewTasks
from .spaces import InboxSpaceTasks, SpaceOperations


class ReadViewUnavailableError(RuntimeError):
    """The source-checkout read view cannot open the existing engine state safely."""


class StateSchemaUnavailableError(RuntimeError):
    """A mutating engine cannot safely open this existing state schema."""


class _ReadOnlyStore:
    def __init__(self, profile: LocalEngineContext) -> None:
        self._profile = profile

    def connect(self) -> sqlite3.Connection:
        return open_local_database_read_only(self._profile)


class _ReadOnlyRetrieval(RetrievalOperations):
    def __init__(
        self,
        profile: LocalEngineContext,
        *,
        allowed_space_ids: frozenset[str],
    ) -> None:
        self.profile = profile
        self._store = _ReadOnlyStore(profile)
        self._allowed_space_ids = allowed_space_ids

    def search(
        self,
        query: str,
        *,
        space_id: str | None = None,
        payload_family: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> tuple[RetrievalResult, ...]:
        return self._search(
            query,
            space_id=space_id,
            payload_family=payload_family,
            record_type=record_type,
            limit=limit,
            allowed_space_ids=self._allowed_space_ids,
        )

    def fetch(self, result_id: str) -> RetrievalResult | None:
        return self._fetch(result_id, allowed_space_ids=self._allowed_space_ids)

    def read_page(self, result_id: str) -> PageResult | None:
        return self._read_page(result_id, allowed_space_ids=self._allowed_space_ids)


class BrainEngine(CaptureOperations, SpaceOperations, ReviewOperations, RetrievalOperations):
    """Concrete local engine retained as a compatibility facade over task modules."""

    def __init__(
        self,
        profile: LocalEngineContext,
        *,
        faults: Collection[CaptureFault | PortabilityFault | BackupFault],
        clock: Callable[[], datetime],
        enrichment_provider: EnrichmentProvider | None,
        validate_mutation_authority: Callable[[], None] | None = None,
    ) -> None:
        if profile.provider_mode is ProviderMode.CLOUD:
            raise ValueError("Phase 1 local engine does not enable cloud enrichment")
        if profile.provider_mode is ProviderMode.NONE and enrichment_provider is not None:
            raise ValueError("provider-none mode cannot construct an enrichment provider")
        if enrichment_provider is not None and not callable(
            getattr(enrichment_provider, "enrich", None)
        ):
            raise ValueError("invalid enrichment provider")
        if validate_mutation_authority is not None and not callable(validate_mutation_authority):
            raise ValueError("invalid mutation authority validator")
        assert_root_identity(profile.root, profile.root_identity)
        schema = inspect_phase1_state(profile)
        if schema.state in {"invalid", "newer"}:
            raise StateSchemaUnavailableError(f"local state schema is {schema.state}")
        self.profile = profile
        self._faults = set(faults)
        self._clock = clock
        self._enrichment_provider = enrichment_provider
        lease_identity = "engine-" + sha256(profile.owner_actor_id.encode("utf-8")).hexdigest()[:32]
        self._writer_lease = FileLease(
            profile.root / ".open-brain",
            lease_identity,
            clock=clock,
            validate_acquire=validate_mutation_authority,
            parent_root_identity=profile.root_identity,
        )
        with self._writer_lease.acquire_shared_writer():
            self._store = _LocalStore(profile, clock=self._clock)
        self.capture = CaptureTasks(self)
        self.inbox = InboxSpaceTasks(self)
        self.review = ReviewTasks(self)
        self.retrieval = RetrievalTasks(self)
        self.portability = PortabilityTasks(self)
        self.backup = BackupTasks(self)
        self.reconciliation = ReconciliationTasks(self)
        self.markdown_import = MarkdownImportTasks(self)
        daemon_mutation_path = DaemonMutationPath.reserved(profile.root)
        phase1 = Phase1TaskSet(
            capture=self.capture,
            inbox=self.inbox,
            review=self.review,
            retrieval=self.retrieval,
        )
        self._task_set = EngineTaskSet(
            profile=profile,
            capture=self.capture,
            inbox=self.inbox,
            review=self.review,
            retrieval=self.retrieval,
            portability=self.portability,
            backup=self.backup,
            reconciliation=self.reconciliation,
            markdown_import=self.markdown_import,
            daemon_mutation_path=daemon_mutation_path,
            phase1=phase1,
        )

    @classmethod
    def open(
        cls,
        profile: LocalEngineContext,
        *,
        faults: Collection[CaptureFault | PortabilityFault | BackupFault] | None = None,
        clock: Callable[[], datetime] | None = None,
        enrichment_provider: EnrichmentProvider | None = None,
        validate_mutation_authority: Callable[[], None] | None = None,
    ) -> BrainEngine:
        if not isinstance(profile, LocalEngineContext):
            raise ValueError("invalid local profile")
        engine = cls(
            profile,
            faults=faults or set(),
            clock=clock or _utc_now,
            enrichment_provider=enrichment_provider,
            validate_mutation_authority=validate_mutation_authority,
        )
        with engine._writer_lease.acquire_shared_writer():
            engine._recover()
            for name in profile.starter_spaces:
                key = sha256(name.encode("utf-8")).hexdigest()
                engine._space_operation("create", None, name, f"starter.{key}")
        return engine

    @property
    def tasks(self) -> EngineTaskSet:
        return self._task_set

    def recover(self) -> int:
        with self._writer_lease.acquire_shared_writer():
            return self._recover()

    def _rederive_live_search_projection(self) -> None:
        rederive_live_search_projection(self)

    def _recover(self) -> int:
        recovered = 0
        for table, processor in (
            ("space_operations", self._process_space_operation),
            ("captures", self._process_capture),
            ("route_operations", self._process_route),
            ("proposal_sets", self._process_proposal_set),
            ("decisions", self._process_decision),
        ):
            connection = self._store.connect()
            try:
                rows = tuple(
                    connection.execute(f"SELECT * FROM {table} WHERE stage < ?", (_done(table),))
                )
            finally:
                connection.close()
            for row in rows:
                processor(row)
                recovered += 1
        return recovered

    def _fault(self, point: CaptureFault | PortabilityFault | BackupFault) -> None:
        if point in self._faults:
            self._faults.remove(point)
            raise InjectedFault(point)

    def _assert_root(self) -> None:
        assert_root_identity(self.profile.root, self.profile.root_identity)


def open_local_engine(
    profile: LocalEngineContext,
    *,
    faults: Collection[CaptureFault | PortabilityFault | BackupFault] | None = None,
    clock: Callable[[], datetime] | None = None,
    enrichment_provider: EnrichmentProvider | None = None,
    validate_before_write: Callable[[], None] | None = None,
) -> EngineTaskSet:
    """Open one local root and expose only its named task capabilities."""
    return BrainEngine.open(
        profile,
        faults=faults,
        clock=clock,
        enrichment_provider=enrichment_provider,
        validate_mutation_authority=validate_before_write,
    ).tasks


def open_authoritative_local_engine(
    profile: LocalEngineContext,
    authority: object | None,
    *,
    faults: Collection[CaptureFault | PortabilityFault | BackupFault] | None = None,
    clock: Callable[[], datetime] | None = None,
    enrichment_provider: EnrichmentProvider | None = None,
) -> EngineTaskSet:
    """Open one local root for mutation only while daemon lifetime authority remains active."""
    require_daemon_authority(profile, authority)
    return BrainEngine.open(
        profile,
        faults=faults,
        clock=clock,
        enrichment_provider=enrichment_provider,
        validate_mutation_authority=lambda: require_daemon_authority(profile, authority),
    ).tasks


def recover_authoritative_local_engine(
    profile: LocalEngineContext,
    authority: object | None,
    *,
    clock: Callable[[], datetime] | None = None,
    enrichment_provider: EnrichmentProvider | None = None,
) -> int:
    """Replay durable engine transitions only while daemon authority remains active."""
    require_daemon_authority(profile, authority)
    engine = BrainEngine(
        profile,
        faults=set(),
        clock=clock or _utc_now,
        enrichment_provider=enrichment_provider,
        validate_mutation_authority=lambda: require_daemon_authority(profile, authority),
    )
    return engine.recover()


def open_local_read_view(
    profile: LocalEngineContext,
    *,
    allowed_space_ids: frozenset[str] = frozenset(),
) -> _ReadOnlyRetrieval:
    """Open the existing retrieval state read-only without recovery or lease acquisition."""
    if not isinstance(profile, LocalEngineContext) or not isinstance(allowed_space_ids, frozenset):
        raise ValueError("invalid local profile")
    schema = inspect_phase1_state(profile)
    if schema.state == "absent":
        raise ReadViewUnavailableError("read-only state schema is absent")
    if schema.state == "newer":
        raise ReadViewUnavailableError("read-only state schema is newer than this application")
    if schema.state == "recovery_required":
        raise ReadViewUnavailableError("read-only state requires writable SQLite recovery")
    if schema.state != "current":
        raise ReadViewUnavailableError("read-only state schema is invalid")
    try:
        connection = open_local_database_read_only(profile)
        try:
            search_available = live_search_schema_is_available(connection)
        finally:
            connection.close()
    except SchemaError, sqlite3.Error:
        search_available = False
    if not search_available:
        raise ReadViewUnavailableError("read-only live search schema is unavailable")
    return _ReadOnlyRetrieval(profile, allowed_space_ids=allowed_space_ids)


__all__ = [
    "BrainEngine",
    "CaptureAction",
    "CaptureFault",
    "CaptureReceipt",
    "CaptureSubmission",
    "CaptureSubmissionPath",
    "CaptureTasks",
    "DecisionOutcome",
    "DecisionRecord",
    "EnrichmentProvider",
    "EnrichmentRequest",
    "EnrichmentUnavailable",
    "EngineTaskSet",
    "EventPayload",
    "FilePayload",
    "InboxItem",
    "InboxSpaceTasks",
    "InjectedFault",
    "LocalEngineContext",
    "MeasurementPayload",
    "Payload",
    "Phase1TaskSet",
    "ProposalDraft",
    "ProposalRecord",
    "PublicJobCaptureContext",
    "PublicJobCaptureSink",
    "PublicProvenance",
    "ReadViewUnavailableError",
    "ReferencePayload",
    "recover_authoritative_local_engine",
    "RetrievalResult",
    "RetrievalTasks",
    "ReviewTasks",
    "RoutedCapture",
    "ScopedRetrieval",
    "SpaceRecord",
    "StateSchemaUnavailableError",
    "TextPayload",
    "open_local_engine",
    "open_authoritative_local_engine",
    "open_local_read_view",
]
