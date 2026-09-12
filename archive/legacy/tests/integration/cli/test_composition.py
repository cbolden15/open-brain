from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.capture.models import CaptureWorkItem
from open_brain_engine.core.ids import canonical_json_bytes, capture_id_for, review_id_for
from open_brain_engine.core.models import (
    Authority,
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.engine import LockScope
from open_brain_engine.providers.base import ProviderService
from open_brain_engine.providers.deterministic import DeterministicDistillationProvider
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.sqlite import connect_database, migrate
from open_brain_engine.storage.writer_record import write_canonical_writer_record

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy._compat.open_brain.config import (
    AppConfig,
    NamedSecretRef,
    RetainedRoots,
    SecretRef,
    SecretRefKind,
)
from open_brain_legacy._compat.open_brain.services.connectors import (
    INTERNAL_CONNECTOR_ENTRY_POINT_GROUP,
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureIdentity,
    ConnectorHost,
    ConnectorManifest,
    ConnectorMetadataLogger,
    ConnectorProfile,
    ConnectorRegistry,
    ConnectorRunContext,
)
from open_brain_legacy._compat.open_brain.services.http_server import HttpServerFactory
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import MediaCommand
from open_brain_legacy._compat.open_brain_connectors.capture.poll import FilesystemYouTubePollState
from open_brain_legacy._compat.open_brain_connectors.production.youtube_poll import (
    YouTubePollCheckpoint,
    YouTubeReferenceConnector,
    YouTubeReferenceTransport,
    load_private_youtube_config,
)
from open_brain_legacy.capture.queue import FilesystemCaptureQueue
from open_brain_legacy.cli._registry import SCHEDULED_ROUTES, command_names
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.scheduled import ScheduledDispatchStatus, dispatch_scheduled_route
from open_brain_legacy.integrations.life_os import LifePlanRequest
from open_brain_legacy.integrations.life_os_runtime import (
    LifeOSPlanningRuntime,
    LifeOSRuntimeOperation,
)
from open_brain_legacy.integrations.messaging import (
    MessageBatch,
    MessageCandidate,
    MessageConfidence,
)
from open_brain_legacy.integrations.messaging_runtime import (
    PersistentMessagingCursorStore,
    SqliteMessageInbox,
)
from open_brain_legacy.ledger.store import PublishedReferenceInspection
from open_brain_legacy.operations.backup import get_backup_job
from open_brain_legacy.operations.backup_writer import (
    FilesystemBackupSource,
    FilesystemBackupStore,
)
from open_brain_legacy.operations.capture_jobs import get_capture_job
from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.index import IndexRoots, check_index
from open_brain_legacy.operations.scheduler import EXPECTED_JOB_IDS
from open_brain_legacy.operations.writer_jobs import WriterLease, get_writer_job_spec
from open_brain_legacy.production.imessage import ImessageHistoryClient
from open_brain_legacy.review.maintenance import (
    CurationClass,
    CurationTarget,
    predecessor_curation_taxonomy,
)
from open_brain_legacy.review.store import SqliteReviewStore
from open_brain_legacy.services.application import (
    ConfiguredScheduledAdapters,
    SingleUserLocalApplication,
    build_command_adapters,
)
from open_brain_legacy.services.entrypoints import run_legacy_cli as run

_VALID_ENVIRONMENT = {
    "OPEN_BRAIN_STATE_ROOT": "/synthetic/state",
    "OPEN_BRAIN_WORK_ROOT": "/synthetic/work",
    "OPEN_BRAIN_PERSONAL_ROOT": "/synthetic/personal",
    "OPEN_BRAIN_CAPTURE_ROOT": "/synthetic/capture",
    "OPEN_BRAIN_SAVED_CONTENT_ROOT": "/synthetic/saved-content",
    "OPEN_BRAIN_BACKUP_ROOT": "/synthetic/backup",
}


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


@dataclass
class _YouTubeMediaAdapter:
    calls: list[str] = field(default_factory=list)

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        del url, command
        self.calls.append("playlist")
        return ("video000001",)

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        del command
        self.calls.append(video_id)
        return YouTubeMediaResult(
            title="Synthetic video",
            caption_vtt="WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic transcript",
            captions_pending=False,
        )


@dataclass(frozen=True, slots=True)
class _YouTubeConnectorEntryPoint:
    name: str = "youtube"
    value: str = "synthetic.youtube:connector"
    connector: object = field(default_factory=YouTubeReferenceConnector)

    def load(self) -> object:
        return self.connector


@dataclass(frozen=True, slots=True)
class _YouTubeConnectorSource:
    entry_point: _YouTubeConnectorEntryPoint = field(
        default_factory=_YouTubeConnectorEntryPoint
    )

    def entry_points(self, *, group: str) -> tuple[_YouTubeConnectorEntryPoint, ...]:
        assert group == INTERNAL_CONNECTOR_ENTRY_POINT_GROUP
        return (self.entry_point,)


def _youtube_connector_application(
    *,
    brain_root: Path,
    config: AppConfig,
    youtube_config: Path,
    media: _YouTubeMediaAdapter,
) -> tuple[
    SingleUserLocalApplication,
    dict[
        str,
        Callable[
            [ConnectorManifest, ConnectorBudget, ConnectorMetadataLogger],
            ConnectorRunContext,
        ],
    ],
]:
    profile = ConnectorProfile(
        allow_list=("youtube",),
        egress_enabled=config.egress_enabled,
        budget_limits=ConnectorBudgetLimits(
            max_fetches=50,
            max_extractions=1_000,
            max_submissions=1_000,
        ),
    )
    application = SingleUserLocalApplication.open(
        brain_root,
        connector_profile=profile,
        connector_host=ConnectorHost(ConnectorRegistry(_YouTubeConnectorSource())),
    )

    def context_factory(
        manifest: ConnectorManifest,
        budget: ConnectorBudget,
        logger: ConnectorMetadataLogger,
    ) -> ConnectorRunContext:
        assert manifest == YouTubeReferenceConnector.manifest
        poll_config = load_private_youtube_config(youtube_config)
        return ConnectorRunContext(
            capture_identity=ConnectorCaptureIdentity(
                "youtube",
                "JOB-029",
                application.public_job_context("JOB-029"),
            ),
            capture_sink=application.public_job_sink("JOB-029"),
            transport=YouTubeReferenceTransport(
                subscriptions=poll_config.subscriptions,
                media_adapter=media,
            ),
            checkpoint=YouTubePollCheckpoint(
                FilesystemYouTubePollState(config.state_root / "youtube-poll")
            ),
            clock=FixedClock().now,
            budget=budget,
            metadata_logger=logger,
        )

    return application, {"youtube": context_factory}


@dataclass
class _ImessageHistory(ImessageHistoryClient):
    payload: bytes = b""
    calls: int = 0

    def history(self, *, chat_id: str, after_rowid: int) -> bytes:
        assert chat_id == "synthetic-chat"
        assert after_rowid >= 0
        self.calls += 1
        return self.payload


@dataclass
class _HttpServer:
    served: int = 0
    closed: int = 0

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        assert poll_interval == 0.5
        self.served += 1

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self.closed += 1


@dataclass
class _HttpServerFactory:
    addresses: list[tuple[str, int]]
    servers: list[_HttpServer]

    def __call__(
        self,
        address: tuple[str, int],
        _handler: type[BaseHTTPRequestHandler],
    ) -> _HttpServer:
        self.addresses.append(address)
        server = _HttpServer()
        self.servers.append(server)
        return server


def _filesystem_config(tmp_path: Path) -> AppConfig:
    paths = {
        "work": tmp_path / "work",
        "personal": tmp_path / "personal",
        "capture": tmp_path / "capture",
        "saved_content": tmp_path / "saved-content",
        "state": tmp_path / "state",
        "backup": tmp_path / "backup",
    }
    for path in paths.values():
        path.mkdir()
    config = AppConfig(
        roots=RetainedRoots(
            work=paths["work"],
            personal=paths["personal"],
            capture=paths["capture"],
            saved_content=paths["saved_content"],
            state=paths["state"],
        ),
        backup=paths["backup"],
        host_identity="synthetic-host",
    )
    write_canonical_writer_record(
        state_root=config.state_root,
        identity_id="synthetic-host",
        generation=1,
        recorded_at=FixedClock().now(),
    )
    return config


def _optional_automation_environment(
    tmp_path: Path,
    *,
    message_resource_ref: str = "messages_primary",
) -> dict[str, str]:
    life_os_config = tmp_path / "life-os.json"
    life_os_config.write_bytes(canonical_json_bytes({"schema_version": 1, "candidate_limit": 100}))
    messages_config = tmp_path / "messages.json"
    messages_config.write_bytes(
        canonical_json_bytes({"schema_version": 1, "resource_ref": message_resource_ref})
    )
    for path in (life_os_config, messages_config):
        path.chmod(0o600)
    return {
        "OPEN_BRAIN_LIFEOS_CONFIG": str(life_os_config),
        "OPEN_BRAIN_MESSAGES_CONFIG": str(messages_config),
    }


def _retention_environment(tmp_path: Path, config: AppConfig) -> dict[str, str]:
    recovery = config.backup_root / "recovery-baseline.json"
    recovery.write_bytes(b"synthetic recovery baseline")
    retention_config = tmp_path / "retention.json"
    retention_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "root": "backup",
                "candidates": [
                    {
                        "artifact_id": "artifact_recovery_baseline",
                        "relative_path": recovery.name,
                        "expires_at": "2026-08-01T00:00:00Z",
                        "kind": "recovery_critical",
                    }
                ],
            }
        )
    )
    retention_config.chmod(0o600)
    return {"OPEN_BRAIN_RETENTION_CONFIG": str(retention_config)}


def _provider_environment(tmp_path: Path) -> dict[str, str]:
    provider_config = tmp_path / "provider.json"
    provider_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "local_endpoint": "http://127.0.0.1:11434/api/generate",
                "local_model": "synthetic-model",
                "cloud_module": "open_brain_legacy.providers.optional_cloud",
                "cloud_model": "synthetic-cloud-model",
                "credential_name": "provider_token",
            }
        )
    )
    provider_config.chmod(0o600)
    return {"OPEN_BRAIN_PROVIDER_CONFIG": str(provider_config)}


def _deterministic_provider_service() -> ProviderService:
    provider = DeterministicDistillationProvider()
    return ProviderService(
        provider_name="local",
        cloud_enabled=False,
        local_factory=lambda: provider,
        cloud_factory=lambda _credential: provider,
        resolve_cloud_secret=lambda: None,
    )


def _create_applied_action_review(config: AppConfig) -> tuple[str, str]:
    aggregate = ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + "a" * 64,
            source_ref="https://example.test/synthetic-action",
            privacy_tier=PrivacyTier.PERSONAL,
            proposed_intent=Intent.ACTION_CANDIDATE,
            proposal_reason="Synthetic planning candidate",
            capture_why="Owner approved synthetic planning candidate",
            created_at=FixedClock().now(),
            created_by=Actor(ActorKind.SYSTEM, "synthetic-planner"),
        )
    )
    payload = canonical_json_bytes(aggregate.to_dict())
    with SqliteReviewStore(
        root=config.state_root,
        database_name="review/review.sqlite3",
        clock=FixedClock(),
    ) as reviews:
        reviews.create_if_absent(
            aggregate,
            payload_digest=sha256(payload).hexdigest(),
        )
        decision = reviews.decide(
            aggregate.proposal.review_id,
            ReviewDecisionCommand.create(
                decision_id="decision-synthetic-planning",
                target_state=ReviewState.APPLIED,
                reason="Owner approved synthetic planning candidate",
                occurred_at=FixedClock().now(),
                actor=Actor(ActorKind.OWNER, "synthetic-owner"),
            ),
        )
    assert decision.approved_record is not None
    return decision.approved_record.record_id, str(decision.approved_record.review_id)


def _with_git_inventory(config: AppConfig, tmp_path: Path) -> AppConfig:
    home = tmp_path / "git-home"
    dev = tmp_path / "dev"
    home.mkdir()
    dev.mkdir()
    for root in (config.work_root, config.personal_root):
        subprocess.run(("/usr/bin/git", "init", "--quiet"), cwd=root, check=True)
        subprocess.run(
            ("/usr/bin/git", "config", "user.name", "Synthetic Operator"),
            cwd=root,
            check=True,
        )
        subprocess.run(
            ("/usr/bin/git", "config", "user.email", "synthetic@example.test"),
            cwd=root,
            check=True,
        )
    inventory = tmp_path / "git-inventory.json"
    inventory.write_bytes(
        canonical_json_bytes(
            {
                "version": 1,
                "home_root": str(home),
                "dev_root": str(dev),
                "repositories": [
                    {
                        "repo_id": "work_brain",
                        "kind": "work",
                        "relative_path": ".",
                        "record_id": "work_brain_sync",
                        "digest_sha256": "a" * 64,
                        "push_target_digest_sha256": None,
                    },
                    {
                        "repo_id": "personal_brain",
                        "kind": "personal",
                        "relative_path": ".",
                        "record_id": "personal_brain_sync",
                        "digest_sha256": "b" * 64,
                        "push_target_digest_sha256": None,
                    },
                ],
            }
        )
    )
    inventory.chmod(0o600)
    return replace(
        config,
        secret_refs=(
            NamedSecretRef(
                "git_inventory",
                SecretRef(SecretRefKind.FILE, str(inventory)),
            ),
            NamedSecretRef(
                "ingress_service_token",
                SecretRef.parse("env:OPEN_BRAIN_INGRESS_TOKEN"),
            ),
            NamedSecretRef(
                "ui_service_token",
                SecretRef.parse("env:OPEN_BRAIN_UI_TOKEN"),
            ),
        ),
    )


def _synthetic_config() -> AppConfig:
    return AppConfig(
        roots=RetainedRoots(
            work=Path("/synthetic/work"),
            personal=Path("/synthetic/personal"),
            capture=Path("/synthetic/capture"),
            saved_content=Path("/synthetic/saved-content"),
            state=Path("/synthetic/state"),
        ),
        backup=Path("/synthetic/backup"),
        provider="local",
        cloud_enabled=False,
        egress_enabled=False,
    )


def test_composition_root_wires_a_real_config_adapter_end_to_end() -> None:
    adapters = build_command_adapters(_synthetic_config())

    exit_code = main(("config", "--json"), command_adapters=adapters)

    assert exit_code is ExitCode.SUCCESS


def test_composition_root_wires_every_public_command_family(tmp_path: Path) -> None:
    adapters = build_command_adapters(_filesystem_config(tmp_path))

    assert tuple(sorted(adapters.adapters)) == tuple(
        command for command in command_names() if command != "migrate"
    )


def test_default_composition_excludes_pre_alpha_compatibility_routes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapters = build_command_adapters(_filesystem_config(tmp_path))

    assert {"migrate", "parity", "shadow"}.isdisjoint(adapters.adapters)
    assert {"parity", "shadow"}.isdisjoint(command_names())
    assert main(("migrate", "--json"), command_adapters=adapters) is ExitCode.FAILURE
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    for arguments in (
        ("parity", "--json"),
        ("shadow", "--json"),
        ("doctor", "--cutover", "--json"),
    ):
        assert main(arguments, command_adapters=adapters) is ExitCode.USAGE
        assert json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_composition_root_runs_confined_query_capture_and_proposals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    pages = config.work_root / "pages"
    pages.mkdir()
    (pages / "synthetic.md").write_text(
        "---\ntitle: Synthetic topic\n---\n\nA bounded work-only result.\n",
        encoding="utf-8",
    )
    adapters = build_command_adapters(config)

    query_exit = main(("query", "synthetic", "--json"), command_adapters=adapters)
    query_output = json.loads(capsys.readouterr().out)
    capture_exit = main(
        ("capture", "text", "Synthetic capture", "Owner-authored fixture", "--json"),
        command_adapters=adapters,
    )
    capture_output = json.loads(capsys.readouterr().out)
    proposal_exit = main(("proposals", "list", "--json"), command_adapters=adapters)
    proposal_output = json.loads(capsys.readouterr().out)

    assert query_exit is ExitCode.SUCCESS
    assert query_output["status"] == "ok"
    assert query_output["results"]
    assert capture_exit is ExitCode.SUCCESS
    assert capture_output["status"] == "queued"
    assert proposal_exit is ExitCode.SUCCESS
    assert proposal_output == {
        "command": "proposals",
        "proposals": [],
        "status": "listed",
    }


def test_composition_root_status_is_complete_on_initialized_empty_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    (config.work_root / "pages").mkdir()
    adapters = build_command_adapters(config)

    exit_code = main(("status", "--strict", "--json"), command_adapters=adapters)
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.SUCCESS
    assert output["status"] == "complete"
    assert len(output["metrics"]) == 8


def test_composition_root_status_fails_closed_on_corrupt_run_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    (config.work_root / "pages").mkdir()
    runlog = config.state_root / "runlog" / "JOB-004"
    runlog.mkdir(parents=True)
    (runlog / ("a" * 64 + ".json")).write_text(
        '{"schema_version":1,"broken":true}',
        encoding="utf-8",
    )

    exit_code = main(
        ("status", "--strict", "--json"),
        command_adapters=build_command_adapters(config),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.FAILURE
    assert output == {
        "command": "status",
        "error": {
            "code": "production_command_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }


def test_composition_root_config_adapter_rejects_unexpected_arguments() -> None:
    adapters = build_command_adapters(_synthetic_config())

    exit_code = main(("config", "extra-argument"), command_adapters=adapters)

    assert exit_code is ExitCode.USAGE


def test_composition_root_wires_review_edit_to_the_real_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    review = ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + "a" * 64,
            source_ref="synthetic-source",
            privacy_tier=PrivacyTier.WORK,
            proposed_intent=Intent.IDEA,
            proposal_reason="Synthetic proposal",
            capture_why="Synthetic owner statement",
            created_at=FixedClock().now(),
            created_by=Actor(ActorKind.SYSTEM, "fixture"),
        )
    )
    taxonomy = predecessor_curation_taxonomy()
    with SqliteReviewStore(
        root=config.state_root,
        database_name="review/review.sqlite3",
        clock=FixedClock(),
    ) as store:
        payload = canonical_json_bytes(review.to_dict())
        store.create_if_absent(review, payload_digest=sha256(payload).hexdigest())
        store.register_curation_target(
            CurationTarget.create(
                review=review,
                tier=PrivacyTier.WORK,
                category="projects",
                slug="synthetic",
                title="Synthetic title",
                classification_class=CurationClass.NEW_PAGE,
                occurred_at=FixedClock().now(),
                taxonomy=taxonomy,
            )
        )

    exit_code = main(
        (
            "--json",
            "review",
            "edit",
            str(review.proposal.review_id),
            "--tier=projects",
            "--category=patterns",
            "--slug=nested/synthetic.md",
        ),
        command_adapters=build_command_adapters(config),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.SUCCESS
    assert output["status"] == "edited"
    with SqliteReviewStore(
        root=config.state_root,
        database_name="review/review.sqlite3",
        clock=FixedClock(),
    ) as store:
        edited = store.get_curation_target(review.proposal.review_id)
        assert edited is not None
        assert edited.tier is PrivacyTier.WORK
        assert edited.page.as_posix() == "patterns/nested/synthetic.md"


def test_run_wires_the_real_entry_point_when_environment_is_valid() -> None:
    exit_code = run(("config", "--json"), environment=_VALID_ENVIRONMENT)

    assert exit_code == ExitCode.SUCCESS


def test_catalog_config_reference_launches_backup_through_process_entry(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    assert all(
        get_job(job_id).env_refs == ("OPEN_BRAIN_CONFIG",)
        for job_id in ("JOB-011", "JOB-014", "JOB-023", "JOB-025")
    )
    config_path = tmp_path / "open-brain.toml"
    config_path.write_text(
        "\n".join(
            (
                "[paths]",
                f'work_root = "{config.work_root}"',
                f'personal_root = "{config.personal_root}"',
                f'capture_root = "{config.capture_root}"',
                f'saved_content_root = "{config.saved_content_root}"',
                f'state_root = "{config.state_root}"',
                f'backup_root = "{config.backup_root}"',
                "",
                "[host]",
                'identity = "synthetic-host"',
                "",
                "[providers]",
                'default = "local"',
                "cloud_enabled = false",
                "",
                "[egress]",
                "enabled = false",
                "",
            )
        ),
        encoding="utf-8",
    )

    exit_code = run(
        ("backup", "run", "--profile=capture", "--json"),
        environment={"OPEN_BRAIN_CONFIG": str(config_path)},
    )

    assert exit_code is ExitCode.SUCCESS


def test_run_degrades_to_unavailable_when_environment_is_incomplete() -> None:
    exit_code = run(("config", "--json"), environment={})

    assert exit_code == ExitCode.FAILURE


def test_run_classifies_scheduled_configuration_failure_as_exit_78(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ("backup", "run", "--profile=capture", "--json"),
        environment={},
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.CONFIGURATION
    assert output["error"]["code"] == "scheduled_application_configuration"


def test_run_still_handles_version_without_any_environment() -> None:
    exit_code = run(("--version",), environment={})

    assert exit_code == ExitCode.SUCCESS


def test_composed_scheduled_adapter_runs_backup_and_replays_window(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    (config.capture_root / "event.json").write_bytes(b"synthetic capture")
    adapters = ConfiguredScheduledAdapters(config=config, clock=FixedClock())

    first = main(
        ("backup", "run", "--profile=capture", "--json"),
        scheduled_adapters=adapters,
    )
    first_output = json.loads(capsys.readouterr().out)
    replay = main(
        ("backup", "run", "--profile=capture", "--json"),
        scheduled_adapters=adapters,
    )
    replay_output = json.loads(capsys.readouterr().out)

    assert first is ExitCode.SUCCESS
    assert replay is ExitCode.SUCCESS
    assert first_output == {"command": "JOB-011", "status": "completed"}
    assert replay_output == first_output
    assert len(tuple((config.backup_root / "backups").iterdir())) == 1


def test_production_composition_routes_every_catalog_job_without_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _with_git_inventory(_filesystem_config(tmp_path), tmp_path)
    (config.work_root / "pages").mkdir()
    (config.state_root / "index").mkdir()
    youtube_config = tmp_path / "youtube.json"
    youtube_config.write_bytes(canonical_json_bytes({"schema_version": 1, "subscriptions": []}))
    youtube_config.chmod(0o600)
    imessage_config = tmp_path / "imessage.json"
    imessage_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "chat_id": "synthetic-chat",
                "allowed_senders": ["owner@example.test"],
            }
        )
    )
    imessage_config.chmod(0o600)
    ui_config = tmp_path / "ui-bind.json"
    ingress_config = tmp_path / "ingress-bind.json"
    for path, port in ((ui_config, 8788), (ingress_config, 8789)):
        path.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "host": "127.0.0.1",
                    "port": port,
                    "allow_private_network": False,
                }
            )
        )
        path.chmod(0o600)
    application, context_factories = _youtube_connector_application(
        brain_root=tmp_path / "brain",
        config=config,
        youtube_config=youtube_config,
        media=_YouTubeMediaAdapter(),
    )
    adapters = ConfiguredScheduledAdapters(
        config=config,
        clock=FixedClock(),
        environment={
            **_optional_automation_environment(tmp_path),
            **_provider_environment(tmp_path),
            **_retention_environment(tmp_path, config),
            "OPEN_BRAIN_IMESSAGE_CONFIG": str(imessage_config),
            "OPEN_BRAIN_YOUTUBE_CONFIG": str(youtube_config),
            "OPEN_BRAIN_UI_CONFIG": str(ui_config),
            "OPEN_BRAIN_INGRESS_CONFIG": str(ingress_config),
            "OPEN_BRAIN_UI_TOKEN": "synthetic-ui-token",
            "OPEN_BRAIN_INGRESS_TOKEN": "synthetic-ingress-token",
        },
        connector_context_factories=context_factories,
        imessage_history_client=_ImessageHistory(),
        public_application=application,
    )
    event_root = config.state_root / "events"
    review_root = config.state_root / "review"
    event_root.mkdir()
    review_root.mkdir()
    connection = connect_database(root=event_root, database_name="events.sqlite3")
    try:
        migrate(connection, clock=FixedClock())
    finally:
        connection.close()
    with SqliteReviewStore(
        root=review_root,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ):
        pass
    source = FilesystemBackupSource(
        work_root=config.work_root,
        personal_root=config.personal_root,
        capture_root=config.capture_root,
        saved_content_root=config.saved_content_root,
        state_root=config.state_root,
    )
    store = FilesystemBackupStore(root=config.backup_root)
    for job_id in ("JOB-011", "JOB-014", "JOB-023", "JOB-025"):
        get_backup_job(job_id).run(
            source=source,
            store=store,
            created_at=FixedClock().now(),
            generation="runtime-2026-08-16" if job_id == "JOB-025" else None,
        )
    assert (
        adapters.dispatch_writer(get_writer_job_spec("JOB-016")).status
        is ScheduledDispatchStatus.COMPLETED
    )
    assert (
        adapters.dispatch_writer(get_writer_job_spec("JOB-022")).status
        is ScheduledDispatchStatus.COMPLETED
    )
    now_payload = (config.work_root / "NOW.md").read_bytes()
    for replica in (
        config.state_root / "now" / "edge" / "NOW.md",
        config.state_root / "now" / "ingress" / "NOW.md",
    ):
        replica.write_bytes(now_payload)
    inbox = SqliteMessageInbox(root=config.state_root, clock=FixedClock())
    inbox.enqueue(
        MessageBatch(
            resource_ref="messages_primary",
            cursor_ref=None,
            next_cursor_ref="cursor_001",
            candidates=(),
        )
    )
    inbox.enqueue(
        MessageBatch(
            resource_ref="messages_primary",
            cursor_ref="cursor_001",
            next_cursor_ref="cursor_002",
            candidates=(),
        )
    )
    monkeypatch.setattr(
        'open_brain_legacy.services.application.inspect_published_references',
        lambda **_: PublishedReferenceInspection(0, 0),
    )

    results = tuple(dispatch_scheduled_route(route, adapters) for route in SCHEDULED_ROUTES)

    assert tuple(route.job_id for route in SCHEDULED_ROUTES) == EXPECTED_JOB_IDS
    assert len(results) == 30
    assert all(result.status is ScheduledDispatchStatus.COMPLETED for result in results), tuple(
        (result.job_id, result.status.value, result.exit_code)
        for result in results
        if result.status is not ScheduledDispatchStatus.COMPLETED
    )
    assert (
        len(
            tuple((config.state_root / "operations" / "effects" / "prepared").glob("*.effect.json"))
        )
        == 4
    )
    assert not (config.state_root / "operations" / "effects" / "empty").exists()


def test_composed_youtube_poll_uses_private_reference_and_durable_engine_capture(
    tmp_path: Path,
) -> None:
    config = replace(_filesystem_config(tmp_path), egress_enabled=True)
    youtube_config = tmp_path / "youtube.json"
    youtube_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "subscriptions": [
                    {
                        "url": "https://www.youtube.com/playlist?list=synthetic",
                        "privacy": PrivacyDecision.create(
                            tier=PrivacyTier.PUBLIC,
                            reason=PrivacyReason.POLICY_PUBLIC,
                            policy_version="privacy-v1",
                            authority=Authority(cloud=False, external_egress=True),
                        ).to_dict(),
                    }
                ],
            }
        )
    )
    youtube_config.chmod(0o600)
    media = _YouTubeMediaAdapter()
    application, context_factories = _youtube_connector_application(
        brain_root=tmp_path / "brain",
        config=config,
        youtube_config=youtube_config,
        media=media,
    )

    adapters = ConfiguredScheduledAdapters(
        config=config,
        clock=FixedClock(),
        environment={"OPEN_BRAIN_YOUTUBE_CONFIG": str(youtube_config)},
        connector_context_factories=context_factories,
        public_application=application,
    )

    result = adapters.dispatch_capture(get_capture_job("JOB-029"))

    assert result.status is ScheduledDispatchStatus.COMPLETED
    assert media.calls == ["playlist", "video000001"]
    assert len(application.tasks.inbox.list()) == 1


def test_process_entrypoint_composes_explicit_youtube_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(_filesystem_config(tmp_path), egress_enabled=True)
    config_path = tmp_path / "open-brain.toml"
    config_path.write_text(
        "\n".join(
            (
                "[paths]",
                f'work_root = "{config.work_root}"',
                f'personal_root = "{config.personal_root}"',
                f'capture_root = "{config.capture_root}"',
                f'saved_content_root = "{config.saved_content_root}"',
                f'state_root = "{config.state_root}"',
                f'backup_root = "{config.backup_root}"',
                "",
                "[host]",
                'identity = "synthetic-host"',
                "",
                "[providers]",
                'default = "local"',
                "cloud_enabled = false",
                "",
                "[egress]",
                "enabled = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    youtube_config = tmp_path / "youtube.json"
    youtube_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "subscriptions": [
                    {
                        "url": "https://www.youtube.com/playlist?list=synthetic",
                        "privacy": PrivacyDecision.create(
                            tier=PrivacyTier.PUBLIC,
                            reason=PrivacyReason.POLICY_PUBLIC,
                            policy_version="privacy-v1",
                            authority=Authority(cloud=False, external_egress=True),
                        ).to_dict(),
                    }
                ],
            }
        )
    )
    youtube_config.chmod(0o600)
    media = _YouTubeMediaAdapter()
    monkeypatch.setattr(
        'open_brain_legacy.production.media.compose_production_capture_media_adapter',
        lambda *, config: media,
    )
    brain_root = tmp_path / "brain"

    exit_code = run(
        (
            "capture",
            "poll",
            "--source=youtube",
            "--mode=ingress",
            "--json",
        ),
        environment={
            "OPEN_BRAIN_CONFIG": str(config_path),
            "OPEN_BRAIN_JOB_ID": "JOB-029",
            "OPEN_BRAIN_ROOT": str(brain_root),
            "OPEN_BRAIN_YOUTUBE_CONFIG": str(youtube_config),
        },
    )

    assert exit_code is ExitCode.SUCCESS
    assert media.calls == ["playlist", "video000001"]
    assert len(SingleUserLocalApplication.open(brain_root).tasks.inbox.list()) == 1


def test_composed_youtube_poll_requires_its_declared_environment_reference(
    tmp_path: Path,
) -> None:
    config = replace(_filesystem_config(tmp_path), egress_enabled=True)
    youtube_config = tmp_path / "youtube.json"
    youtube_config.write_bytes(
        canonical_json_bytes({"schema_version": 1, "subscriptions": []})
    )
    youtube_config.chmod(0o600)
    media = _YouTubeMediaAdapter()
    application, context_factories = _youtube_connector_application(
        brain_root=tmp_path / "brain",
        config=config,
        youtube_config=youtube_config,
        media=media,
    )
    result = ConfiguredScheduledAdapters(
        config=config,
        clock=FixedClock(),
        connector_context_factories=context_factories,
        public_application=application,
    ).dispatch_capture(get_capture_job("JOB-029"))

    assert result.status is ScheduledDispatchStatus.FAILED
    assert result.exit_code == 78
    assert media.calls == []


def test_composed_imessage_ingress_uses_private_reference_and_durable_engine_capture(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    private_config = tmp_path / "imessage.json"
    private_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "chat_id": "synthetic-chat",
                "allowed_senders": ["owner@example.test"],
            }
        )
    )
    private_config.chmod(0o600)
    history = _ImessageHistory(
        payload=canonical_json_bytes(
            {
                "rowid": 1,
                "chat_id": "synthetic-chat",
                "sender": "owner@example.test",
                "text": "Synthetic local message",
                "timestamp": "2026-08-16T19:59:00Z",
            }
        )
    )
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    adapters = ConfiguredScheduledAdapters(
        config=config,
        clock=FixedClock(),
        environment={"OPEN_BRAIN_IMESSAGE_CONFIG": str(private_config)},
        imessage_history_client=history,
        public_application=application,
    )

    result = adapters.dispatch_capture(get_capture_job("JOB-005"))

    assert result.status is ScheduledDispatchStatus.COMPLETED
    assert history.calls == 1
    assert len(application.tasks.inbox.list()) == 1


def test_composed_imessage_service_uses_keepalive_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _filesystem_config(tmp_path)
    private_config = tmp_path / "imessage.json"
    private_config.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "chat_id": "synthetic-chat",
                "allowed_senders": ["owner@example.test"],
            }
        )
    )
    private_config.chmod(0o600)
    calls: list[str] = []

    class _Runtime:
        def run_once(self) -> None:
            calls.append("once")

        def run_forever(self) -> None:
            calls.append("forever")

    monkeypatch.setattr(
        'open_brain_legacy.services.application.compose_production_imessage_ingress',
        lambda **_: _Runtime(),
    )
    adapters = ConfiguredScheduledAdapters(
        config=config,
        clock=FixedClock(),
        environment={"OPEN_BRAIN_IMESSAGE_CONFIG": str(private_config)},
        imessage_service_mode=True,
        public_application=SingleUserLocalApplication.open(tmp_path / "brain"),
    )

    result = adapters.dispatch_capture(get_capture_job("JOB-005"))

    assert result.status is ScheduledDispatchStatus.COMPLETED
    assert calls == ["forever"]


def test_composed_imessage_ingress_requires_declared_environment_reference(
    tmp_path: Path,
) -> None:
    result = ConfiguredScheduledAdapters(
        config=_filesystem_config(tmp_path),
        clock=FixedClock(),
        imessage_history_client=_ImessageHistory(),
    ).dispatch_capture(get_capture_job("JOB-005"))

    assert result.status is ScheduledDispatchStatus.FAILED
    assert result.exit_code == 78


def test_composed_http_jobs_start_closed_route_lifecycles(
    tmp_path: Path,
) -> None:
    config = replace(
        _filesystem_config(tmp_path),
        secret_refs=(
            NamedSecretRef(
                "ingress_service_token",
                SecretRef.parse("env:OPEN_BRAIN_INGRESS_TOKEN"),
            ),
            NamedSecretRef(
                "ui_service_token",
                SecretRef.parse("env:OPEN_BRAIN_UI_TOKEN"),
            ),
        ),
    )
    (config.work_root / "pages").mkdir()
    ui_config = tmp_path / "ui-bind.json"
    ingress_config = tmp_path / "ingress-bind.json"
    for path, port in ((ui_config, 8788), (ingress_config, 8789)):
        path.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "host": "127.0.0.1",
                    "port": port,
                    "allow_private_network": False,
                }
            )
        )
        path.chmod(0o600)
    factory = _HttpServerFactory([], [])
    adapters = ConfiguredScheduledAdapters(
        config=config,
        clock=FixedClock(),
        environment={
            "OPEN_BRAIN_UI_CONFIG": str(ui_config),
            "OPEN_BRAIN_INGRESS_CONFIG": str(ingress_config),
            "OPEN_BRAIN_UI_TOKEN": "synthetic-ui-token",
            "OPEN_BRAIN_INGRESS_TOKEN": "synthetic-ingress-token",
        },
        http_service_mode=True,
        http_server_factory=cast(HttpServerFactory, factory),
        public_application=SingleUserLocalApplication.open(tmp_path / "brain"),
    )

    results = (
        adapters.dispatch_optional(get_job("JOB-026")),
        adapters.dispatch_capture(get_capture_job("JOB-027")),
        adapters.dispatch_capture(get_capture_job("JOB-028")),
    )

    assert all(result.status is ScheduledDispatchStatus.COMPLETED for result in results)
    assert factory.addresses == [
        ("127.0.0.1", 8788),
        ("127.0.0.1", 8789),
        ("127.0.0.1", 8789),
    ]
    assert [(server.served, server.closed) for server in factory.servers] == [
        (1, 1),
        (1, 1),
        (1, 1),
    ]


def test_composed_git_sync_requires_one_owner_only_inventory_reference(
    tmp_path: Path,
) -> None:
    result = ConfiguredScheduledAdapters(
        _filesystem_config(tmp_path),
        FixedClock(),
    ).dispatch_writer(get_writer_job_spec("JOB-015"))

    assert result.status is ScheduledDispatchStatus.FAILED
    assert result.exit_code == 78


def test_composed_nightly_job_drains_work_capture_and_distills_locally(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    text = "Synthetic nightly capture"
    envelope = CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=text,
        captured_at=FixedClock().now(),
        capture_why="Preserve the synthetic nightly context",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.CLI,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:" + sha256(text.encode()).hexdigest(),
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )
    item = CaptureWorkItem.create(
        envelope=envelope,
        available_at=FixedClock().now(),
    )
    FilesystemCaptureQueue(config.capture_root).enqueue(
        item,
        item_id=str(envelope.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )

    result = ConfiguredScheduledAdapters(
        config,
        FixedClock(),
        environment=_provider_environment(tmp_path),
        provider_service=_deterministic_provider_service(),
    ).dispatch_writer(get_writer_job_spec("JOB-010"))

    assert result.status is ScheduledDispatchStatus.COMPLETED
    assert tuple((config.capture_root / "active").glob("*.json")) == ()
    assert len(tuple((config.state_root / "distilled").glob("*.json"))) == 1


def test_composed_nightly_job_requires_private_provider_config(tmp_path: Path) -> None:
    result = ConfiguredScheduledAdapters(
        _filesystem_config(tmp_path),
        FixedClock(),
    ).dispatch_writer(get_writer_job_spec("JOB-010"))

    assert result.exit_code == 78


def test_composed_backup_requires_the_matching_canonical_writer_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    record_path = config.state_root / ".open-brain-host" / "writer-record.json"
    record_path.unlink()

    exit_code = main(
        ("backup", "run", "--profile=capture", "--json"),
        scheduled_adapters=ConfiguredScheduledAdapters(config, FixedClock()),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.CONFIGURATION
    assert output["error"]["code"] == "scheduled_application_configuration"


def test_composed_backup_rejects_a_different_canonical_writer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    write_canonical_writer_record(
        state_root=config.state_root,
        identity_id="different-writer",
        generation=2,
        recorded_at=datetime(2026, 8, 16, 20, 1, tzinfo=UTC),
    )

    exit_code = main(
        ("backup", "run", "--profile=capture", "--json"),
        scheduled_adapters=ConfiguredScheduledAdapters(config, FixedClock()),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.CONFIGURATION
    assert output["error"]["code"] == "scheduled_application_configuration"


def test_composed_backup_preserves_lock_contention_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)

    def lock_busy(**_: object) -> None:
        raise LockBusyError("synthetic contention")

    monkeypatch.setattr('open_brain_legacy.services.application.run_writer_job', lock_busy)
    exit_code = main(
        ("backup", "run", "--profile=capture", "--json"),
        scheduled_adapters=ConfiguredScheduledAdapters(config, FixedClock()),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.LOCK_HELD
    assert output["error"]["code"] == "scheduled_application_lock_held"


def test_backup_effect_holds_canonical_authority_for_the_complete_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _filesystem_config(tmp_path)

    def assert_authority_lock(*, lease: WriterLease, **_: object) -> None:
        with (
            lease.acquire(LockScope.BACKUP_PROFILE),
            pytest.raises(LockBusyError, match="already held"),
        ):
            write_canonical_writer_record(
                state_root=config.state_root,
                identity_id="different-writer",
                generation=2,
                recorded_at=datetime(2026, 8, 16, 20, 1, tzinfo=UTC),
            )

    monkeypatch.setattr(
        'open_brain_legacy.services.application.run_writer_job',
        assert_authority_lock,
    )

    exit_code = main(
        ("backup", "run", "--profile=capture", "--json"),
        scheduled_adapters=ConfiguredScheduledAdapters(config, FixedClock()),
    )

    assert exit_code is ExitCode.SUCCESS


def test_composed_scheduled_adapter_runs_index_and_replays_window(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    output_root = config.state_root / "index"
    output_root.mkdir()
    (config.work_root / "page.md").write_text("Synthetic work page.\n", encoding="utf-8")
    (config.saved_content_root / "capture.txt").write_text(
        "Synthetic approved capture.\n", encoding="utf-8"
    )
    (config.work_root / "punctuation.md").write_text("---\n", encoding="utf-8")
    (config.personal_root / "private.md").write_text(
        "Synthetic private content.\n", encoding="utf-8"
    )
    adapters = ConfiguredScheduledAdapters(config=config, clock=FixedClock())

    first = main(
        ("index", "--scope=all", "--json"),
        scheduled_adapters=adapters,
    )
    first_output = json.loads(capsys.readouterr().out)
    replay = main(
        ("index", "--scope=all", "--json"),
        scheduled_adapters=adapters,
    )
    replay_output = json.loads(capsys.readouterr().out)
    checked = check_index(
        target=get_job("JOB-016").deployment_target,
        roots=IndexRoots(
            pages_root=config.work_root,
            captures_root=config.saved_content_root,
            output_root=output_root,
        ),
    )

    assert first is ExitCode.SUCCESS
    assert replay is ExitCode.SUCCESS
    assert first_output == {"command": "JOB-016", "status": "completed"}
    assert replay_output == first_output
    assert checked.available is True
    assert checked.document_count == 3


def test_composed_index_requires_staged_output_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)

    exit_code = main(
        ("index", "--scope=all", "--json"),
        scheduled_adapters=ConfiguredScheduledAdapters(config, FixedClock()),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.CONFIGURATION
    assert output["error"]["code"] == "scheduled_application_configuration"


def test_composed_optional_life_os_jobs_persist_and_reset_the_current_plan(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    candidate_id, review_id = _create_applied_action_review(config)
    adapters = ConfiguredScheduledAdapters(
        config,
        FixedClock(),
        environment=_optional_automation_environment(tmp_path),
    )

    midday = adapters.dispatch_optional(get_job("JOB-017"))
    planned = adapters.dispatch_optional(get_job("JOB-018"))
    reset = adapters.dispatch_optional(get_job("JOB-019"))
    runtime = LifeOSPlanningRuntime.bind(root=config.state_root)

    assert (midday.exit_code, planned.exit_code, reset.exit_code) == (0, 0, 0)
    midday_request = runtime.load(
        operation=LifeOSRuntimeOperation.MIDDAY,
        plan_date=FixedClock().now().date(),
    )
    assert isinstance(midday_request, LifePlanRequest)
    assert tuple(
        (candidate.candidate_id, candidate.review_id)
        for candidate in midday_request.action_candidates
    ) == ((candidate_id, review_id),)
    assert runtime.store.get("life_plan_2026-08-16") is None


def test_composed_optional_automation_jobs_require_owner_only_configs(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    adapters = ConfiguredScheduledAdapters(config, FixedClock())

    assert adapters.dispatch_optional(get_job("JOB-004")).exit_code == 1
    assert adapters.dispatch_optional(get_job("JOB-017")).exit_code == 78
    assert adapters.dispatch_optional(get_job("JOB-020")).exit_code == 78
    assert adapters.dispatch_optional(get_job("JOB-024")).exit_code == 78


def test_composed_sqlite_backup_and_retention_are_real_read_only_operations(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    database_root = config.state_root / "events"
    database_root.mkdir()
    database = database_root / "events.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events(value TEXT NOT NULL)")
        connection.execute("INSERT INTO events(value) VALUES ('synthetic')")
    environment = _retention_environment(tmp_path, config)
    adapters = ConfiguredScheduledAdapters(
        config,
        FixedClock(),
        environment=environment,
    )
    source_bytes = database.read_bytes()

    sqlite_result = adapters.dispatch_optional(get_job("JOB-004"))
    retention_result = adapters.dispatch_optional(get_job("JOB-024"))

    assert (sqlite_result.exit_code, retention_result.exit_code) == (0, 0)
    assert database.read_bytes() == source_bytes
    assert not (config.backup_root / "backups").exists()
    assert (config.backup_root / "recovery-baseline.json").exists()


def test_composed_interactive_retention_uses_private_manifest_and_blocks_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    environment = _retention_environment(tmp_path, config)
    adapters = build_command_adapters(config, environment=environment)

    dry_run = main(
        ("retention", "--dry-run", "--json"),
        command_adapters=adapters,
    )
    dry_run_output = json.loads(capsys.readouterr().out)
    apply = main(
        ("retention", "--apply", "--json"),
        command_adapters=adapters,
    )
    apply_output = json.loads(capsys.readouterr().out)

    assert dry_run is ExitCode.SUCCESS
    assert dry_run_output["candidate_count"] == 1
    assert dry_run_output["protected_count"] == 1
    assert dry_run_output["removed_count"] == 0
    assert apply is ExitCode.FAILURE
    assert apply_output["status"] == "failed"
    assert (config.backup_root / "recovery-baseline.json").exists()


def test_composed_message_extract_writes_review_and_advances_cursor(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    resource_ref = "messages_configured"
    inbox = SqliteMessageInbox(root=config.state_root, clock=FixedClock())
    inbox.enqueue(
        MessageBatch(
            resource_ref=resource_ref,
            cursor_ref=None,
            next_cursor_ref="cursor_002",
            candidates=(
                MessageCandidate(
                    message_ref="message_001",
                    content_ref="content_001",
                    confidence=MessageConfidence.HIGH,
                ),
            ),
        )
    )

    result = ConfiguredScheduledAdapters(
        config,
        FixedClock(),
        environment=_optional_automation_environment(
            tmp_path,
            message_resource_ref=resource_ref,
        ),
    ).dispatch_optional(get_job("JOB-020"))
    state = PersistentMessagingCursorStore(root=config.state_root, clock=FixedClock())
    with SqliteReviewStore(
        root=config.state_root,
        database_name="review/review.sqlite3",
        clock=FixedClock(),
    ) as reviews:
        capture_id = capture_id_for(
            {
                "identity_version": 1,
                "source": "messaging",
                "content_ref": "content_001",
            }
        )
        queued = reviews.get(review_id_for(capture_id, Intent.ACTION_CANDIDATE.value))

    assert result.exit_code == 0
    assert state.current_cursor(resource_ref) == "cursor_002"
    assert queued is not None
    assert queued.proposal.source_ref == "content_001"


def test_composed_message_sync_dry_run_does_not_write_review_or_cursor(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    resource_ref = "messages_configured"
    SqliteMessageInbox(root=config.state_root, clock=FixedClock()).enqueue(
        MessageBatch(
            resource_ref=resource_ref,
            cursor_ref=None,
            next_cursor_ref="cursor_002",
            candidates=(
                MessageCandidate(
                    message_ref="message_001",
                    content_ref="content_001",
                    confidence=MessageConfidence.HIGH,
                ),
            ),
        )
    )

    result = ConfiguredScheduledAdapters(
        config,
        FixedClock(),
        environment=_optional_automation_environment(
            tmp_path,
            message_resource_ref=resource_ref,
        ),
    ).dispatch_optional(get_job("JOB-021"))
    state = PersistentMessagingCursorStore(root=config.state_root, clock=FixedClock())

    assert result.exit_code == 0
    assert state.current_cursor(resource_ref) is None
    assert not (config.state_root / "review" / "review.sqlite3").exists()


def test_composed_scheduled_adapter_builds_work_only_now_projection(
    tmp_path: Path,
) -> None:
    config = _filesystem_config(tmp_path)
    pages = config.work_root / "pages"
    pages.mkdir()
    (pages / "focus.md").write_text("# Synthetic focus\n", encoding="utf-8")

    exit_code = main(
        ("now", "build", "--role=writer", "--json"),
        scheduled_adapters=ConfiguredScheduledAdapters(config, FixedClock()),
    )

    assert exit_code is ExitCode.SUCCESS
    payload = (config.work_root / "NOW.md").read_text(encoding="utf-8")
    assert payload.startswith("# NOW\n\n<!-- open-brain-now-generation:now_")
    assert "Synthetic focus" in payload


def test_composition_root_wires_probe_backed_doctor_without_production_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapters = build_command_adapters(_synthetic_config())

    exit_code = main(
        ("doctor", "--json", "--role=writer"),
        command_adapters=adapters,
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.FAILURE
    assert output["command"] == "doctor"
    assert output["checks"]
    assert "cutover_ready" not in output


def test_composed_doctor_has_a_fully_healthy_concrete_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    event_root = config.state_root / "events"
    review_root = config.state_root / "review"
    event_root.mkdir()
    review_root.mkdir()
    connection = connect_database(root=event_root, database_name="events.sqlite3")
    try:
        migrate(connection, clock=FixedClock())
    finally:
        connection.close()
    with SqliteReviewStore(
        root=review_root,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ):
        pass
    source = FilesystemBackupSource(
        work_root=config.work_root,
        personal_root=config.personal_root,
        capture_root=config.capture_root,
        saved_content_root=config.saved_content_root,
        state_root=config.state_root,
    )
    store = FilesystemBackupStore(root=config.backup_root)
    for job_id in ("JOB-011", "JOB-014", "JOB-023", "JOB-025"):
        get_backup_job(job_id).run(
            source=source,
            store=store,
            created_at=FixedClock().now(),
            generation="runtime-2026-08-16" if job_id == "JOB-025" else None,
        )
    monkeypatch.setattr(
        'open_brain_legacy.services.application.inspect_published_references',
        lambda **_: PublishedReferenceInspection(0, 0),
    )
    monkeypatch.setattr(
        'open_brain_legacy.services.application._utc_now',
        lambda: FixedClock().now(),
    )

    exit_code = main(
        ("doctor", "--json", "--role=writer"),
        command_adapters=build_command_adapters(config),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.SUCCESS
    assert output["status"] == "healthy"
    assert all(check["state"] == "healthy" for check in output["checks"])


def test_invalid_configuration_still_routes_doctor_to_visible_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(("doctor", "--json", "--role=writer"), environment={})
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.FAILURE
    assert output["command"] == "doctor"
    configuration = next(check for check in output["checks"] if check["probe"] == "configuration")
    assert configuration["state"] == "unhealthy"
    assert configuration["finding_class"] == "configuration-invalid"


def test_composed_doctor_rejects_a_different_canonical_writer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _filesystem_config(tmp_path)
    write_canonical_writer_record(
        state_root=config.state_root,
        identity_id="different-writer",
        generation=2,
        recorded_at=datetime(2026, 8, 16, 20, 1, tzinfo=UTC),
    )

    main(
        ("doctor", "--json", "--role=writer"),
        command_adapters=build_command_adapters(config),
    )
    output = json.loads(capsys.readouterr().out)
    writer = next(check for check in output["checks"] if check["probe"] == "writer-ownership")

    assert writer["state"] == "unhealthy"
    assert writer["finding_class"] == "writer-ownership-conflict"


def test_composed_schema_probe_reads_both_databases_without_migrating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    event_root = state_root / "events"
    review_root = state_root / "review"
    event_root.mkdir(parents=True)
    review_root.mkdir()
    connection = connect_database(root=event_root, database_name="events.sqlite3")
    try:
        migrate(connection, clock=FixedClock())
    finally:
        connection.close()
    with SqliteReviewStore(
        root=review_root,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ):
        pass
    config = AppConfig(
        roots=RetainedRoots(
            work=tmp_path / "work",
            personal=tmp_path / "personal",
            capture=tmp_path / "capture",
            saved_content=tmp_path / "saved-content",
            state=state_root,
        ),
        backup=tmp_path / "backup",
        host_identity="synthetic-host",
    )

    main(
        ("doctor", "--json", "--role=writer"),
        command_adapters=build_command_adapters(config),
    )
    output = json.loads(capsys.readouterr().out)
    schema = next(check for check in output["checks"] if check["probe"] == "schema")

    assert schema["state"] == "healthy"
    assert schema["finding_class"] is None


def test_composition_binds_stale_reference_reader_to_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import open_brain_legacy.services.application as composition_module

    monkeypatch.setattr(
        composition_module,
        "inspect_published_references",
        lambda **_: PublishedReferenceInspection(4, 1),
    )

    main(
        ("doctor", "--json", "--role=writer"),
        command_adapters=build_command_adapters(_synthetic_config()),
    )
    output = json.loads(capsys.readouterr().out)
    stale = next(check for check in output["checks"] if check["probe"] == "stale-references")

    assert stale["state"] == "unhealthy"
    assert stale["count"] == 1
