"""Installed-artifact-owned native observations for the Phase 7 differential."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from io import StringIO
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import NoReturn, Protocol, cast, runtime_checkable

from open_brain_engine.capture.models import (
    CaptureWorkItem,
    DistillationWorkItem,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.capture.redaction import VersionedCaptureRedactor
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import (
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    PrivacyTier,
    Provenance,
    RawCapture,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import (
    EventRecord,
    PutDisposition,
    PutResult,
    RedactionReceipt,
)
from open_brain_engine.review.models import ActorKind, ReviewProposal, ReviewState
from open_brain_engine.storage.filesystem import AtomicFilesystemRawStore, raw_relative_path
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
)

from open_brain_legacy.capture.queue import FilesystemCaptureQueue
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.doctor import DoctorCliResult, show_doctor
from open_brain_legacy.cli.main import main as open_brain_cli
from open_brain_legacy.cli.operations import OperationsCliResult, show_cron
from open_brain_legacy.cli.status import StatusCliResult, show_status
from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.models import LedgerRoute, LedgerTaxonomy
from open_brain_legacy.ledger.sanitize import LedgerSection, sanitize_leaf
from open_brain_legacy.ledger.scan import scan_distillation_work_item
from open_brain_legacy.ledger.service import (
    ApplyResult,
    CaptureCitationResolver,
    LedgerService,
    PreparedLedgerApply,
)
from open_brain_legacy.ledger.stage import LedgerStage, stage_scan_record
from open_brain_legacy.ledger.store import SqliteLedgerStore
from open_brain_legacy.operations.doctor import (
    DoctorCheck,
    DoctorOutcome,
    DoctorResult,
    DoctorRole,
    FindingClass,
    ProbeName,
    ProbeReading,
    ProbeState,
    run_doctor,
)
from open_brain_legacy.operations.runlog import RunMetadata
from open_brain_legacy.operations.status import StatusMetric, StatusReading, collect_status
from open_brain_legacy.review.routing import (
    IntentRoutingDestination,
    IntentRoutingResult,
    IntentRoutingStatus,
    Phase4IntentRouter,
)
from open_brain_legacy.review.service import OwnerAuthoredOutput, ReviewApplicationService
from open_brain_legacy.review.store import SqliteReviewStore

_CAPTURED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
_SCENARIO_FIELDS = frozenset(
    {
        "name",
        "source_type",
        "content_kind",
        "source_url",
        "title",
        "shared_text",
        "capture_why",
        "capture_why_origin",
        "capture_source",
        "content_origin",
        "privacy_tier",
        "proposed_intent",
        "proposal_reason",
        "expected_intent",
        "expected_review_proposal",
    }
)


class ObservationValidationError(ValueError):
    """A native result cannot be represented by the closed observation contract."""


class OpenBrainCliProfile(StrEnum):
    STATUS = "status"
    CRON = "cron"


OPEN_BRAIN_CLI_PROFILE_FIELDS: Mapping[OpenBrainCliProfile, tuple[str, ...]] = (
    MappingProxyType(
        {
            OpenBrainCliProfile.STATUS: (
                "command",
                "metrics",
                "schema_version",
                "status",
                "strict",
            ),
            OpenBrainCliProfile.CRON: (
                "command",
                "run_count",
                "runs",
                "status",
                "window_seconds",
            ),
        }
    )
)


@dataclass(frozen=True, slots=True)
class CliFieldDigestObservation:
    field: str
    digest_sha256: str


@dataclass(frozen=True, slots=True)
class CliObservation:
    profile: OpenBrainCliProfile
    command: str
    status: str
    exit_code: int
    field_digests: tuple[CliFieldDigestObservation, ...]
    redacted: bool


@dataclass(frozen=True, slots=True)
class DoctorCheckObservation:
    probe: ProbeName
    state: ProbeState
    finding_class: FindingClass | None


@dataclass(frozen=True, slots=True)
class DoctorFindingObservation:
    probe: ProbeName
    finding_class: FindingClass
    state: ProbeState


@dataclass(frozen=True, slots=True)
class DoctorObservation:
    outcome: DoctorOutcome
    checks: tuple[DoctorCheckObservation, ...]
    findings: tuple[DoctorFindingObservation, ...]


@dataclass(frozen=True, slots=True)
class RequestObservation:
    request_status: None
    status_unavailable_reason: str
    native_lifecycle_state: str
    request_id: str
    content_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawFileSetObservation:
    file_digests_sha256: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueueTransitionObservation:
    from_state: str
    to_state: str
    attempt_count: int
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class QueueObservation:
    transitions: tuple[QueueTransitionObservation, ...]


@dataclass(frozen=True, slots=True)
class ProvenanceObservation:
    schema_version: int
    content_kind: ContentKind
    privacy_tier: PrivacyTier
    source_kind: SourceType
    source_ref_digest_sha256: str
    content_origin: ContentOrigin
    owner_context: CaptureWhyOrigin
    redaction_policy_version: int


@dataclass(frozen=True, slots=True)
class RoutingObservation:
    destination: IntentRoutingDestination


@dataclass(frozen=True, slots=True)
class LedgerCitationObservation:
    ledger_item_ids: tuple[str, ...]
    citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewProposalObservation:
    schema_version: int
    review_id: str
    capture_id: str
    source_ref_digest_sha256: str
    privacy_tier: PrivacyTier
    proposed_intent: Intent
    proposal_reason_digest_sha256: str
    capture_why_digest_sha256: str
    state: ReviewState
    created_at: str
    actor_kind: ActorKind
    actor_label_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ReviewProposalsObservation:
    proposals: tuple[ReviewProposalObservation, ...]


@dataclass(frozen=True, slots=True)
class Phase7Observation:
    request: RequestObservation
    raw_files: RawFileSetObservation
    queue: QueueObservation
    provenance: ProvenanceObservation
    routing: RoutingObservation
    ledger: LedgerCitationObservation
    reviews: ReviewProposalsObservation
    cli: CliObservation
    doctor: DoctorObservation


@dataclass(frozen=True, slots=True)
class SyntheticPhase7Scenario:
    name: str
    source_type: str
    content_kind: str
    source_url: str
    title: str
    shared_text: str
    capture_why: str
    capture_why_origin: str
    capture_source: str
    content_origin: str
    privacy_tier: str
    proposed_intent: str
    proposal_reason: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SyntheticPhase7Scenario:
        if type(value) is not dict or set(value) != _SCENARIO_FIELDS:
            raise ObservationValidationError("invalid synthetic observation scenario")
        expected_review = value["expected_review_proposal"]
        if type(expected_review) is not bool:
            raise ObservationValidationError("invalid synthetic observation scenario")
        text_fields = _SCENARIO_FIELDS - {"expected_review_proposal"}
        if any(type(value[field]) is not str for field in text_fields):
            raise ObservationValidationError("invalid synthetic observation scenario")
        return cls(
            name=cast(str, value["name"]),
            source_type=cast(str, value["source_type"]),
            content_kind=cast(str, value["content_kind"]),
            source_url=cast(str, value["source_url"]),
            title=cast(str, value["title"]),
            shared_text=cast(str, value["shared_text"]),
            capture_why=cast(str, value["capture_why"]),
            capture_why_origin=cast(str, value["capture_why_origin"]),
            capture_source=cast(str, value["capture_source"]),
            content_origin=cast(str, value["content_origin"]),
            privacy_tier=cast(str, value["privacy_tier"]),
            proposed_intent=cast(str, value["proposed_intent"]),
            proposal_reason=cast(str, value["proposal_reason"]),
        )


@runtime_checkable
class Phase7ObservationPort(Protocol):
    def observe(
        self, *, scenario: SyntheticPhase7Scenario, execution_root: Path
    ) -> Phase7Observation: ...


def digest_cli_profile_fields(
    *, profile: OpenBrainCliProfile, envelope: Mapping[str, object]
) -> tuple[tuple[str, str], ...]:
    if not isinstance(profile, OpenBrainCliProfile) or type(envelope) is not dict:
        raise ObservationValidationError("invalid CLI profile fields")
    fields = OPEN_BRAIN_CLI_PROFILE_FIELDS[profile]
    if set(envelope) != set(fields):
        raise ObservationValidationError("invalid CLI profile fields")
    try:
        return tuple(
            (field, sha256(canonical_json_bytes(envelope[field])).hexdigest())
            for field in fields
        )
    except (TypeError, ValueError):
        raise ObservationValidationError("invalid CLI profile value") from None


def observe_open_brain_cli(
    *,
    profile: OpenBrainCliProfile,
    stdout: str,
    exit_code: int,
    sensitive_values: tuple[str, ...] = (),
) -> CliObservation:
    if type(stdout) is not str:
        raise ObservationValidationError("invalid CLI output")
    try:
        decoded = json.loads(
            stdout,
            object_pairs_hook=_no_duplicate_json_object,
            parse_constant=_reject_json_constant,
        )
    except ObservationValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ObservationValidationError("invalid CLI output") from None
    if type(decoded) is not dict:
        raise ObservationValidationError("invalid CLI output")
    envelope = cast(dict[str, object], decoded)
    field_digests = digest_cli_profile_fields(profile=profile, envelope=envelope)
    _validate_cli_profile_values(profile=profile, envelope=envelope, exit_code=exit_code)
    try:
        rendered = canonical_json_bytes(envelope).decode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ObservationValidationError("invalid CLI profile value") from None
    if any(value and value in rendered for value in sensitive_values):
        raise ObservationValidationError("CLI output is not redacted")
    return CliObservation(
        profile=profile,
        command=cast(str, envelope["command"]),
        status=cast(str, envelope["status"]),
        exit_code=exit_code,
        field_digests=tuple(
            CliFieldDigestObservation(field=field, digest_sha256=digest)
            for field, digest in field_digests
        ),
        redacted=True,
    )


def observe_open_brain_doctor(
    *, result: DoctorResult, cli_result: DoctorCliResult
) -> DoctorObservation:
    if not isinstance(result, DoctorResult) or not isinstance(cli_result, DoctorCliResult):
        raise ObservationValidationError("invalid doctor observation")
    if (
        len(result.checks) != len(ProbeName)
        or tuple(check.probe for check in result.checks) != tuple(ProbeName)
        or any(not isinstance(check, DoctorCheck) for check in result.checks)
    ):
        raise ObservationValidationError("incomplete doctor check inventory")
    expected_findings = tuple(
        check for check in result.checks if check.state is not ProbeState.HEALTHY
    )
    if (
        result.findings != expected_findings
        or any(finding.finding_class is None for finding in result.findings)
        or len({finding.probe for finding in result.findings}) != len(result.findings)
    ):
        raise ObservationValidationError("invalid doctor finding inventory")
    expected_cli = show_doctor(result=result)
    if (
        cli_result.exit_code != result.exit_code
        or cli_result.exit_code != expected_cli.exit_code
        or cli_result.envelope != expected_cli.envelope
    ):
        raise ObservationValidationError("doctor CLI envelope mismatch")
    return DoctorObservation(
        outcome=result.outcome,
        checks=tuple(
            DoctorCheckObservation(
                probe=check.probe,
                state=check.state,
                finding_class=check.finding_class,
            )
            for check in result.checks
        ),
        findings=tuple(
            DoctorFindingObservation(
                probe=finding.probe,
                finding_class=cast(FindingClass, finding.finding_class),
                state=finding.state,
            )
            for finding in result.findings
        ),
    )


def observe_routing_result(result: IntentRoutingResult) -> RoutingObservation:
    if not isinstance(result, IntentRoutingResult):
        raise ObservationValidationError("invalid routing observation")
    valid = (
        result.status is IntentRoutingStatus.HELD
        and result.intent is Intent.HOLD
        and result.destination is IntentRoutingDestination.HOLD
        and result.review_id is None
    ) or (
        result.status is IntentRoutingStatus.REFERENCE_APPLIED
        and result.intent is Intent.REFERENCE
        and result.destination
        in {IntentRoutingDestination.WORK, IntentRoutingDestination.PERSONAL}
        and result.review_id is None
    ) or (
        result.status is IntentRoutingStatus.REVIEW_OPEN
        and result.intent in {Intent.IDEA, Intent.ACTION_CANDIDATE}
        and result.destination is IntentRoutingDestination.REVIEW
        and result.review_id is not None
    )
    if not valid:
        raise ObservationValidationError("invalid routing observation")
    return RoutingObservation(destination=result.destination)


class NativePhase7ObservationPort:
    """Invoke native Open Brain synthetic seams and return only redacted observations."""

    def observe(
        self, *, scenario: SyntheticPhase7Scenario, execution_root: Path
    ) -> Phase7Observation:
        if (
            not isinstance(scenario, SyntheticPhase7Scenario)
            or not isinstance(execution_root, Path)
            or not execution_root.is_absolute()
        ):
            raise ObservationValidationError("invalid native observation input")
        execution_root.mkdir(parents=True, exist_ok=True)
        capture = _capture(scenario)
        request, raw_files, queue, observed_capture = _capture_lifecycle(
            capture, execution_root / "capture"
        )
        routing_result, reviews = _route_and_observe_reviews(
            scenario=scenario,
            capture=observed_capture,
            root=execution_root / "routing",
        )
        ledger = _observe_unavailable_ledger()
        cli = _invoke_cli(
            profile=_scenario_cli_profile(scenario),
            sensitive_values=(
                scenario.source_url,
                scenario.title,
                scenario.shared_text,
                scenario.capture_why,
                scenario.proposal_reason,
            ),
        )
        doctor = _invoke_doctor(scenario)
        return Phase7Observation(
            request=request,
            raw_files=raw_files,
            queue=queue,
            provenance=_provenance(observed_capture),
            routing=observe_routing_result(routing_result),
            ledger=ledger,
            reviews=reviews,
            cli=cli,
            doctor=doctor,
        )


class _FixedClock:
    def now(self) -> datetime:
        return _CAPTURED_AT


class _UnusedOutputSink:
    def write_if_absent(self, _output: OwnerAuthoredOutput) -> PutResult:
        raise RuntimeError("review output delivery is outside the synthetic observation")


class _AppliedLedgerObservationBoundary:
    def __init__(self) -> None:
        self.applied: tuple[LedgerStage, PreparedLedgerApply] | None = None

    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult:
        stage.validate()
        prepared.validate_for(stage)
        self.applied = (stage, prepared)
        return ApplyResult(status="applied")


class _SyntheticStatusAdapter:
    def dispatch(self, argv: tuple[str, ...]) -> StatusCliResult:
        if argv:
            raise ObservationValidationError("unexpected status CLI arguments")
        probes = {
            metric: lambda _timeout: StatusReading.available(value=0)
            for metric in StatusMetric
        }
        return show_status(
            result=collect_status(probes=probes, timeout_seconds=1.0, strict=False)
        )


class _SyntheticCronReader:
    def reports(self, *, window_seconds: int) -> tuple[RunMetadata, ...]:
        if window_seconds != 86_400:
            raise ObservationValidationError("unexpected cron window")
        return ()


class _SyntheticCronAdapter:
    def dispatch(self, argv: tuple[str, ...]) -> OperationsCliResult:
        if argv:
            raise ObservationValidationError("unexpected cron CLI arguments")
        return show_cron(reader=_SyntheticCronReader())


def _capture(scenario: SyntheticPhase7Scenario) -> CaptureEnvelope:
    try:
        why_origin = CaptureWhyOrigin(scenario.capture_why_origin)
        capture = CaptureEnvelope.create(
            source_type=SourceType(scenario.source_type),
            content_kind=ContentKind(scenario.content_kind),
            source_url=scenario.source_url,
            title=scenario.title,
            shared_text=scenario.shared_text,
            captured_at=_CAPTURED_AT,
            capture_why=scenario.capture_why,
            capture_why_origin=why_origin,
            capture_source=CaptureSource(scenario.capture_source),
            provenance=Provenance.create(
                source_ref=scenario.source_url,
                content_origin=ContentOrigin(scenario.content_origin),
                owner_context=why_origin,
            ),
            raw_assets=(),
            privacy_decision=classify_privacy(
                PrivacyTier(scenario.privacy_tier), policy_version="phase7-observation-v1"
            ),
        )
    except ValueError as error:
        raise ObservationValidationError("invalid native capture observation") from error
    return CaptureEnvelope.from_canonical_bytes(capture.canonical_bytes())


def _capture_lifecycle(
    capture: CaptureEnvelope, root: Path
) -> tuple[
    RequestObservation,
    RawFileSetObservation,
    QueueObservation,
    CaptureEnvelope,
]:
    queue_root = root / "queue"
    queue = FilesystemCaptureQueue(queue_root)
    item = CaptureWorkItem.create(envelope=capture, available_at=capture.captured_at)
    queued = queue.enqueue(
        item,
        item_id=str(capture.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )
    if queued.record_id != str(capture.capture_id):
        raise ObservationValidationError("queue receipt observation failed")
    pending = _queue_record(queue_root)
    lease = queue.claim(worker_id="phase7-observation", now=_CAPTURED_AT)
    if lease is None or lease.item_id != queued.record_id:
        raise ObservationValidationError("queue claim observation failed")
    processing = _queue_record(queue_root)

    raw_root = root / "raw"
    raw_root.mkdir(parents=True)
    raw_store = AtomicFilesystemRawStore(root=raw_root)
    raw_receipt = raw_store.put_if_absent(RawCapture.create(envelope=capture, assets=()))
    restored = raw_store.get(capture.capture_id)
    raw_path = raw_root / raw_relative_path(capture.capture_id)
    if (
        raw_receipt.disposition is not PutDisposition.CREATED
        or raw_receipt.record_id != str(capture.capture_id)
        or restored is None
        or restored.envelope != capture
        or not raw_path.is_file()
        or _digest(raw_path.read_bytes()) != raw_receipt.digest_sha256
    ):
        raise ObservationValidationError("raw persistence observation failed")

    queue.acknowledge(lease, completed_at=_CAPTURED_AT)
    if tuple((queue_root / "active").glob("*.json")):
        raise ObservationValidationError("queue acknowledgement observation failed")
    transitions = (
        _transition(pending, _string(processing["state"])),
        _transition(processing, "acknowledged"),
    )
    return (
        RequestObservation(
            request_status=None,
            status_unavailable_reason="PAR7-001 request-status semantics lack approval",
            native_lifecycle_state="acknowledged",
            request_id=_opaque("request", queued.record_id),
            content_ids=(_opaque("content", raw_receipt.record_id),),
        ),
        RawFileSetObservation((raw_receipt.digest_sha256,)),
        QueueObservation(transitions),
        restored.envelope,
    )


def _route_and_observe_reviews(
    *, scenario: SyntheticPhase7Scenario, capture: CaptureEnvelope, root: Path
) -> tuple[IntentRoutingResult, ReviewProposalsObservation]:
    root.mkdir(parents=True)
    ledger = _AppliedLedgerObservationBoundary()
    stage: LedgerStage | None = None
    prepared: PreparedLedgerApply | None = None
    if scenario.proposed_intent == Intent.REFERENCE.value:
        route_tier = (
            PrivacyTier.PERSONAL
            if scenario.privacy_tier == PrivacyTier.PERSONAL.value
            else PrivacyTier.WORK
        )
        stage, prepared = _prepare_reference(
            capture=capture, route_tier=route_tier, root=root / "ledger"
        )
    with SqliteReviewStore(
        root=root,
        database_name="reviews.sqlite3",
        clock=_FixedClock(),
    ) as store:
        reviews = ReviewApplicationService(
            store=store,
            output_sink=_UnusedOutputSink(),
            clock=_FixedClock(),
        )
        result = Phase4IntentRouter(
            ledger=ledger,
            reviews=reviews,
            clock=_FixedClock(),
        ).route(
            capture=capture,
            proposed_intent=scenario.proposed_intent,
            proposal_reason=scenario.proposal_reason,
            stage=stage,
            prepared=prepared,
        )
        if result.status is IntentRoutingStatus.REFERENCE_APPLIED:
            if ledger.applied != (stage, prepared):
                raise ObservationValidationError("reference apply observation failed")
        elif ledger.applied is not None:
            raise ObservationValidationError("unexpected reference apply observation")
        proposals: tuple[ReviewProposalObservation, ...] = ()
        if result.review_id is not None:
            aggregate = store.get(result.review_id)
            if aggregate is None:
                raise ObservationValidationError("review persistence observation failed")
            proposals = (_review_proposal_observation(aggregate.proposal),)
        return result, ReviewProposalsObservation(proposals)


def _prepare_reference(
    *, capture: CaptureEnvelope, route_tier: PrivacyTier, root: Path
) -> tuple[LedgerStage, PreparedLedgerApply]:
    route = LedgerRoute.create(
        path_prefix=("synthetic", "reference"),
        topic_id="reference",
        topic_label="Reference",
        privacy_tier=route_tier,
    )
    taxonomy = LedgerTaxonomy.create(version="phase7-observation-v1", routes=(route,))
    payload = {
        "text": "Synthetic extracted reference",
        "capture_why": capture.capture_why,
        "capture_source": capture.capture_source.value,
        "source_type": capture.source_type.value,
        "content_kind": capture.content_kind.value,
        "provenance": capture.provenance.to_dict(),
    }
    event = EventRecord.create(
        event_id="evt_" + str(capture.capture_id)[4:20],
        stream_id=capture.capture_id,
        event_type="capture.extracted",
        occurred_at=capture.captured_at,
        privacy_decision=capture.privacy_decision,
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="b" * 64,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version="phase7-observation-v1",
        ),
    )
    item = DistillationWorkItem.create(
        capture_id=capture.capture_id,
        event_id=event.event_id,
        redacted_event_digest_sha256=sha256(event.canonical_bytes()).hexdigest(),
    )
    record = scan_distillation_work_item(
        item=item,
        event=event,
        taxonomy=taxonomy,
        source_locator=PurePosixPath("synthetic/reference/note"),
    )
    stage = stage_scan_record(record=record, taxonomy=taxonomy)
    root.mkdir(parents=True)
    store = SqliteLedgerStore(root=root / "private-ledger")
    markdown_root = root / "markdown"
    markdown_root.mkdir(mode=0o700)
    citation_id = "cite-synthetic"
    service = LedgerService(
        store=store,
        citations=CaptureCitationResolver(
            citations={
                (str(stage.binding.capture_id), stage.binding.event_id): TrustedCitation.create(
                    citation_id=citation_id,
                    destination=markdown_relative_path(
                        f"capture_ref_{citation_id}"
                    ).as_posix(),
                )
            }
        ),
        sink=AtomicMarkdownSink(root=markdown_root),
        reader=AtomicMarkdownReader(root=markdown_root),
    )
    sanitized = sanitize_leaf(
        item_id="synthetic-reference",
        section=LedgerSection.SUMMARY,
        text="Synthetic cited knowledge",
    )
    if sanitized.leaf is None:
        raise ObservationValidationError("reference preparation observation failed")
    prepared = service.prepare(
        stage=stage,
        section=LedgerSection.SUMMARY,
        leaf=sanitized.leaf,
    )
    return stage, prepared


def _review_proposal_observation(proposal: ReviewProposal) -> ReviewProposalObservation:
    return ReviewProposalObservation(
        schema_version=proposal.schema_version,
        review_id=_opaque("review", str(proposal.review_id)),
        capture_id=_opaque("capture", str(proposal.capture_id)),
        source_ref_digest_sha256=_digest(proposal.source_ref),
        privacy_tier=proposal.privacy_tier,
        proposed_intent=proposal.proposed_intent,
        proposal_reason_digest_sha256=_digest(proposal.proposal_reason),
        capture_why_digest_sha256=_digest(proposal.capture_why),
        state=proposal.state,
        created_at=proposal.created_at.isoformat().replace("+00:00", "Z"),
        actor_kind=proposal.created_by.kind,
        actor_label_digest_sha256=_digest(proposal.created_by.label),
    )


def _invoke_cli(
    *, profile: OpenBrainCliProfile, sensitive_values: tuple[str, ...]
) -> CliObservation:
    if profile is OpenBrainCliProfile.STATUS:
        registry = CommandAdapterRegistry({"status": _SyntheticStatusAdapter()})
    else:
        registry = CommandAdapterRegistry({"cron": _SyntheticCronAdapter()})
    stream = StringIO()
    with redirect_stdout(stream):
        exit_code = int(
            open_brain_cli(
                ["--json", profile.value],
                command_adapters=registry,
            )
        )
    return observe_open_brain_cli(
        profile=profile,
        stdout=stream.getvalue(),
        exit_code=exit_code,
        sensitive_values=sensitive_values,
    )


def _invoke_doctor(scenario: SyntheticPhase7Scenario) -> DoctorObservation:
    probes = (
        {}
        if scenario.name == "youtube_playlist_hold"
        else {ProbeName.OPTIONAL_PROVIDER: lambda _timeout: ProbeReading.unhealthy()}
    )
    result = run_doctor(
        role=DoctorRole.PROBE,
        probes=probes,
        timeout_seconds=1.0,
        strict=True,
    )
    return observe_open_brain_doctor(result=result, cli_result=show_doctor(result=result))


def _observe_unavailable_ledger() -> LedgerCitationObservation:
    stream = StringIO()
    with redirect_stdout(stream):
        exit_code = int(open_brain_cli(["--json", "ledger"], command_adapters=None))
    try:
        envelope = json.loads(
            stream.getvalue(),
            object_pairs_hook=_no_duplicate_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ObservationValidationError("ledger availability observation failed") from None
    if (
        type(envelope) is not dict
        or exit_code != 1
        or envelope.get("command") != "ledger"
        or envelope.get("status") != "unavailable"
    ):
        raise ObservationValidationError("ledger availability observation failed")
    return LedgerCitationObservation(ledger_item_ids=(), citation_ids=())


def _scenario_cli_profile(scenario: SyntheticPhase7Scenario) -> OpenBrainCliProfile:
    if scenario.name == "youtube_playlist_hold":
        return OpenBrainCliProfile.CRON
    if scenario.name in {
        "social_reference",
        "saved_web_reference",
        "idea_candidate",
        "third_party_action_candidate",
    }:
        return OpenBrainCliProfile.STATUS
    raise ObservationValidationError("unsupported scenario CLI profile")


def _provenance(capture: CaptureEnvelope) -> ProvenanceObservation:
    policy_version = VersionedCaptureRedactor().redact(
        _normalized_extraction(capture), capture
    ).receipt.policy_version
    if policy_version != "open-brain-redaction-v1":
        raise ObservationValidationError("unsupported redaction policy observation")
    return ProvenanceObservation(
        schema_version=capture.schema_version,
        content_kind=capture.content_kind,
        privacy_tier=capture.privacy_decision.tier,
        source_kind=capture.source_type,
        source_ref_digest_sha256=_digest(capture.provenance.source_ref),
        content_origin=capture.provenance.content_origin,
        owner_context=capture.provenance.owner_context,
        redaction_policy_version=1,
    )


def _normalized_extraction(capture: CaptureEnvelope) -> NormalizedExtraction:
    extractor = {
        SourceType.TEXT: ExtractorKind.TEXT,
        SourceType.WEB: ExtractorKind.ARTICLE,
        SourceType.YOUTUBE: ExtractorKind.YOUTUBE,
        SourceType.SOCIAL: ExtractorKind.SOCIAL,
    }[capture.source_type]
    return NormalizedExtraction.create(
        extractor=extractor,
        state=ExtractionState.COMPLETE,
        source_type=capture.source_type,
        content_kind=capture.content_kind,
        metadata=ExtractionMetadata.create(
            title=capture.title,
            canonical_url=capture.source_url,
        ),
        text=capture.shared_text,
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=(),
        failure=None,
    )


def _queue_record(root: Path) -> dict[str, object]:
    records = sorted((root / "active").glob("*.json"))
    if len(records) != 1:
        raise ObservationValidationError("queue persistence observation failed")
    try:
        value = json.loads(records[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ObservationValidationError("queue persistence observation failed") from None
    if type(value) is not dict:
        raise ObservationValidationError("queue persistence observation failed")
    return cast(dict[str, object], value)


def _transition(
    before: Mapping[str, object], after_state: str
) -> QueueTransitionObservation:
    item = _mapping(before["item"])
    from_state = _string(before["state"])
    attempt_count = item["attempt_count"]
    last_error_code = item["last_error_code"]
    if (
        type(attempt_count) is not int
        or (last_error_code is not None and type(last_error_code) is not str)
        or from_state == after_state
    ):
        raise ObservationValidationError("queue transition observation failed")
    return QueueTransitionObservation(
        from_state=from_state,
        to_state=after_state,
        attempt_count=attempt_count,
        last_error_code=last_error_code,
    )


def _validate_cli_profile_values(
    *, profile: OpenBrainCliProfile, envelope: Mapping[str, object], exit_code: int
) -> None:
    if type(exit_code) is not int or exit_code != 0:
        raise ObservationValidationError("invalid CLI exit")
    if profile is OpenBrainCliProfile.STATUS:
        valid = (
            envelope["command"] == "status"
            and envelope["status"] == "complete"
            and envelope["schema_version"] == 1
            and type(envelope["strict"]) is bool
            and type(envelope["metrics"]) is list
        )
    else:
        run_count = envelope["run_count"]
        runs = envelope["runs"]
        valid = (
            envelope["command"] == "cron"
            and envelope["status"] == "reported"
            and envelope["window_seconds"] == 86_400
            and type(run_count) is int
            and run_count >= 0
            and type(runs) is list
            and len(runs) == run_count
        )
    if not valid:
        raise ObservationValidationError("invalid CLI profile value")


def _no_duplicate_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationValidationError("duplicate CLI JSON field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ObservationValidationError("invalid JSON value")


def _mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ObservationValidationError("invalid native observation mapping")
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    if type(value) is not str:
        raise ObservationValidationError("invalid native observation string")
    return value


def _digest(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return sha256(payload).hexdigest()


def _opaque(prefix: str, value: str) -> str:
    return f"{prefix}_{_digest(value)[:16]}"
