from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.models import Authority, PrivacyReason, PrivacyTier
from open_brain_engine.engine import (
    ContentOrigin,
    PrivacyDecision,
    Provenance,
    ProviderMode,
    ReferencePayload,
)

from open_brain_legacy._compat.open_brain.extensions.connectors import CONNECTOR_ENTRY_POINT_GROUP
from open_brain_legacy._compat.open_brain.services.connectors import (
    INTERNAL_CONNECTOR_ENTRY_POINT_GROUP,
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCapabilityPolicy,
    ConnectorCaptureIdentity,
    ConnectorConfigurationError,
    ConnectorDiscoveryError,
    ConnectorEntryPoint,
    ConnectorHost,
    ConnectorManifest,
    ConnectorMetadataLogger,
    ConnectorOutcome,
    ConnectorPayload,
    ConnectorProfile,
    ConnectorRegistry,
    ConnectorRunContext,
    ConnectorRunEvidence,
    ConnectorRunReceipt,
    InstalledConnectorEntryPointSource,
)
from open_brain_legacy.services.application import SingleUserLocalApplication


class _EntryPoint:
    def __init__(self, name: str, value: str, connector: object) -> None:
        self.name = name
        self.value = value
        self._connector = connector
        self.load_count = 0

    def load(self) -> object:
        self.load_count += 1
        return self._connector


class _Source:
    def __init__(self, *entries: ConnectorEntryPoint) -> None:
        self._entries = entries
        self.metadata_calls = 0

    def entry_points(self, *, group: str) -> tuple[ConnectorEntryPoint, ...]:
        assert group == INTERNAL_CONNECTOR_ENTRY_POINT_GROUP
        self.metadata_calls += 1
        return self._entries


class _InstalledSource(InstalledConnectorEntryPointSource):
    def __init__(self, *entries: ConnectorEntryPoint) -> None:
        self._entries = entries

    def entry_points(self, *, group: str) -> tuple[ConnectorEntryPoint, ...]:
        assert group == CONNECTOR_ENTRY_POINT_GROUP
        return self._entries


class _Connector:
    def __init__(self, manifest: ConnectorManifest) -> None:
        self.manifest = manifest

    def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
        context.metadata_logger.record("synthetic.connector.completed")
        return ConnectorRunReceipt(
            connector_name=self.manifest.name,
            outcome=ConnectorOutcome.COMPLETED,
            failure_code=None,
            discovered_count=0,
            fetched_count=context.budget.fetched_count,
            extracted_count=context.budget.extracted_count,
            submitted_count=context.budget.submitted_count,
            stubbed_count=0,
            created_count=0,
            duplicate_count=0,
            checkpoint_committed=False,
            metadata_count=context.metadata_logger.count,
        )


@dataclass(frozen=True, slots=True)
class _Capability:
    connector_name: str = "youtube"
    budget: ConnectorBudget | None = None
    checkpoint_committed: bool = False

    def bind_budget(self, budget: ConnectorBudget) -> _Capability:
        if self.budget is not None:
            raise ValueError("capability already bound")
        return _Capability(
            connector_name=self.connector_name,
            budget=budget,
            checkpoint_committed=self.checkpoint_committed,
        )

    def bind_run(
        self,
        budget: ConnectorBudget,
        evidence: ConnectorRunEvidence,
    ) -> _Capability:
        del evidence
        return self.bind_budget(budget)


def _manifest(
    *,
    name: str = "youtube",
    payloads: tuple[ConnectorPayload, ...] = (ConnectorPayload.REFERENCE_OR_FILE,),
    schedules: tuple[str, ...] = ("JOB-029",),
    secrets: tuple[str, ...] = (),
    action_authorities: tuple[str, ...] = (),
) -> ConnectorManifest:
    return ConnectorManifest(
        schema_version=1,
        name=name,
        version="1",
        payloads=payloads,
        schedules=schedules,
        secrets=secrets,
        action_authorities=action_authorities,
        external_egress=True,
    )


def _context_factory(
    application: SingleUserLocalApplication,
    calls: list[str],
) -> Callable[[ConnectorManifest, ConnectorBudget, ConnectorMetadataLogger], ConnectorRunContext]:
    def build(
        manifest: ConnectorManifest,
        budget: ConnectorBudget,
        logger: ConnectorMetadataLogger,
    ) -> ConnectorRunContext:
        calls.append(manifest.name)
        return ConnectorRunContext(
            capture_identity=ConnectorCaptureIdentity(
                "youtube",
                "JOB-029",
                application.public_job_context("JOB-029"),
            ),
            capture_sink=application.public_job_sink("JOB-029"),
            transport=_Capability(),
            checkpoint=_Capability(),
            clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
            budget=budget,
            metadata_logger=logger,
        )

    return build


def test_closed_manifest_requires_exact_ordered_schema() -> None:
    manifest = _manifest()

    assert ConnectorManifest.from_dict(manifest.to_dict()) == manifest
    with pytest.raises(ValueError, match="connector manifest"):
        ConnectorManifest.from_dict({**manifest.to_dict(), "unknown": True})
    with pytest.raises(ValueError, match="connector manifest"):
        ConnectorManifest(
            schema_version=1,
            name="youtube",
            version="1",
            payloads=(ConnectorPayload.REFERENCE_OR_FILE,),
            schedules=("JOB-029", "JOB-001"),
            secrets=(),
            action_authorities=(),
            external_egress=True,
        )


def test_discovery_enumerates_metadata_without_loading() -> None:
    entry = _EntryPoint("youtube", "synthetic.youtube:connector", _Connector(_manifest()))
    registry = ConnectorRegistry(_Source(entry))

    discovered = registry.discover(ConnectorProfile(allow_list=("youtube",), egress_enabled=True))

    assert tuple((item.name, item.value) for item in discovered) == (
        ("youtube", "synthetic.youtube:connector"),
    )
    assert entry.load_count == 0


@pytest.mark.parametrize(
    "entries, profile",
    [
        (
            (_EntryPoint("Youtube", "synthetic.youtube:connector", object()),),
            ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        ),
        (
            (
                _EntryPoint("youtube", "synthetic.youtube:first", object()),
                _EntryPoint("youtube", "synthetic.youtube:second", object()),
            ),
            ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        ),
    ],
)
def test_malformed_and_duplicate_discovery_fails_before_load(
    entries: tuple[_EntryPoint, ...], profile: ConnectorProfile
) -> None:
    registry = ConnectorRegistry(_Source(*entries))

    with pytest.raises(ConnectorDiscoveryError):
        registry.discover(profile)

    assert [entry.load_count for entry in entries] == [0] * len(entries)


def test_installed_but_unlisted_connector_is_hidden_without_loading() -> None:
    entry = _EntryPoint("youtube", "synthetic.youtube:connector", object())
    registry = ConnectorRegistry(_Source(entry))

    assert registry.discover(ConnectorProfile()) == ()
    assert entry.load_count == 0


def test_installed_registry_is_metadata_only_and_cannot_resolve() -> None:
    entry = _EntryPoint("youtube", "synthetic.youtube:connector", _Connector(_manifest()))
    registry = ConnectorRegistry(_InstalledSource(entry))
    profile = ConnectorProfile(allow_list=("youtube",), egress_enabled=True)

    assert tuple((item.name, item.value) for item in registry.discover(profile)) == (
        ("youtube", "synthetic.youtube:connector"),
    )
    with pytest.raises(ConnectorConfigurationError, match="requires isolated worker"):
        registry.resolve("youtube", profile)

    assert entry.load_count == 0


@pytest.mark.parametrize(
    "connector, policy",
    [
        (_Connector(_manifest(name="other")), ConnectorCapabilityPolicy()),
        (_Connector(_manifest(secrets=("connector_secret",))), ConnectorCapabilityPolicy()),
        (
            _Connector(_manifest(payloads=(ConnectorPayload.EVENT,))),
            ConnectorCapabilityPolicy(),
        ),
    ],
)
def test_name_mismatch_and_unsupported_capability_fail_before_context(
    tmp_path: Path,
    connector: _Connector,
    policy: ConnectorCapabilityPolicy,
) -> None:
    entry = _EntryPoint("youtube", "synthetic.youtube:connector", connector)
    host = ConnectorHost(ConnectorRegistry(_Source(entry), capability_policy=policy))
    context_calls: list[str] = []
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    receipt = host.run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=_context_factory(application, context_calls),
    )

    assert receipt.outcome is ConnectorOutcome.FAILED
    assert context_calls == []
    assert entry.load_count == 1


def test_egress_off_defers_without_metadata_loader_or_context_work(tmp_path: Path) -> None:
    entry = _EntryPoint("youtube", "synthetic.youtube:connector", _Connector(_manifest()))
    source = _Source(entry)
    context_calls: list[str] = []
    host = ConnectorHost(ConnectorRegistry(source))
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    receipt = host.run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=False),
        context_factory=_context_factory(application, context_calls),
    )

    assert receipt.outcome is ConnectorOutcome.DEFERRED
    assert receipt.fetched_count == receipt.extracted_count == receipt.submitted_count == 0
    assert source.metadata_calls == entry.load_count == 0
    assert context_calls == []


def test_allow_listed_absent_connector_fails_only_when_requested(tmp_path: Path) -> None:
    source = _Source()
    host = ConnectorHost(ConnectorRegistry(source))
    profile = ConnectorProfile(allow_list=("youtube",), egress_enabled=True)
    context_calls: list[str] = []
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    assert host.discover(profile) == ()
    receipt = host.run(
        "youtube",
        profile=profile,
        context_factory=_context_factory(application, context_calls),
    )

    assert receipt.outcome is ConnectorOutcome.FAILED
    assert receipt.failure_code is not None
    assert receipt.failure_code.value == "not_discovered"
    assert context_calls == []


def test_default_profile_discovers_nothing_and_context_has_capture_only_authority(
    tmp_path: Path,
) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    field_names = tuple(field.name for field in fields(ConnectorRunContext))
    receipt = ConnectorRunReceipt.deferred("youtube")
    rendered = repr(receipt)

    assert application.connector_profile.allow_list == ()
    assert application.discover_connectors() == ()
    assert application.tasks.profile.provider_mode is ProviderMode.NONE
    context = application.public_job_context("JOB-029")
    sink = application.public_job_sink("JOB-029")
    assert context.role_claim["capabilities"] == ("capture.accept",)
    assert sink.context == context
    assert context.actor_id != application.tasks.profile.owner_actor_id
    assert field_names == (
        "capture_identity",
        "capture_sink",
        "transport",
        "checkpoint",
        "clock",
        "budget",
        "metadata_logger",
    )
    assert not {"root", "database", "review", "publication", "action"} & set(field_names)
    assert "https://example.test/private" not in rendered
    assert sha256(b"https://example.test/private").hexdigest() not in rendered
    assert "open_brain.internal_connectors.v1" not in (
        Path(__file__).resolve().parents[3] / "pyproject.toml"
    ).read_text(encoding="utf-8")


def test_app_composition_uses_only_the_internal_extension_contract() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "src/open_brain_legacy/services/application.py"
    ).read_text(encoding="utf-8")

    assert "open_brain.production.youtube_poll" not in source
    assert "open_brain.capture.poll" not in source
    assert "open_brain_connectors" not in source
    assert "YouTubeReferenceConnector" not in source


def test_empty_receipt_is_closed_and_metadata_only() -> None:
    receipt = ConnectorRunReceipt.empty("youtube", metadata_count=1)

    assert receipt.outcome is ConnectorOutcome.EMPTY
    assert receipt.to_dict() == {
        "checkpoint_committed": False,
        "connector_name": "youtube",
        "created_count": 0,
        "discovered_count": 0,
        "duplicate_count": 0,
        "extracted_count": 0,
        "failure_code": None,
        "fetched_count": 0,
        "metadata_count": 1,
        "outcome": "empty",
        "stubbed_count": 0,
        "submitted_count": 0,
    }
    assert "https://" not in repr(receipt)


def test_host_rejects_unsupported_checkpoint_commit_claim(tmp_path: Path) -> None:
    class _FalseCheckpointClaim(_Connector):
        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            return ConnectorRunReceipt(
                connector_name=self.manifest.name,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=context.budget.discovered_count,
                fetched_count=context.budget.fetched_count,
                extracted_count=context.budget.extracted_count,
                submitted_count=context.budget.submitted_count,
                stubbed_count=0,
                created_count=0,
                duplicate_count=0,
                checkpoint_committed=True,
                metadata_count=context.metadata_logger.count,
            )

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    receipt = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _FalseCheckpointClaim(_manifest()),
                )
            )
        )
    ).run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=_context_factory(application, []),
    )

    assert receipt.outcome is ConnectorOutcome.FAILED
    assert receipt.failure_code is not None
    assert receipt.failure_code.value == "invalid_receipt"


def test_host_prevents_connector_from_mutating_its_budget_view(
    tmp_path: Path,
) -> None:
    class _OverBudgetConnector(_Connector):
        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            object.__setattr__(
                context.budget,
                "_fetched_count",
                context.budget.limits.max_fetches + 1,
            )
            return ConnectorRunReceipt(
                connector_name=self.manifest.name,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=0,
                fetched_count=context.budget.fetched_count,
                extracted_count=0,
                submitted_count=0,
                stubbed_count=0,
                created_count=0,
                duplicate_count=0,
                checkpoint_committed=False,
                metadata_count=context.metadata_logger.count,
            )

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    host = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _OverBudgetConnector(_manifest()),
                )
            )
        )
    )

    receipt = host.run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=_context_factory(application, []),
    )

    assert receipt.outcome is ConnectorOutcome.FAILED
    assert receipt.failure_code is not None
    assert receipt.failure_code.value == "invalid_receipt"


def test_host_capture_capability_enforces_submission_limit_on_connector(
    tmp_path: Path,
) -> None:
    privacy = PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=True),
    )

    class _RepeatedSubmitter(_Connector):
        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            payload = ReferencePayload(
                "https://example.test/synthetic",
                "Synthetic bounded connector submission.",
            )
            for index in range(3):
                with suppress(ValueError):
                    context.capture_sink.submit(
                        payload,
                        delivery_id=f"connector.youtube.synthetic-{index}",
                        source_origin=ContentOrigin.THIRD_PARTY,
                        source_reference=payload.url,
                        provenance=Provenance.create(
                            source_ref=payload.url,
                            content_origin=ContentOrigin.THIRD_PARTY,
                            owner_context="automation_absent",
                        ),
                        privacy=privacy,
                    )
            return ConnectorRunReceipt(
                connector_name=self.manifest.name,
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=context.budget.discovered_count,
                fetched_count=context.budget.fetched_count,
                extracted_count=context.budget.extracted_count,
                submitted_count=context.budget.submitted_count,
                stubbed_count=0,
                created_count=1,
                duplicate_count=0,
                checkpoint_committed=False,
                metadata_count=context.metadata_logger.count,
            )

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    host = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _RepeatedSubmitter(_manifest()),
                )
            )
        )
    )

    receipt = host.run(
        "youtube",
        profile=ConnectorProfile(
            allow_list=("youtube",),
            egress_enabled=True,
            budget_limits=ConnectorBudgetLimits(max_submissions=1),
        ),
        context_factory=_context_factory(application, []),
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert receipt.submitted_count == receipt.created_count == 1
    assert len(application.tasks.inbox.list()) == 1


def test_host_rejects_context_with_detached_capabilities(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    connector = _Connector(_manifest())
    host = ConnectorHost(
        ConnectorRegistry(
            _Source(_EntryPoint("youtube", "synthetic.youtube:connector", connector))
        )
    )

    def detached_context(
        manifest: ConnectorManifest,
        budget: ConnectorBudget,
        logger: ConnectorMetadataLogger,
    ) -> ConnectorRunContext:
        del manifest, logger
        return ConnectorRunContext(
            capture_identity=ConnectorCaptureIdentity(
                "youtube",
                "JOB-029",
                application.public_job_context("JOB-029"),
            ),
            capture_sink=application.public_job_sink("JOB-029"),
            transport=_Capability(),
            checkpoint=_Capability(),
            clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
            budget=ConnectorBudget(budget.limits),
            metadata_logger=ConnectorMetadataLogger(),
        )

    receipt = host.run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=detached_context,
    )

    assert receipt.outcome is ConnectorOutcome.FAILED
    assert receipt.failure_code is not None
    assert receipt.failure_code.value == "invalid_receipt"


def test_host_converts_context_and_connector_exceptions_to_bounded_failures(
    tmp_path: Path,
) -> None:
    class _FailingConnector(_Connector):
        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            del context
            raise RuntimeError("synthetic private detail")

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    connector = _FailingConnector(_manifest())
    host = ConnectorHost(
        ConnectorRegistry(
            _Source(_EntryPoint("youtube", "synthetic.youtube:connector", connector))
        )
    )
    profile = ConnectorProfile(allow_list=("youtube",), egress_enabled=True)

    context_failure = host.run(
        "youtube",
        profile=profile,
        context_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("private path")),
    )
    connector_failure = host.run(
        "youtube",
        profile=profile,
        context_factory=_context_factory(application, []),
    )

    assert context_failure.failure_code is not None
    assert context_failure.failure_code.value == "runtime_failed"
    assert connector_failure.failure_code is not None
    assert connector_failure.failure_code.value == "runtime_failed"
    assert "private" not in repr(context_failure) + repr(connector_failure)


def test_discovery_and_registration_descriptor_exceptions_are_bounded(
    tmp_path: Path,
) -> None:
    class _BrokenSource:
        def entry_points(self, *, group: str) -> tuple[ConnectorEntryPoint, ...]:
            del group
            raise RuntimeError("private-discovery-detail")

    class _BrokenRegistration:
        @property
        def manifest(self) -> ConnectorManifest:
            raise RuntimeError("private-manifest-detail")

        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            raise AssertionError(context)

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    profile = ConnectorProfile(allow_list=("youtube",), egress_enabled=True)
    discovery_failure = ConnectorHost(ConnectorRegistry(_BrokenSource())).run(
        "youtube",
        profile=profile,
        context_factory=_context_factory(application, []),
    )
    registration_failure = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _BrokenRegistration(),
                )
            )
        )
    ).run(
        "youtube",
        profile=profile,
        context_factory=_context_factory(application, []),
    )

    assert discovery_failure.failure_code is not None
    assert discovery_failure.failure_code.value == "invalid_registration"
    assert registration_failure.failure_code is not None
    assert registration_failure.failure_code.value == "invalid_registration"
    rendered = repr(discovery_failure) + repr(registration_failure)
    assert "private-discovery-detail" not in rendered
    assert "private-manifest-detail" not in rendered


def test_registry_captures_manifest_and_run_descriptors_exactly_once(tmp_path: Path) -> None:
    class _FlakyRegistration:
        def __init__(self) -> None:
            self.manifest_reads = 0
            self.run_reads = 0

        @property
        def manifest(self) -> ConnectorManifest:
            self.manifest_reads += 1
            if self.manifest_reads > 1:
                raise RuntimeError("private registration detail")
            return _manifest()

        @property
        def run(self) -> Callable[[ConnectorRunContext], ConnectorRunReceipt]:
            self.run_reads += 1
            if self.run_reads > 1:
                raise RuntimeError("private run detail")
            return self._run

        def _run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            context.metadata_logger.record("synthetic.connector.completed")
            return ConnectorRunReceipt(
                connector_name="youtube",
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=0,
                fetched_count=0,
                extracted_count=0,
                submitted_count=0,
                stubbed_count=0,
                created_count=0,
                duplicate_count=0,
                checkpoint_committed=False,
                metadata_count=context.metadata_logger.count,
            )

    registration = _FlakyRegistration()
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    receipt = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    registration,
                )
            )
        )
    ).run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=_context_factory(application, []),
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert registration.manifest_reads == 1
    assert registration.run_reads == 1


def test_host_rejects_hostile_contract_subclasses_before_field_reentry(
    tmp_path: Path,
) -> None:
    class _HostileManifest(ConnectorManifest):
        def __getattribute__(self, name: str) -> object:
            armed = object.__getattribute__(self, "__dict__").get("armed", False)
            if name == "schedules" and armed:
                raise RuntimeError("private-manifest-field-detail")
            return super().__getattribute__(name)

    class _HostileReceipt(ConnectorRunReceipt):
        def __getattribute__(self, name: str) -> object:
            armed = object.__getattribute__(self, "__dict__").get("armed", False)
            if name == "metadata_count" and armed:
                raise RuntimeError("private-receipt-field-detail")
            return super().__getattribute__(name)

    manifest = _HostileManifest(
        schema_version=1,
        name="youtube",
        version="1",
        payloads=(ConnectorPayload.REFERENCE_OR_FILE,),
        schedules=("JOB-029",),
        secrets=(),
        action_authorities=(),
        external_egress=True,
    )
    object.__setattr__(manifest, "armed", True)

    hostile_receipt = _HostileReceipt(
        connector_name="youtube",
        outcome=ConnectorOutcome.COMPLETED,
        failure_code=None,
        discovered_count=0,
        fetched_count=0,
        extracted_count=0,
        submitted_count=0,
        stubbed_count=0,
        created_count=0,
        duplicate_count=0,
        checkpoint_committed=False,
        metadata_count=0,
    )
    object.__setattr__(hostile_receipt, "armed", True)

    class _ManifestSubclassRegistration:
        def __init__(self, value: ConnectorManifest) -> None:
            self.manifest = value

        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            raise AssertionError(context)

    class _ReceiptSubclassRegistration(_Connector):
        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            del context
            return hostile_receipt

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    profile = ConnectorProfile(allow_list=("youtube",), egress_enabled=True)

    manifest_failure = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _ManifestSubclassRegistration(manifest),
                )
            )
        )
    ).run(
        "youtube",
        profile=profile,
        context_factory=_context_factory(application, []),
    )
    receipt_failure = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _ReceiptSubclassRegistration(_manifest()),
                )
            )
        )
    ).run(
        "youtube",
        profile=profile,
        context_factory=_context_factory(application, []),
    )

    assert manifest_failure.failure_code is not None
    assert manifest_failure.failure_code.value == "invalid_registration"
    assert receipt_failure.failure_code is not None
    assert receipt_failure.failure_code.value == "invalid_receipt"
    rendered = repr(manifest_failure) + repr(receipt_failure)
    assert "private-manifest-field-detail" not in rendered
    assert "private-receipt-field-detail" not in rendered


def test_host_bounds_post_construction_poisoning_of_exact_contract_values(
    tmp_path: Path,
) -> None:
    class _PrivateFailure:
        def __iter__(self) -> object:
            raise RuntimeError("private-mutated-field-detail")

        def __eq__(self, other: object) -> bool:
            del other
            raise RuntimeError("private-mutated-field-detail")

        def __len__(self) -> int:
            raise RuntimeError("private-mutated-field-detail")

        def __le__(self, other: object) -> bool:
            del other
            raise RuntimeError("private-mutated-field-detail")

    application = SingleUserLocalApplication.open(tmp_path / "brain")
    context_factory = _context_factory(application, [])

    poisoned_profile = ConnectorProfile(allow_list=("youtube",), egress_enabled=True)
    object.__setattr__(poisoned_profile, "allow_list", _PrivateFailure())
    profile_failure = ConnectorHost(ConnectorRegistry(_Source())).run(
        "youtube",
        profile=poisoned_profile,
        context_factory=context_factory,
    )

    poisoned_manifest = _manifest()
    object.__setattr__(poisoned_manifest, "schedules", _PrivateFailure())
    manifest_failure = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    _Connector(poisoned_manifest),
                )
            )
        )
    ).run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=context_factory,
    )

    normal_connector = _Connector(_manifest())
    normal_host = ConnectorHost(
        ConnectorRegistry(
            _Source(
                _EntryPoint(
                    "youtube",
                    "synthetic.youtube:connector",
                    normal_connector,
                )
            )
        )
    )

    def poisoned_context(
        manifest: ConnectorManifest,
        budget: ConnectorBudget,
        logger: ConnectorMetadataLogger,
    ) -> ConnectorRunContext:
        context = context_factory(manifest, budget, logger)
        object.__setattr__(
            context.capture_identity,
            "connector_name",
            _PrivateFailure(),
        )
        return context

    context_failure = normal_host.run(
        "youtube",
        profile=ConnectorProfile(allow_list=("youtube",), egress_enabled=True),
        context_factory=poisoned_context,
    )

    class _PoisoningConnector(_Connector):
        def __init__(self, target: str) -> None:
            super().__init__(_manifest())
            self.target = target

        def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
            receipt = ConnectorRunReceipt(
                connector_name="youtube",
                outcome=ConnectorOutcome.COMPLETED,
                failure_code=None,
                discovered_count=0,
                fetched_count=0,
                extracted_count=0,
                submitted_count=0,
                stubbed_count=0,
                created_count=0,
                duplicate_count=0,
                checkpoint_committed=False,
                metadata_count=0,
            )
            if self.target == "budget":
                object.__setattr__(
                    context.budget.limits,
                    "max_fetches",
                    _PrivateFailure(),
                )
            elif self.target == "logger":
                object.__setattr__(context.metadata_logger, "_events", _PrivateFailure())
            else:
                object.__setattr__(receipt, "metadata_count", _PrivateFailure())
            return receipt

    post_run_failures = []
    for target in ("budget", "logger", "receipt"):
        host = ConnectorHost(
            ConnectorRegistry(
                _Source(
                    _EntryPoint(
                        "youtube",
                        "synthetic.youtube:connector",
                        _PoisoningConnector(target),
                    )
                )
            )
        )
        post_run_failures.append(
            host.run(
                "youtube",
                profile=ConnectorProfile(
                    allow_list=("youtube",),
                    egress_enabled=True,
                ),
                context_factory=context_factory,
            )
        )

    failures = [
        profile_failure,
        manifest_failure,
        context_failure,
        *post_run_failures,
    ]
    assert [failure.failure_code.value for failure in failures if failure.failure_code] == [
        "not_allowed",
        "invalid_registration",
        "invalid_receipt",
        "invalid_receipt",
        "invalid_receipt",
        "invalid_receipt",
    ]
    assert "private-mutated-field-detail" not in "".join(map(repr, failures))
