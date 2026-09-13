"""App-owned construction for CLI, HTTP, MCP, and scheduled capabilities."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.core.ports import Clock
from open_brain_engine.engine import (
    EngineTaskSet,
    LockScope,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    open_local_engine,
)
from open_brain_engine.events.store import SqliteEventStore
from open_brain_engine.providers.base import ProviderService
from open_brain_engine.storage.locks import FileLease, LockBusyError, inspect_file_leases
from open_brain_engine.storage.sqlite import SCHEMA_VERSION, inspect_event_schema
from open_brain_engine.storage.writer_record import WriterRecordError, read_canonical_writer_record

from open_brain_legacy._compat.open_brain.capture.http import BodyReader, ShareHttpHandler
from open_brain_legacy._compat.open_brain.cli._common import (
    CommandDispatchResult,
    ExitCode,
    redacted_error,
    unavailable_envelope,
)
from open_brain_legacy._compat.open_brain.cli.phase1_registry import Phase1CommandAdapterRegistry
from open_brain_legacy._compat.open_brain.config import AppConfig, ConfigError, SecretRefKind
from open_brain_legacy._compat.open_brain.integrations.config import IntegrationConfig
from open_brain_legacy._compat.open_brain.integrations.mcp import EngineMcpAdapter
from open_brain_legacy._compat.open_brain.integrations.phase1_ui import Phase1UiHandler
from open_brain_legacy._compat.open_brain.integrations.ports import (
    Capability,
    ProviderSyncRequest,
    SyncStatus,
)
from open_brain_legacy._compat.open_brain.profile import compile_single_user_local
from open_brain_legacy._compat.open_brain.services.connectors import (
    ConnectorHost,
    ConnectorOutcome,
    ConnectorProfile,
    RunContextFactory,
)
from open_brain_legacy._compat.open_brain.services.http_server import (
    HttpRouteMode,
    HttpServerFactory,
)
from open_brain_legacy._compat.open_brain.services.runtime import (
    ServiceConfigurationError,
    compose_http_from_config,
    load_private_http_bind_config,
    read_private_service_secret,
)
from open_brain_legacy.capture.distillation_worker import DistillationProcessStatus
from open_brain_legacy.capture.egress import OutboundFetcher
from open_brain_legacy.capture.queue import read_pending_queue_snapshot
from open_brain_legacy.capture.service import ProcessStatus
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.config import ConfigCliResult, show_config
from open_brain_legacy.cli.doctor import DoctorCommandAdapter
from open_brain_legacy.cli.production_adapters import build_production_command_adapters
from open_brain_legacy.cli.review import ReviewCommandAdapter
from open_brain_legacy.integrations.life_os import LifePlanRequest, LifeResetRequest
from open_brain_legacy.integrations.life_os_runtime import LifeOSPlanningRuntime
from open_brain_legacy.integrations.messaging_runtime import (
    PersistentMessagingCursorStore,
    PersistentMessagingRuntime,
    SqliteMessageInbox,
    SqliteReviewProposalWriter,
)
from open_brain_legacy.integrations.retrieval import MetadataOnlyRetrievalFeedback
from open_brain_legacy.ledger.embed import embed_text
from open_brain_legacy.ledger.models import LedgerRoute, LedgerTaxonomy
from open_brain_legacy.ledger.store import SqliteLedgerStore, inspect_published_references
from open_brain_legacy.operations.backup import BackupError
from open_brain_legacy.operations.backup_writer import (
    BackupEffectCapability,
    BackupWriterApplication,
    CanonicalWriterAuthorityError,
    FilesystemBackupSource,
    FilesystemBackupStore,
    inspect_backup_evidence,
)
from open_brain_legacy.operations.curation_runtime import (
    CurationEffectCapability,
    CurationRuntimeApplication,
)
from open_brain_legacy.operations.curation_runtime import (
    SharedWriterAuthority as CurationSharedWriterAuthority,
)
from open_brain_legacy.operations.doctor import DoctorProbe, ProbeName
from open_brain_legacy.operations.git_sync_runtime import (
    GitRepositoryKind,
    GitSyncEffectCapability,
    GitSyncPlanner,
    GitSyncRuntimeApplication,
    PlannedGitSyncExecutor,
)
from open_brain_legacy.operations.git_sync_runtime import (
    SharedWriterAuthority as GitSharedWriterAuthority,
)
from open_brain_legacy.operations.index import IndexError as IndexOperationError
from open_brain_legacy.operations.index import IndexRoots, check_index
from open_brain_legacy.operations.index_writer import IndexEffectCapability, IndexWriterApplication
from open_brain_legacy.operations.local_effect import FilesystemPreparedEffectCapability
from open_brain_legacy.operations.models import JobSpec
from open_brain_legacy.operations.now import NowItem, NowProjectionInput, NowRoots, check_now
from open_brain_legacy.operations.now_runtime import (
    NowEffectCapability,
    NowRuntimeApplication,
)
from open_brain_legacy.operations.now_runtime import (
    SharedWriterAuthority as NowSharedWriterAuthority,
)
from open_brain_legacy.operations.probes import (
    BackupEvidenceSnapshot,
    BackupProfileEvidence,
    SchemaSnapshot,
    StaleReferenceSnapshot,
    backup_evidence_probe,
    configuration_probe,
    lock_state_probe,
    optional_provider_probe,
    queue_age_probe,
    schema_probe,
    stale_reference_probe,
    unavailable_probe,
    writer_ownership_probe,
)
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.scheduled_results import ScheduledDispatchResult
from open_brain_legacy.operations.writer_jobs import (
    WriterJobError,
    WriterJobSpec,
    run_writer_job,
)
from open_brain_legacy.production.capture import compose_production_capture_runtime
from open_brain_legacy.production.curation import (
    ProductionCurationError,
    build_production_curation_batch,
)
from open_brain_legacy.production.git_sync import (
    GitInventoryError,
    SubprocessGitCommandRunner,
    load_private_git_inventory,
)
from open_brain_legacy.production.imessage import (
    ImessageConfigError,
    ImessageHistoryClient,
    compose_production_imessage_ingress,
)
from open_brain_legacy.production.local_jobs import (
    CloseDayPreparationApplication,
    FilesystemSignalCutoffStore,
    GitSignalScanner,
    HookSyncPlanApplication,
    SignalScanApplication,
    WorkWikiLintApplication,
    build_hook_plans,
    scan_work_wiki,
)
from open_brain_legacy.production.media import compose_production_capture_media_adapter
from open_brain_legacy.production.optional_automation import (
    OptionalAutomationConfigError,
    approved_life_os_candidates,
    load_private_life_os_config,
    load_private_messages_config,
)
from open_brain_legacy.production.personal_capture import PersonalCaptureStatus
from open_brain_legacy.production.providers import (
    ProductionProviderConfigError,
    compose_production_provider,
    load_private_provider_config,
)
from open_brain_legacy.production.retention import (
    ProductionRetentionError,
    compose_production_retention_service,
)
from open_brain_legacy.production.sqlite_backup import (
    SQLiteBackupProbeError,
    probe_local_sqlite_backups,
)
from open_brain_legacy.production.transport import DnsPinnedHttpTransport, SystemResolver
from open_brain_legacy.review.maintenance import predecessor_curation_taxonomy
from open_brain_legacy.review.store import (
    REVIEW_SCHEMA_VERSION,
    SqliteReviewStore,
    inspect_review_schema,
)
from open_brain_legacy.services.capabilities import (
    ProductionApplication,
    compose_production_application,
)

_LOCK_STALE_AFTER_SECONDS = {
    LockScope.SHARED_WRITER: 300,
    LockScope.INDEX: 7_200,
    LockScope.BACKUP_PROFILE: 86_400,
    LockScope.INGRESS: 300,
}

__all__ = ["ProductionApplication", "compose_production_application"]


@dataclass(frozen=True, slots=True)
class SingleUserLocalApplication:
    """One app-owned local root exposing the engine's public task capabilities."""

    tasks: EngineTaskSet
    feedback: MetadataOnlyRetrievalFeedback
    connector_profile: ConnectorProfile = field(default_factory=ConnectorProfile)
    connector_host: ConnectorHost = field(default_factory=ConnectorHost)

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        connector_profile: ConnectorProfile | None = None,
        connector_host: ConnectorHost | None = None,
    ) -> SingleUserLocalApplication:
        selected_profile = ConnectorProfile() if connector_profile is None else connector_profile
        selected_host = ConnectorHost() if connector_host is None else connector_host
        if not isinstance(selected_profile, ConnectorProfile) or not isinstance(
            selected_host, ConnectorHost
        ):
            raise ValueError("invalid connector application configuration")
        return cls(
            tasks=open_local_engine(compile_single_user_local(root)),
            feedback=MetadataOnlyRetrievalFeedback(),
            connector_profile=selected_profile,
            connector_host=selected_host,
        )

    def discover_connectors(self) -> tuple[str, ...]:
        """Enumerate allow-listed connector metadata without importing connector modules."""
        return tuple(item.name for item in self.connector_host.discover(self.connector_profile))

    def cli_adapters(self) -> Phase1CommandAdapterRegistry:
        from open_brain_legacy._compat.open_brain.cli.phase1 import build_phase1_command_adapters

        return build_phase1_command_adapters(self.tasks.phase1)

    def ui_handler(self, expected_bearer_token: str) -> Phase1UiHandler:
        return Phase1UiHandler(expected_bearer_token=expected_bearer_token, tasks=self.tasks.phase1)

    def share_handler(
        self,
        *,
        expected_bearer_token: str,
        body_reader: BodyReader,
        clock: Callable[[], datetime],
    ) -> ShareHttpHandler:
        return ShareHttpHandler(
            expected_bearer_token=expected_bearer_token,
            capture=self.tasks.capture,
            clock=clock,
            body_reader=body_reader,
        )

    def mcp_adapter(self, *, allowed_space_ids: frozenset[str] = frozenset()) -> EngineMcpAdapter:
        return EngineMcpAdapter(
            retrieval=self.tasks.retrieval.scoped(
                allowed_space_ids=allowed_space_ids
            ),
            feedback=self.feedback,
        )

    def public_job_sink(self, job_id: str) -> PublicJobCaptureSink:
        return self.tasks.capture.public_job_sink(self.public_job_context(job_id))

    def public_job_context(self, job_id: str) -> PublicJobCaptureContext:
        """Return the validated non-owner capture actor used by one public job."""
        identities = {
            "JOB-005": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0005",
            "JOB-027": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0027",
            "JOB-028": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0028",
            "JOB-029": "c5b2a0d0-26a5-4a52-a4ee-8a2be15d0029",
        }
        identity = identities.get(job_id)
        if identity is None:
            raise ValueError("unsupported public capture job")
        actor_id = "actor_" + identity
        return PublicJobCaptureContext.create(
            profile=self.tasks.profile,
            actor_id=actor_id,
            role_claim={
                "actor_id": actor_id,
                "capabilities": ["capture.accept"],
                "role_claim_id": "role_claim_" + identity,
                "role_id": "role_" + identity,
                "tenant_id": self.tasks.profile.tenant_id,
            },
        )


@dataclass(frozen=True, slots=True)
class ConfigCommandAdapter:
    """Route the plain ``config`` argv to the real, injected application config."""

    config: AppConfig

    def dispatch(self, argv: tuple[str, ...]) -> CommandDispatchResult:
        if any(argument not in {"--json", "--dry-run"} for argument in argv):
            return ConfigCliResult(
                exit_code=ExitCode.USAGE,
                envelope={
                    "command": "config",
                    "error": redacted_error("invalid_config_request"),
                    "status": "invalid",
                },
            )
        return show_config(config=self.config)


@dataclass(frozen=True, slots=True)
class UnavailablePhase1CommandAdapter:
    """Keep Phase 1-only families closed under the legacy composition path."""

    command: str

    def dispatch(self, _argv: tuple[str, ...]) -> ConfigCliResult:
        return ConfigCliResult(
            exit_code=ExitCode.FAILURE,
            envelope=unavailable_envelope(self.command),
        )


@dataclass(frozen=True, slots=True)
class ConfiguredReviewCommandAdapter:
    """Open the root-confined review store only for the selected command."""

    config: AppConfig
    clock: Clock

    def dispatch(self, argv: tuple[str, ...]) -> CommandDispatchResult:
        with SqliteReviewStore(
            root=self.config.state_root,
            database_name="review/review.sqlite3",
            clock=self.clock,
        ) as store:
            return ReviewCommandAdapter(
                maintenance=store,
                taxonomy=predecessor_curation_taxonomy(),
                clock=self.clock,
            ).dispatch(argv)


@dataclass(frozen=True, slots=True)
class _CanonicalBackupLease:
    authority: FileLease
    backup: FileLease
    validate_authority: Callable[[], None]

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        if scope is not LockScope.BACKUP_PROFILE:
            raise CanonicalWriterAuthorityError("invalid canonical backup lease")
        with (
            self.authority.acquire(LockScope.SHARED_WRITER),
            self.backup.acquire(scope),
        ):
            self.validate_authority()
            yield
            self.validate_authority()


@dataclass(frozen=True, slots=True)
class _CanonicalScopedLease:
    authority: FileLease
    scoped: FileLease
    expected_scope: LockScope
    validate_authority: Callable[[], None]

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        if scope is not self.expected_scope:
            raise CanonicalWriterAuthorityError("invalid canonical scoped lease")
        with (
            self.authority.acquire(LockScope.SHARED_WRITER),
            self.scoped.acquire(scope),
        ):
            self.validate_authority()
            yield
            self.validate_authority()


@dataclass(frozen=True, slots=True)
class _CanonicalWriterLease:
    lease: FileLease
    validate_authority: Callable[[], None]

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        if scope is not LockScope.SHARED_WRITER:
            raise CanonicalWriterAuthorityError("invalid canonical writer lease")
        with self.lease.acquire(scope):
            self.validate_authority()
            yield
            self.validate_authority()


@dataclass(slots=True)
class _DeterministicLocalIndexEmbedder:
    model_id: str = "open-brain-local-token-v1-d64"
    requires_cloud_authority: bool = False
    requires_external_egress: bool = False

    def embed(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str):
            raise IndexOperationError("invalid local embedding input")
        if text.strip():
            try:
                return embed_text(text, dimensions=64)
            except ValueError:
                digest = sha256(text.encode("utf-8")).digest()
                return tuple(
                    0.125 if digest[index // 8] & (1 << (index % 8)) else -0.125
                    for index in range(64)
                )
        return (1.0, *([0.0] * 63))


@dataclass(frozen=True, slots=True)
class ConfigurationFailedScheduledAdapters:
    """Preserve the documented scheduler preflight exit when config cannot load."""

    def dispatch_capture(self, application: object) -> ScheduledDispatchResult:
        job_id = getattr(getattr(application, "job", None), "id", "JOB-000")
        return ScheduledDispatchResult.configuration(job_id)

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult:
        return ScheduledDispatchResult.configuration(application.id)

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        return ScheduledDispatchResult.configuration(application.job_id)


@dataclass(frozen=True, slots=True)
class ConfiguredScheduledAdapters:
    """Production-safe scheduled dispatch with only concretely backed effects enabled."""

    config: AppConfig
    clock: Clock
    environment: Mapping[str, object] = field(default_factory=dict)
    connector_context_factories: Mapping[str, RunContextFactory] = field(default_factory=dict)
    imessage_history_client: ImessageHistoryClient | None = None
    imessage_service_mode: bool = False
    http_server_factory: HttpServerFactory | None = None
    http_service_mode: bool = False
    provider_service: ProviderService | None = None
    public_application: SingleUserLocalApplication | None = None

    def dispatch_capture(self, application: object) -> ScheduledDispatchResult:
        job_id = getattr(getattr(application, "job", None), "id", "JOB-000")
        try:
            if job_id not in {"JOB-005", "JOB-027", "JOB-028", "JOB-029"}:
                return ScheduledDispatchResult.configuration(job_id)
            if self.public_application is None:
                return ScheduledDispatchResult.configuration(job_id)
            sink = self.public_application.public_job_sink(job_id)
            if job_id == "JOB-005":
                return self._dispatch_imessage(job_id=job_id, sink=sink)
            if job_id in {"JOB-027", "JOB-028"}:
                return self._dispatch_http_service(job_id, sink=sink)
            if job_id == "JOB-029":
                return self._dispatch_youtube_poll(job_id=job_id)
        except Exception:
            return ScheduledDispatchResult.configuration(job_id)
        return ScheduledDispatchResult.completed(job_id)

    def _dispatch_imessage(
        self,
        *,
        job_id: str,
        sink: PublicJobCaptureSink,
    ) -> ScheduledDispatchResult:
        reference = self.environment.get("OPEN_BRAIN_IMESSAGE_CONFIG")
        if not isinstance(reference, str) or not reference:
            return ScheduledDispatchResult.configuration(job_id)
        try:
            runtime = compose_production_imessage_ingress(
                config_path=Path(reference),
                state_root=self.config.state_root,
                sink=sink,
                history_client=self.imessage_history_client,
            )
            if self.imessage_service_mode:
                runtime.run_forever()
            else:
                runtime.run_once()
        except ImessageConfigError:
            return ScheduledDispatchResult.configuration(job_id)
        except Exception:
            return ScheduledDispatchResult.failed(job_id)
        return ScheduledDispatchResult.completed(job_id)

    def _dispatch_http_service(
        self,
        job_id: str,
        *,
        sink: PublicJobCaptureSink | None = None,
    ) -> ScheduledDispatchResult:
        settings = {
            "JOB-026": (
                "OPEN_BRAIN_UI_CONFIG",
                "ui_service_token",
                HttpRouteMode.UI_ONLY,
            ),
            "JOB-027": (
                "OPEN_BRAIN_INGRESS_CONFIG",
                "ingress_service_token",
                HttpRouteMode.SHARE_ONLY,
            ),
            "JOB-028": (
                "OPEN_BRAIN_INGRESS_CONFIG",
                "ingress_service_token",
                HttpRouteMode.SHARE_ONLY,
            ),
        }
        selected = settings.get(job_id)
        if selected is None:
            return ScheduledDispatchResult.configuration(job_id)
        config_name, secret_name, route_mode = selected
        reference = self.environment.get(config_name)
        if not isinstance(reference, str) or not reference:
            return ScheduledDispatchResult.configuration(job_id)
        environment = {
            key: value
            for key, value in self.environment.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        try:
            if self.public_application is None:
                return ScheduledDispatchResult.configuration(job_id)
            lifecycle = compose_http_from_config(
                config=self.config,
                application=self.public_application,
                environment=environment,
                file_reader=read_private_service_secret,
                bind=load_private_http_bind_config(Path(reference)),
                secret_name=secret_name,
                route_mode=route_mode,
                capture=sink,
            )
            if not self.http_service_mode:
                return ScheduledDispatchResult.completed(job_id)
            if route_mode is HttpRouteMode.SHARE_ONLY:
                if self.config.host_identity is None:
                    return ScheduledDispatchResult.configuration(job_id)
                with FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ).acquire(LockScope.INGRESS):
                    lifecycle.start(
                        server_factory=self.http_server_factory
                    ).serve_forever()
            else:
                lifecycle.start(
                    server_factory=self.http_server_factory
                ).serve_forever()
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(job_id)
        except (ConfigError, ServiceConfigurationError, ValueError):
            return ScheduledDispatchResult.configuration(job_id)
        except Exception:
            return ScheduledDispatchResult.failed(job_id)
        return ScheduledDispatchResult.completed(job_id)

    def _dispatch_youtube_poll(
        self,
        *,
        job_id: str,
    ) -> ScheduledDispatchResult:
        reference = self.environment.get("OPEN_BRAIN_YOUTUBE_CONFIG")
        if (
            not isinstance(reference, str)
            or not reference
            or not Path(reference).is_absolute()
        ):
            return ScheduledDispatchResult.configuration(job_id)
        if self.public_application is None:
            return ScheduledDispatchResult.configuration(job_id)
        context_factory = self.connector_context_factories.get("youtube")
        if context_factory is None:
            return ScheduledDispatchResult.configuration(job_id)
        try:
            receipt = self.public_application.connector_host.run(
                "youtube",
                profile=self.public_application.connector_profile,
                context_factory=context_factory,
            )
        except Exception:
            return ScheduledDispatchResult.failed(job_id)
        if receipt.outcome is ConnectorOutcome.DEFERRED:
            return ScheduledDispatchResult.completed(job_id)
        if receipt.outcome is not ConnectorOutcome.COMPLETED or receipt.stubbed_count:
            return ScheduledDispatchResult.failed(job_id)
        return ScheduledDispatchResult.completed(job_id)

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult:
        if application.id in {"JOB-001", "JOB-013"}:
            role = "probe" if application.id == "JOB-001" else "writer"
            result = DoctorCommandAdapter(
                probes=_doctor_probes(self.config, clock=self.clock.now)
            ).dispatch((f"--role={role}",))
            return (
                ScheduledDispatchResult.completed(application.id)
                if result.exit_code == ExitCode.SUCCESS
                else ScheduledDispatchResult.failed(application.id)
            )
        if application.id == "JOB-002":
            try:
                index_check = check_index(
                    target=application.deployment_target,
                    roots=IndexRoots(
                        pages_root=self.config.work_root,
                        captures_root=self.config.saved_content_root,
                        output_root=self.config.state_root / "index",
                    ),
                )
            except Exception:
                return ScheduledDispatchResult.configuration(application.id)
            return (
                ScheduledDispatchResult.completed(application.id)
                if index_check.available and index_check.generation_id is not None
                else ScheduledDispatchResult.failed(application.id)
            )
        if application.id in {"JOB-003", "JOB-030"}:
            try:
                now_check = check_now(
                    target=application.deployment_target,
                    roots=NowRoots(
                        canonical_output_root=self.config.work_root,
                        edge_output_root=self.config.state_root / "now" / "edge",
                        ingress_output_root=self.config.state_root / "now" / "ingress",
                    ),
                )
            except Exception:
                return ScheduledDispatchResult.configuration(application.id)
            return (
                ScheduledDispatchResult.completed(application.id)
                if now_check.available and now_check.marker_valid
                else ScheduledDispatchResult.failed(application.id)
            )
        if application.id == "JOB-004":
            try:
                probe_local_sqlite_backups(
                    state_root=self.config.state_root,
                    clock=self.clock,
                )
            except SQLiteBackupProbeError:
                return ScheduledDispatchResult.failed(application.id)
            return ScheduledDispatchResult.completed(application.id)
        if application.id in {"JOB-017", "JOB-018", "JOB-019"}:
            return self._dispatch_life_os(application)
        if application.id in {"JOB-020", "JOB-021"}:
            return self._dispatch_messaging(application)
        if application.id == "JOB-024":
            reference = self.environment.get("OPEN_BRAIN_RETENTION_CONFIG")
            if not isinstance(reference, str) or not reference:
                return ScheduledDispatchResult.configuration(application.id)
            context = self._optional_writer_context(application)
            if context is None:
                return ScheduledDispatchResult.configuration(application.id)
            _now, lease = context
            try:
                service = compose_production_retention_service(
                    app_config=self.config,
                    config_path=Path(reference),
                    clock=self.clock,
                )
            except ProductionRetentionError:
                return ScheduledDispatchResult.configuration(application.id)
            try:
                with lease.acquire(LockScope.SHARED_WRITER):
                    service.retain(dry_run=True)
            except LockBusyError:
                return ScheduledDispatchResult.lock_held(application.id)
            except (CanonicalWriterAuthorityError, WriterRecordError):
                return ScheduledDispatchResult.configuration(application.id)
            except ProductionRetentionError:
                return ScheduledDispatchResult.failed(application.id)
            return ScheduledDispatchResult.completed(application.id)
        if application.id == "JOB-026":
            return self._dispatch_http_service(application.id)
        return ScheduledDispatchResult.failed(application.id)

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        if application.job_id == "JOB-016":
            return self._dispatch_index(application)
        if application.job_id == "JOB-022":
            return self._dispatch_now(application)
        if application.job_id == "JOB-012":
            return self._dispatch_curation(application)
        if application.job_id == "JOB-015":
            return self._dispatch_git_sync(application)
        if application.job_id == "JOB-010":
            return self._dispatch_nightly_capture(application)
        if application.job_id not in {"JOB-011", "JOB-014", "JOB-023", "JOB-025"}:
            return self._dispatch_local_writer(application)
        if self.config.host_identity is None:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            now = self.clock.now()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return ScheduledDispatchResult.configuration(application.job_id)
            profile = {
                "JOB-011": "capture",
                "JOB-014": "full",
                "JOB-023": "personal",
                "JOB-025": "runtime-state",
            }[application.job_id]
            source = FilesystemBackupSource(
                work_root=self.config.work_root,
                personal_root=self.config.personal_root,
                capture_root=self.config.capture_root,
                saved_content_root=self.config.saved_content_root,
                state_root=self.config.state_root,
            )
            store = FilesystemBackupStore(root=self.config.backup_root)
            capability = BackupEffectCapability(
                root=self.config.backup_root,
                source=source,
                store=store,
                writer_record=writer_record,
                writer_record_reader=lambda: read_canonical_writer_record(
                    self.config.state_root
                ),
            )
            authority_lease = FileLease(
                self.config.state_root,
                self.config.host_identity,
                clock=self.clock.now,
            )
            backup_lease = FileLease(
                self.config.state_root,
                self.config.host_identity,
                backup_profile=profile,
                clock=self.clock.now,
            )
            lease = _CanonicalBackupLease(
                authority=authority_lease,
                backup=backup_lease,
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
            replay_key = f"{application.job_id.lower()}-{now.date().isoformat()}"
        except (BackupError, CanonicalWriterAuthorityError, ConfigError, WriterRecordError):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            with SqliteReplayJournal(
                root=self.config.state_root,
                clock=self.clock,
            ) as journal:
                run_writer_job(
                    job_id=application.job_id,
                    root=self.config.backup_root,
                    replay_key=replay_key,
                    journal=journal,
                    application=BackupWriterApplication(
                        job_id=application.job_id,
                        created_at=now,
                    ),
                    effect_capability=capability,
                    lease=lease,
                )
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (CanonicalWriterAuthorityError, ConfigError, WriterRecordError):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_local_writer(
        self,
        application: WriterJobSpec,
    ) -> ScheduledDispatchResult:
        if application.job_id not in {
            "JOB-006",
            "JOB-007",
            "JOB-008",
            "JOB-009",
        }:
            return ScheduledDispatchResult.configuration(application.job_id)
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return ScheduledDispatchResult.configuration(application.job_id)
        lease: FileLease | _CanonicalWriterLease | None = None
        if application.lock_scope is LockScope.INGRESS:
            if self.config.host_identity is None:
                return ScheduledDispatchResult.configuration(application.job_id)
            lease = FileLease(
                self.config.state_root,
                self.config.host_identity,
                clock=self.clock.now,
            )
        elif application.lock_scope is LockScope.SHARED_WRITER:
            if self.config.host_identity is None:
                return ScheduledDispatchResult.configuration(application.job_id)
            try:
                writer_record = read_canonical_writer_record(self.config.state_root)
            except WriterRecordError:
                return ScheduledDispatchResult.configuration(application.job_id)
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            lease = _CanonicalWriterLease(
                lease=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
        cutoff_store: FilesystemSignalCutoffStore | None = None
        local_application: (
            CloseDayPreparationApplication
            | SignalScanApplication
            | WorkWikiLintApplication
            | HookSyncPlanApplication
        )
        try:
            if application.job_id == "JOB-006":
                with SqliteReviewStore(
                    root=self.config.state_root,
                    database_name="review/review.sqlite3",
                    clock=self.clock,
                ) as reviews:
                    local_application = CloseDayPreparationApplication(
                        reviews=reviews.active_reviews()
                    )
            elif application.job_id == "JOB-007":
                inventory = load_private_git_inventory(
                    _named_private_file(self.config, "git_inventory")
                )
                runner = SubprocessGitCommandRunner(
                    home_root=inventory.home_root,
                    allowed_roots=(
                        self.config.work_root,
                        self.config.personal_root,
                        inventory.dev_root,
                    ),
                )
                planner = GitSyncPlanner(
                    work_root=self.config.work_root,
                    personal_root=self.config.personal_root,
                    dev_root=inventory.dev_root,
                    repositories=inventory.repositories,
                    runner=runner,
                )
                repository_ids = tuple(
                    item.repo_id
                    for item in inventory.repositories
                    if item.kind is not GitRepositoryKind.PERSONAL
                )
                cutoff_store = FilesystemSignalCutoffStore(
                    root=self.config.state_root
                )
                since = cutoff_store.load(default=now - timedelta(days=1))
                signals = (
                    ()
                    if not repository_ids or since >= now
                    else GitSignalScanner(
                        repository_ids=repository_ids,
                        planner=planner,
                        runner=runner,
                    ).scan(since=since, until=now)
                )
                local_application = SignalScanApplication(signals=signals)
            elif application.job_id == "JOB-008":
                local_application = WorkWikiLintApplication(
                    snapshot=scan_work_wiki(
                        root=self.config.work_root,
                        as_of=now,
                    )
                )
            else:
                inventory = load_private_git_inventory(
                    _named_private_file(self.config, "git_inventory")
                )
                local_application = HookSyncPlanApplication(
                    plans=build_hook_plans(
                        repository_ids=tuple(
                            item.repo_id
                            for item in inventory.repositories
                            if item.kind is not GitRepositoryKind.PERSONAL
                        )
                    ),
                    inventory_digest_sha256=inventory.digest_sha256,
                )
            with SqliteReplayJournal(
                root=self.config.state_root,
                clock=self.clock,
            ) as journal:
                run_writer_job(
                    job_id=application.job_id,
                    root=self.config.state_root,
                    replay_key=_scheduled_replay_key(application.job_id, now),
                    journal=journal,
                    application=local_application,
                    effect_capability=FilesystemPreparedEffectCapability(
                        root=self.config.state_root,
                        spec=application,
                    ),
                    lease=lease,
                    cutoff=now if application.job_id == "JOB-007" else None,
                )
            if cutoff_store is not None:
                cutoff_store.save(now)
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            GitInventoryError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_nightly_capture(
        self,
        application: WriterJobSpec,
    ) -> ScheduledDispatchResult:
        reference = self.environment.get("OPEN_BRAIN_PROVIDER_CONFIG")
        if (
            self.config.host_identity is None
            or not isinstance(reference, str)
            or not reference
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
                or application.lock_scope is not LockScope.SHARED_WRITER
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            lease = _CanonicalWriterLease(
                lease=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
            environment = {
                key: value
                for key, value in self.environment.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            provider = self.provider_service
            if provider is None:
                provider = compose_production_provider(
                    app_config=self.config,
                    config_path=Path(reference),
                    environment=environment,
                    file_reader=read_private_service_secret,
                )
            else:
                load_private_provider_config(Path(reference))
            fetcher = (
                OutboundFetcher(
                    resolver=SystemResolver(enabled=True),
                    transport=DnsPinnedHttpTransport(enabled=True),
                )
                if self.config.egress_enabled
                else None
            )
            media_adapter = (
                compose_production_capture_media_adapter(config=self.config)
                if self.config.egress_enabled
                else None
            )
            with (
                lease.acquire(LockScope.SHARED_WRITER),
                compose_production_capture_runtime(
                    config=self.config,
                    provider=provider,
                    clock=self.clock,
                    fetcher=fetcher,
                    media_adapter=media_adapter,
                ) as runtime,
            ):
                result = runtime.run(max_items=1_000)
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            ProductionProviderConfigError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        if (
            not result.queue_empty
            or any(status is not ProcessStatus.ACKNOWLEDGED for status in result.capture_statuses)
            or any(
                status is not DistillationProcessStatus.COMPLETED
                for status in result.distillation_statuses
            )
            or any(
                status
                not in {PersonalCaptureStatus.COMPLETED, PersonalCaptureStatus.HELD}
                for status in result.personal_statuses
            )
        ):
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_curation(
        self,
        application: WriterJobSpec,
    ) -> ScheduledDispatchResult:
        if self.config.host_identity is None:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            now = self.clock.now()
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
                or not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            lease = _CanonicalWriterLease(
                lease=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
            replay_key = _scheduled_replay_key(application.job_id, now)
            with (
                SqliteEventStore(
                    root=self.config.state_root / "events",
                    database_name="events.sqlite3",
                    clock=self.clock,
                ) as events,
                SqliteReviewStore(
                    root=self.config.state_root,
                    database_name="review/review.sqlite3",
                    clock=self.clock,
                ) as reviews,
                SqliteLedgerStore(root=self.config.state_root / "ledger") as ledger,
                SqliteReplayJournal(
                    root=self.config.state_root,
                    clock=self.clock,
                ) as journal,
            ):
                composition = build_production_curation_batch(
                    config=self.config,
                    taxonomy=_ledger_taxonomy(self.config),
                    now=now,
                    reviews=reviews,
                    events=events,
                    ledger=ledger,
                )
                if (
                    not composition.output_ids
                    and journal.completed(application.job_id, replay_key) is not None
                ):
                    return ScheduledDispatchResult.completed(application.job_id)
                capability = CurationEffectCapability(
                    root=self.config.state_root,
                    batch=composition.batch,
                    authority=CurationSharedWriterAuthority(
                        LockScope.SHARED_WRITER
                    ),
                )
                run_writer_job(
                    job_id=application.job_id,
                    root=self.config.state_root,
                    replay_key=replay_key,
                    approved_records=composition.approved_records,
                    review_reader=reviews,
                    journal=journal,
                    application=CurationRuntimeApplication(composition.batch),
                    effect_capability=capability,
                    lease=lease,
                )
                if composition.output_ids:
                    reviews.mark_outputs_delivered(
                        composition.output_ids,
                        delivered_at=self.clock.now(),
                    )
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        except (ProductionCurationError, WriterJobError):
            return ScheduledDispatchResult.failed(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_git_sync(
        self,
        application: WriterJobSpec,
    ) -> ScheduledDispatchResult:
        if self.config.host_identity is None:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            inventory = load_private_git_inventory(
                _named_private_file(self.config, "git_inventory")
            )
            runner = SubprocessGitCommandRunner(
                home_root=inventory.home_root,
                allowed_roots=(
                    self.config.work_root,
                    self.config.personal_root,
                    inventory.dev_root,
                ),
            )
            planner = GitSyncPlanner(
                work_root=self.config.work_root,
                personal_root=self.config.personal_root,
                dev_root=inventory.dev_root,
                repositories=inventory.repositories,
                runner=runner,
            )
            batch = planner.plan_batch()
            capability = GitSyncEffectCapability(
                root=self.config.state_root,
                batch=batch,
                executor=PlannedGitSyncExecutor(planner=planner, runner=runner),
                authority=GitSharedWriterAuthority(LockScope.SHARED_WRITER),
            )
            lease = _CanonicalWriterLease(
                lease=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
            now = self.clock.now()
            if (
                not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            with SqliteReplayJournal(
                root=self.config.state_root,
                clock=self.clock,
            ) as journal:
                run_writer_job(
                    job_id=application.job_id,
                    root=self.config.state_root,
                    replay_key=_scheduled_replay_key(application.job_id, now),
                    journal=journal,
                    application=GitSyncRuntimeApplication(batch),
                    effect_capability=capability,
                    lease=lease,
                    personal_local_only=True,
                )
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            GitInventoryError,
            WriterJobError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_now(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        if self.config.host_identity is None:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            now = self.clock.now()
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
                or not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            edge_root = self.config.state_root / "now" / "edge"
            ingress_root = self.config.state_root / "now" / "ingress"
            edge_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            ingress_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            roots = NowRoots(
                canonical_output_root=self.config.work_root,
                edge_output_root=edge_root,
                ingress_output_root=ingress_root,
            )
            projection = _work_now_projection(self.config.work_root)
            lease = _CanonicalWriterLease(
                lease=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
            capability = NowEffectCapability(
                root=self.config.state_root,
                projection=projection,
                roots=roots,
                authority=NowSharedWriterAuthority(LockScope.SHARED_WRITER),
            )
            with SqliteReplayJournal(
                root=self.config.state_root,
                clock=self.clock,
            ) as journal:
                run_writer_job(
                    job_id=application.job_id,
                    root=self.config.state_root,
                    replay_key=_scheduled_replay_key(application.job_id, now),
                    journal=journal,
                    application=NowRuntimeApplication(projection),
                    effect_capability=capability,
                    lease=lease,
                )
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (CanonicalWriterAuthorityError, ConfigError, WriterRecordError):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_index(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        if self.config.host_identity is None:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
            ):
                return ScheduledDispatchResult.configuration(application.job_id)
            now = self.clock.now()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return ScheduledDispatchResult.configuration(application.job_id)
            output_root = self.config.state_root / "index"
            roots = IndexRoots(
                pages_root=self.config.work_root,
                captures_root=self.config.saved_content_root,
                output_root=output_root,
            )
            embedder = _DeterministicLocalIndexEmbedder()
            capability = IndexEffectCapability(
                root=output_root,
                roots=roots,
                embedder=embedder,
                privacy=PrivacyDecision.create(
                    tier=PrivacyTier.WORK,
                    reason=PrivacyReason.POLICY_WORK,
                    policy_version="open-brain-index-v1",
                    authority=Authority(cloud=False, external_egress=False),
                ),
            )
            lease = _CanonicalScopedLease(
                authority=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                scoped=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                expected_scope=LockScope.INDEX,
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
            window = now.replace(
                hour=now.hour - now.hour % 2,
                minute=0,
                second=0,
                microsecond=0,
            )
            replay_key = f"job-016-{window.astimezone(UTC).strftime('%Y%m%dT%H%MZ')}"
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            IndexOperationError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.configuration(application.job_id)
        try:
            with SqliteReplayJournal(
                root=self.config.state_root,
                clock=self.clock,
            ) as journal:
                run_writer_job(
                    job_id=application.job_id,
                    root=output_root,
                    replay_key=replay_key,
                    journal=journal,
                    application=IndexWriterApplication(
                        database_name=roots.database_name,
                        embedding_model_id=embedder.model_id,
                    ),
                    effect_capability=capability,
                    lease=lease,
                )
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.job_id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            IndexOperationError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.job_id)
        except Exception:
            return ScheduledDispatchResult.failed(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def _dispatch_life_os(self, application: JobSpec) -> ScheduledDispatchResult:
        reference = self.environment.get("OPEN_BRAIN_LIFEOS_CONFIG")
        if not isinstance(reference, str) or not reference:
            return ScheduledDispatchResult.configuration(application.id)
        context = self._optional_writer_context(application)
        if context is None:
            return ScheduledDispatchResult.configuration(application.id)
        now, lease = context
        try:
            life_os_config = load_private_life_os_config(Path(reference))
            with lease.acquire(LockScope.SHARED_WRITER):
                runtime = LifeOSPlanningRuntime.bind(root=self.config.state_root)
                plan_date = now.date()
                if application.id == "JOB-017":
                    runtime.midday(
                        LifePlanRequest(
                            plan_date=plan_date,
                            action_candidates=approved_life_os_candidates(
                                root=self.config.state_root,
                                clock=self.clock,
                                config=life_os_config,
                            ),
                        )
                    )
                elif application.id == "JOB-018":
                    runtime.execute_plan(
                        LifePlanRequest(
                            plan_date=plan_date,
                            action_candidates=approved_life_os_candidates(
                                root=self.config.state_root,
                                clock=self.clock,
                                config=life_os_config,
                            ),
                        )
                    )
                else:
                    runtime.execute_reset(LifeResetRequest(plan_date=plan_date))
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            OptionalAutomationConfigError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.id)
        except Exception:
            return ScheduledDispatchResult.failed(application.id)
        return ScheduledDispatchResult.completed(application.id)

    def _dispatch_messaging(self, application: JobSpec) -> ScheduledDispatchResult:
        reference = self.environment.get("OPEN_BRAIN_MESSAGES_CONFIG")
        if not isinstance(reference, str) or not reference:
            return ScheduledDispatchResult.configuration(application.id)
        context = self._optional_writer_context(application)
        if context is None:
            return ScheduledDispatchResult.configuration(application.id)
        _now, lease = context
        try:
            resource_ref = load_private_messages_config(Path(reference)).resource_ref
            with lease.acquire(LockScope.SHARED_WRITER):
                state = PersistentMessagingCursorStore(
                    root=self.config.state_root,
                    clock=self.clock,
                )
                request = ProviderSyncRequest(
                    capability=Capability.MESSAGING,
                    resource_ref=resource_ref,
                    cursor_ref=state.current_cursor(resource_ref),
                    dry_run=application.id == "JOB-021",
                )
                result = PersistentMessagingRuntime(
                    source=SqliteMessageInbox(
                        root=self.config.state_root,
                        clock=self.clock,
                    ),
                    reviews=SqliteReviewProposalWriter(
                        root=self.config.state_root,
                        clock=self.clock,
                    ),
                    state=state,
                    config=IntegrationConfig(
                        live_adapters=frozenset({Capability.MESSAGING})
                    ),
                ).sync(request)
        except LockBusyError:
            return ScheduledDispatchResult.lock_held(application.id)
        except (
            CanonicalWriterAuthorityError,
            ConfigError,
            OptionalAutomationConfigError,
            WriterRecordError,
        ):
            return ScheduledDispatchResult.configuration(application.id)
        except Exception:
            return ScheduledDispatchResult.failed(application.id)
        if result.status in {SyncStatus.COMPLETED, SyncStatus.DRY_RUN}:
            return ScheduledDispatchResult.completed(application.id)
        if result.status is SyncStatus.UNSUPPORTED:
            return ScheduledDispatchResult.configuration(application.id)
        return ScheduledDispatchResult.failed(application.id)

    def _optional_writer_context(
        self,
        application: JobSpec,
    ) -> tuple[datetime, _CanonicalWriterLease] | None:
        if self.config.host_identity is None:
            return None
        try:
            writer_record = read_canonical_writer_record(self.config.state_root)
            now = self.clock.now()
            if (
                writer_record is None
                or writer_record.identity_id != self.config.host_identity
                or not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
                or application.lock_scope is not LockScope.SHARED_WRITER
            ):
                return None
            lease = _CanonicalWriterLease(
                lease=FileLease(
                    self.config.state_root,
                    self.config.host_identity,
                    clock=self.clock.now,
                ),
                validate_authority=lambda: _require_writer_record(
                    self.config,
                    writer_record,
                ),
            )
        except (ConfigError, WriterRecordError):
            return None
        return now, lease


def build_command_adapters(
    config: AppConfig,
    *,
    environment: Mapping[str, object] | None = None,
) -> CommandAdapterRegistry:
    """Build the real command-adapter registry for one loaded application config."""
    env = {} if environment is None else environment
    retention = None
    retention_reference = env.get("OPEN_BRAIN_RETENTION_CONFIG")
    if isinstance(retention_reference, str) and retention_reference:
        try:
            retention = compose_production_retention_service(
                app_config=config,
                config_path=Path(retention_reference),
                clock=_SystemClock(),
            )
        except ProductionRetentionError:
            retention = None
    application = compose_production_application(
        config=config,
        clock=_utc_now,
        retention=retention,
    )
    production = build_production_command_adapters(application.command_dependencies)
    adapters = dict(production.adapters)
    adapters.pop("migrate", None)
    adapters.update(
        {
            "config": ConfigCommandAdapter(config=config),
            "doctor": DoctorCommandAdapter(probes=_doctor_probes(config)),
            "inbox": UnavailablePhase1CommandAdapter(command="inbox"),
            "review": ConfiguredReviewCommandAdapter(config=config, clock=_SystemClock()),
            "spaces": UnavailablePhase1CommandAdapter(command="spaces"),
        }
    )
    return CommandAdapterRegistry(
        adapters=adapters
    )


def _ledger_taxonomy(config: AppConfig) -> LedgerTaxonomy:
    taxonomy = config.ledger.taxonomy
    return LedgerTaxonomy.create(
        version=taxonomy.version,
        routes=tuple(
            LedgerRoute.create(
                path_prefix=route.path_prefix,
                topic_id=route.topic_id,
                topic_label=route.topic_label,
                privacy_tier=route.privacy_tier,
            )
            for route in taxonomy.routes
        ),
    )


def _doctor_probes(
    config: AppConfig,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Mapping[ProbeName, DoctorProbe]:
    current_time = _utc_now if clock is None else clock
    probes = {name: unavailable_probe() for name in ProbeName}
    probes[ProbeName.CONFIGURATION] = configuration_probe(lambda: config)
    probes[ProbeName.QUEUE_AGE] = queue_age_probe(
        reader=lambda: read_pending_queue_snapshot(config.capture_root),
        clock=current_time,
        stale_after_seconds=3_600,
    )
    probes[ProbeName.SCHEMA] = schema_probe(reader=lambda: _schema_snapshot(config))
    probes[ProbeName.WRITER_OWNERSHIP] = writer_ownership_probe(
        host_identity=config.host_identity,
        reader=lambda: read_canonical_writer_record(config.state_root),
    )
    probes[ProbeName.LOCK_STATE] = lock_state_probe(
        reader=lambda: inspect_file_leases(config.state_root),
        clock=current_time,
        stale_after_seconds=_LOCK_STALE_AFTER_SECONDS,
    )
    probes[ProbeName.BACKUP_EVIDENCE] = backup_evidence_probe(
        reader=lambda: _backup_evidence_snapshot(config),
        clock=current_time,
        stale_after_seconds=129_600,
    )
    probes[ProbeName.STALE_REFERENCES] = stale_reference_probe(
        reader=lambda: _stale_reference_snapshot(config)
    )
    probes[ProbeName.OPTIONAL_PROVIDER] = optional_provider_probe(config)
    return probes


def _degraded_doctor_adapters() -> CommandAdapterRegistry:
    def invalid_config() -> AppConfig:
        raise ConfigError("configuration unavailable")

    probes = {name: unavailable_probe() for name in ProbeName}
    probes[ProbeName.CONFIGURATION] = configuration_probe(invalid_config)
    return CommandAdapterRegistry({"doctor": DoctorCommandAdapter(probes=probes)})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_writer_record(
    config: AppConfig,
    expected: object,
) -> None:
    current = read_canonical_writer_record(config.state_root)
    if current != expected:
        raise CanonicalWriterAuthorityError("canonical writer authority changed")


def _named_private_file(config: AppConfig, name: str) -> Path:
    references = tuple(
        reference for reference in config.secret_refs if reference.name == name
    )
    if (
        len(references) != 1
        or references[0].reference.kind is not SecretRefKind.FILE
    ):
        raise ConfigError("private configuration reference unavailable")
    return Path(references[0].reference.value)


@dataclass(frozen=True, slots=True)
class _SystemClock:
    def now(self) -> datetime:
        return _utc_now()


def _schema_snapshot(config: AppConfig) -> SchemaSnapshot:
    event = inspect_event_schema(
        root=config.state_root,
        database_name="events/events.sqlite3",
    )
    review = inspect_review_schema(
        root=config.state_root,
        database_name="review/review.sqlite3",
    )
    return SchemaSnapshot(
        capture_version=event.version,
        expected_capture_version=SCHEMA_VERSION,
        review_version=review.version,
        expected_review_version=REVIEW_SCHEMA_VERSION,
        capture_valid=event.valid,
        review_valid=review.valid,
    )


def _stale_reference_snapshot(config: AppConfig) -> StaleReferenceSnapshot:
    inspection = inspect_published_references(
        metadata_root=config.state_root,
        database_name="ledger/ledger.sqlite3",
        content_root=config.work_root,
    )
    return StaleReferenceSnapshot(
        reference_count=inspection.reference_count,
        stale_count=inspection.stale_count,
    )


def _backup_evidence_snapshot(config: AppConfig) -> BackupEvidenceSnapshot:
    inspection = inspect_backup_evidence(config.backup_root)
    return BackupEvidenceSnapshot(
        manifest_count=inspection.manifest_count,
        malformed_count=inspection.malformed_count,
        profiles=tuple(
            BackupProfileEvidence(profile, latest)
            for profile, latest in inspection.profile_latest
        ),
    )


def _scheduled_replay_key(job_id: str, now: datetime) -> str:
    normalized = now.astimezone(UTC)
    if job_id in {"JOB-005", "JOB-015", "JOB-022"}:
        normalized = normalized.replace(
            minute=normalized.minute - normalized.minute % 5,
            second=0,
            microsecond=0,
        )
        suffix = normalized.strftime("%Y%m%dT%H%MZ")
    else:
        suffix = normalized.date().isoformat()
    return f"{job_id.lower()}-{suffix}"


def _work_now_projection(work_root: Path) -> NowProjectionInput:
    pages_root = work_root / "pages"
    items: list[NowItem] = []
    try:
        paths = sorted(pages_root.rglob("*.md"))
    except OSError:
        paths = []
    for path in paths:
        if len(items) >= 100:
            break
        if path.is_symlink():
            continue
        try:
            relative = path.relative_to(work_root).as_posix()
            payload = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        title = next(
            (
                line[2:].strip()
                for line in payload.splitlines()[:80]
                if line.startswith("# ") and line[2:].strip()
            ),
            path.stem,
        )
        try:
            items.append(
                NowItem(
                    title=title[:1000],
                    source_ref=relative,
                    priority=len(items) + 1,
                    privacy_tier=PrivacyTier.WORK,
                )
            )
        except Exception:
            continue
    return NowProjectionInput(
        focus=tuple(items[:20]),
        queue=tuple(items[20:]),
        life_os=(),
        messages=(),
    )
