from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from open_brain_engine.core.ids import ReviewId, canonical_json_bytes
from open_brain_engine.engine import LockScope
from open_brain_engine.review.models import ApprovedIntentRecord, ReviewAggregate, ReviewState

from .catalog import get_job
from .models import DeploymentTarget, HostRole


class WriterJobError(RuntimeError):
    """A scheduled application violated its local operations contract."""


class ScheduledEffect(StrEnum):
    OPERATOR_ARTIFACT = "operator-artifact"
    APPEND_ONLY_SIGNALS = "append-only-signals"
    DIAGNOSTICS = "diagnostics"
    HOOK_SYNC_PLAN = "hook-sync-plan"
    LEDGER_WRITE = "ledger-write"
    CURATION_PROMOTION = "curation-promotion"
    LOCAL_GIT_SYNC = "local-git-sync"
    BACKUP_SNAPSHOT = "backup-snapshot"
    INDEX_REBUILD = "index-rebuild"
    NOW_PROJECTION = "now-projection"


class ReviewBoundary(StrEnum):
    NONE = "none"
    PREPARATION_ONLY = "preparation-only"
    APPROVED_INPUTS_ONLY = "approved-inputs-only"


class JobRunDisposition(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class WriterJobSpec:
    job_id: str
    command: tuple[str, ...]
    deployment_target: DeploymentTarget
    host_role: HostRole
    lock_scope: LockScope
    effect: ScheduledEffect
    review_boundary: ReviewBoundary
    local_only: bool
    dry_run: bool
    planned_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    record_id: str
    review_id: str
    record_digest_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_id, str)
            or _OPAQUE_ID.fullmatch(self.record_id) is None
            or not isinstance(self.review_id, str)
            or _OPAQUE_ID.fullmatch(self.review_id) is None
            or not isinstance(self.record_digest_sha256, str)
            or _SHA256.fullmatch(self.record_digest_sha256) is None
        ):
            raise WriterJobError("invalid approved record binding")

    @classmethod
    def from_record(cls, record: ApprovedIntentRecord) -> ApprovalBinding:
        if not isinstance(record, ApprovedIntentRecord):
            raise WriterJobError("invalid approved intent record")
        return cls(
            record_id=record.record_id,
            review_id=str(record.review_id),
            record_digest_sha256=sha256(canonical_json_bytes(record.to_dict())).hexdigest(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "review_id": self.review_id,
            "record_digest_sha256": self.record_digest_sha256,
        }


@dataclass(frozen=True, slots=True)
class EffectRecord:
    record_id: str
    digest_sha256: str
    approval: ApprovalBinding | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_id, str)
            or _OPAQUE_ID.fullmatch(self.record_id) is None
            or not isinstance(self.digest_sha256, str)
            or _SHA256.fullmatch(self.digest_sha256) is None
            or self.approval is not None
            and not isinstance(self.approval, ApprovalBinding)
        ):
            raise WriterJobError("invalid scheduled effect record")

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "digest_sha256": self.digest_sha256,
            "approval": None if self.approval is None else self.approval.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EffectParameter:
    name: str
    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _PARAMETER_NAME.fullmatch(self.name) is None
            or not isinstance(self.value, str)
            or not self.value
            or len(self.value) > 256
            or any(ord(character) < 32 for character in self.value)
        ):
            raise WriterJobError("invalid scheduled effect parameter")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True, slots=True)
class PreparedEffect:
    effect: ScheduledEffect
    records: tuple[EffectRecord, ...] = ()
    review_item_ids: tuple[str, ...] = ()
    parameters: tuple[EffectParameter, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.effect, ScheduledEffect)
            or not isinstance(self.records, tuple)
            or any(not isinstance(record, EffectRecord) for record in self.records)
            or len({record.record_id for record in self.records}) != len(self.records)
            or not isinstance(self.review_item_ids, tuple)
            or len(set(self.review_item_ids)) != len(self.review_item_ids)
            or any(
                not isinstance(item_id, str) or _OPAQUE_ID.fullmatch(item_id) is None
                for item_id in self.review_item_ids
            )
            or not isinstance(self.parameters, tuple)
            or any(
                not isinstance(parameter, EffectParameter)
                for parameter in self.parameters
            )
            or [parameter.name for parameter in self.parameters]
            != sorted({parameter.name for parameter in self.parameters})
        ):
            raise WriterJobError("invalid prepared scheduled effect")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "effect": self.effect.value,
            "records": [record.to_dict() for record in self.records],
            "review_item_ids": list(self.review_item_ids),
        }
        if self.parameters:
            value["parameters"] = [parameter.to_dict() for parameter in self.parameters]
        return value

    def digest_sha256(self) -> str:
        return sha256(canonical_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class WriterJobInvocation:
    job_id: str
    command: tuple[str, ...]
    replay_key: str
    effect: ScheduledEffect
    review_boundary: ReviewBoundary
    local_only: bool
    dry_run: bool
    apply_review_decisions: bool
    approved_records: tuple[ApprovedIntentRecord, ...]
    approval_bindings: tuple[ApprovalBinding, ...]
    planned_actions: tuple[str, ...]
    personal_local_only: bool
    cutoff: datetime | None

    @property
    def root(self) -> Path:
        raise WriterJobError("scheduled application has no direct I/O capability")


@dataclass(frozen=True, slots=True)
class EffectCommand:
    job_id: str
    replay_key: str
    request_digest_sha256: str
    prepared: PreparedEffect


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    job_id: str
    replay_key: str
    request_digest_sha256: str
    effect: ScheduledEffect
    effect_digest_sha256: str
    records: tuple[EffectRecord, ...]
    review_item_ids: tuple[str, ...]
    approval_bindings: tuple[ApprovalBinding, ...]
    parameters: tuple[EffectParameter, ...] = ()

    def __post_init__(self) -> None:
        prepared = PreparedEffect(
            self.effect,
            self.records,
            self.review_item_ids,
            self.parameters,
        )
        if (
            not isinstance(self.job_id, str)
            or _JOB_ID.fullmatch(self.job_id) is None
            or not isinstance(self.replay_key, str)
            or _REPLAY_KEY.fullmatch(self.replay_key) is None
            or not isinstance(self.request_digest_sha256, str)
            or _SHA256.fullmatch(self.request_digest_sha256) is None
            or not isinstance(self.effect_digest_sha256, str)
            or self.effect_digest_sha256 != prepared.digest_sha256()
            or not isinstance(self.approval_bindings, tuple)
            or self.approval_bindings != _approval_bindings(self.records)
        ):
            raise WriterJobError("invalid durable effect receipt")

    @classmethod
    def from_command(cls, command: EffectCommand) -> EffectReceipt:
        if not isinstance(command, EffectCommand):
            raise WriterJobError("invalid scheduled effect command")
        return cls(
            job_id=command.job_id,
            replay_key=command.replay_key,
            request_digest_sha256=command.request_digest_sha256,
            effect=command.prepared.effect,
            effect_digest_sha256=command.prepared.digest_sha256(),
            records=command.prepared.records,
            review_item_ids=command.prepared.review_item_ids,
            approval_bindings=_approval_bindings(command.prepared.records),
            parameters=command.prepared.parameters,
        )


@dataclass(frozen=True, slots=True)
class JobRunResult:
    job_id: str
    replay_key: str
    request_digest_sha256: str
    disposition: JobRunDisposition
    effect: ScheduledEffect
    effect_count: int
    review_items_queued: int
    approved_inputs_applied: int


class ReplayJournal(Protocol):
    """Durable replay boundary; begin binds a key to one exact request digest."""

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None: ...

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None: ...

    def complete(self, result: JobRunResult) -> None: ...


class ScheduledApplication(Protocol):
    """Pure preparation boundary. Applications receive no filesystem or effect capability."""

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect: ...


class EffectCapability(Protocol):
    """Effect-specific durable I/O port owned by the composition root.

    Reserve durably binds the replay identity to the exact prepared effect
    before apply may expose that effect. Read must make a fully applied effect
    discoverable after interruption; partial effects must fail closed.
    """

    root: Path
    effect: ScheduledEffect
    local_only: bool
    dry_run: bool

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None: ...

    def reserve(self, command: EffectCommand) -> EffectReceipt: ...

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None: ...

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None: ...


class ApprovedIntentReader(Protocol):
    def get(self, review_id: ReviewId) -> ReviewAggregate | None: ...


class WriterLease(Protocol):
    def acquire(self, scope: LockScope) -> AbstractContextManager[None]: ...


_JOB_ID = re.compile(r"JOB-[0-9]{3}")
_REPLAY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PARAMETER_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _spec(
    job_id: str,
    effect: ScheduledEffect,
    *,
    review_boundary: ReviewBoundary = ReviewBoundary.NONE,
    planned_actions: tuple[str, ...] = (),
) -> WriterJobSpec:
    job = get_job(job_id)
    return WriterJobSpec(
        job_id=job_id,
        command=job.command,
        deployment_target=job.deployment_target,
        host_role=job.host_role,
        lock_scope=job.lock_scope,
        effect=effect,
        review_boundary=review_boundary,
        local_only=True,
        dry_run="--dry-run" in job.command,
        planned_actions=planned_actions,
    )


_WRITER_JOB_SPECS = MappingProxyType(
    {
        "JOB-006": _spec(
            "JOB-006",
            ScheduledEffect.OPERATOR_ARTIFACT,
            review_boundary=ReviewBoundary.PREPARATION_ONLY,
        ),
        "JOB-007": _spec("JOB-007", ScheduledEffect.APPEND_ONLY_SIGNALS),
        "JOB-008": _spec("JOB-008", ScheduledEffect.DIAGNOSTICS),
        "JOB-009": _spec(
            "JOB-009",
            ScheduledEffect.HOOK_SYNC_PLAN,
            planned_actions=("backup", "replace", "prune"),
        ),
        "JOB-010": _spec(
            "JOB-010",
            ScheduledEffect.LEDGER_WRITE,
            review_boundary=ReviewBoundary.APPROVED_INPUTS_ONLY,
        ),
        "JOB-011": _spec("JOB-011", ScheduledEffect.BACKUP_SNAPSHOT),
        "JOB-012": _spec(
            "JOB-012",
            ScheduledEffect.CURATION_PROMOTION,
            review_boundary=ReviewBoundary.APPROVED_INPUTS_ONLY,
        ),
        "JOB-014": _spec("JOB-014", ScheduledEffect.BACKUP_SNAPSHOT),
        "JOB-015": _spec("JOB-015", ScheduledEffect.LOCAL_GIT_SYNC),
        "JOB-016": _spec("JOB-016", ScheduledEffect.INDEX_REBUILD),
        "JOB-022": _spec("JOB-022", ScheduledEffect.NOW_PROJECTION),
        "JOB-023": _spec("JOB-023", ScheduledEffect.BACKUP_SNAPSHOT),
        "JOB-025": _spec("JOB-025", ScheduledEffect.BACKUP_SNAPSHOT),
    }
)


def get_writer_job_spec(job_id: str) -> WriterJobSpec:
    try:
        return _WRITER_JOB_SPECS[job_id]
    except (KeyError, TypeError):
        raise KeyError("unknown writer application job") from None


def run_writer_job(
    *,
    job_id: str,
    root: Path,
    replay_key: str,
    journal: ReplayJournal,
    application: ScheduledApplication,
    effect_capability: EffectCapability,
    lease: WriterLease | None = None,
    cutoff: datetime | None = None,
    approved_records: tuple[ApprovedIntentRecord, ...] = (),
    review_reader: ApprovedIntentReader | None = None,
    personal_local_only: bool = False,
) -> JobRunResult:
    spec = get_writer_job_spec(job_id)
    _validate_root(root)
    if not isinstance(replay_key, str) or _REPLAY_KEY.fullmatch(replay_key) is None:
        raise WriterJobError("invalid scheduled application replay key")
    if type(personal_local_only) is not bool:
        raise WriterJobError("invalid local-only repository policy")
    if job_id == "JOB-007":
        if (
            not isinstance(cutoff, datetime)
            or cutoff.tzinfo is None
            or cutoff.utcoffset() is None
        ):
            raise WriterJobError("signal scan cutoff must be timezone-aware")
    elif cutoff is not None:
        raise WriterJobError("cutoff is only valid for signal scanning")
    if personal_local_only and job_id != "JOB-015":
        raise WriterJobError("personal local-only policy is only valid for Git sync")

    _validate_effect_capability(spec, root, effect_capability)
    approval_bindings = _validate_approved_records(spec, approved_records, review_reader)
    invocation = WriterJobInvocation(
        job_id=spec.job_id,
        command=spec.command,
        replay_key=replay_key,
        effect=spec.effect,
        review_boundary=spec.review_boundary,
        local_only=spec.local_only,
        dry_run=spec.dry_run,
        apply_review_decisions=False,
        approved_records=approved_records,
        approval_bindings=approval_bindings,
        planned_actions=spec.planned_actions,
        personal_local_only=personal_local_only,
        cutoff=cutoff.astimezone(UTC) if cutoff is not None else None,
    )
    request_digest = _request_digest(invocation)

    def execute() -> JobRunResult:
        return _run_with_replay(
            spec=spec,
            invocation=invocation,
            request_digest=request_digest,
            journal=journal,
            application=application,
            effect_capability=effect_capability,
        )

    if spec.lock_scope is LockScope.NONE:
        return execute()
    if lease is None:
        raise WriterJobError("scheduled application requires its catalog lease")
    if spec.lock_scope is LockScope.SHARED_WRITER and (
        spec.deployment_target is not DeploymentTarget.CANONICAL_WRITER
        or spec.host_role is not HostRole.WRITER
    ):
        raise WriterJobError("shared writer lease requires the canonical writer")
    if spec.lock_scope is LockScope.INGRESS and spec.host_role is not HostRole.INGRESS:
        raise WriterJobError("ingress lease requires an append-only ingress job")
    with lease.acquire(spec.lock_scope):
        return execute()


def _validate_root(root: Path) -> None:
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or not root.is_dir()
        or root.is_symlink()
    ):
        raise WriterJobError("scheduled application root must be a safe local directory")


def _validate_effect_capability(
    spec: WriterJobSpec,
    root: Path,
    capability: EffectCapability,
) -> None:
    if (
        not isinstance(capability.root, Path)
        or capability.root != root
        or capability.effect is not spec.effect
        or capability.dry_run is not spec.dry_run
    ):
        raise WriterJobError("scheduled application requires its effect-specific I/O capability")
    if capability.local_only is not True:
        raise WriterJobError("scheduled application effect capability must remain local-only")


def _validate_approved_records(
    spec: WriterJobSpec,
    approved_records: tuple[ApprovedIntentRecord, ...],
    review_reader: ApprovedIntentReader | None,
) -> tuple[ApprovalBinding, ...]:
    if (
        not isinstance(approved_records, tuple)
        or any(not isinstance(record, ApprovedIntentRecord) for record in approved_records)
        or len({record.record_id for record in approved_records}) != len(approved_records)
    ):
        raise WriterJobError("invalid approved intent records")
    if spec.review_boundary is not ReviewBoundary.APPROVED_INPUTS_ONLY:
        if approved_records:
            raise WriterJobError("approved intent records are not valid for this job")
        return ()
    if approved_records and review_reader is None:
        raise WriterJobError("approved intent records require review-store read-back")

    bindings: list[ApprovalBinding] = []
    for record in approved_records:
        assert review_reader is not None
        aggregate = review_reader.get(record.review_id)
        if (
            not isinstance(aggregate, ReviewAggregate)
            or aggregate.proposal.state is not ReviewState.APPLIED
            or aggregate.approved_record != record
        ):
            raise WriterJobError("approved intent record conflicts with review-store read-back")
        bindings.append(ApprovalBinding.from_record(record))
    return tuple(bindings)


def _run_with_replay(
    *,
    spec: WriterJobSpec,
    invocation: WriterJobInvocation,
    request_digest: str,
    journal: ReplayJournal,
    application: ScheduledApplication,
    effect_capability: EffectCapability,
) -> JobRunResult:
    completed = journal.completed(invocation.job_id, invocation.replay_key)
    receipt = effect_capability.recover(invocation.job_id, invocation.replay_key)
    if completed is not None:
        _validate_completed(completed, invocation, request_digest, spec)
        if receipt is None:
            raise WriterJobError("scheduled application durable receipt is missing")
        recovered = _result_from_verified_receipt(
            receipt=receipt,
            spec=spec,
            invocation=invocation,
            request_digest=request_digest,
            capability=effect_capability,
        )
        if replace(completed, disposition=recovered.disposition) != recovered:
            raise WriterJobError("scheduled application replay conflict")
        return replace(completed, disposition=JobRunDisposition.REPLAYED)

    if receipt is not None:
        receipt_effect = _validated_receipt_effect(
            receipt=receipt,
            spec=spec,
            invocation=invocation,
            request_digest=request_digest,
        )
        if effect_capability.read(receipt) is None:
            effect_capability.apply(
                EffectCommand(
                    job_id=receipt.job_id,
                    replay_key=receipt.replay_key,
                    request_digest_sha256=receipt.request_digest_sha256,
                    prepared=receipt_effect,
                ),
                receipt,
            )
        recovered = _result_from_verified_receipt(
            receipt=receipt,
            spec=spec,
            invocation=invocation,
            request_digest=request_digest,
            capability=effect_capability,
        )
        journal.complete(recovered)
        return replace(recovered, disposition=JobRunDisposition.REPLAYED)

    journal.begin(invocation.job_id, invocation.replay_key, request_digest)
    prepared = application.prepare(invocation)
    _validate_prepared_effect(prepared, spec, invocation)
    command = EffectCommand(
        job_id=invocation.job_id,
        replay_key=invocation.replay_key,
        request_digest_sha256=request_digest,
        prepared=prepared,
    )
    receipt = effect_capability.reserve(command)
    reserved_effect = _validated_receipt_effect(
        receipt=receipt,
        spec=spec,
        invocation=invocation,
        request_digest=request_digest,
    )
    if reserved_effect != prepared:
        raise WriterJobError("scheduled application durable reservation conflict")
    effect_capability.apply(command, receipt)
    result = _result_from_verified_receipt(
        receipt=receipt,
        spec=spec,
        invocation=invocation,
        request_digest=request_digest,
        capability=effect_capability,
        expected=prepared,
    )
    journal.complete(result)
    return result


def _validate_completed(
    completed: JobRunResult,
    invocation: WriterJobInvocation,
    request_digest: str,
    spec: WriterJobSpec,
) -> None:
    if (
        not isinstance(completed, JobRunResult)
        or completed.job_id != invocation.job_id
        or completed.replay_key != invocation.replay_key
        or completed.request_digest_sha256 != request_digest
        or completed.effect is not spec.effect
        or completed.disposition not in {JobRunDisposition.APPLIED, JobRunDisposition.NOOP}
    ):
        raise WriterJobError("scheduled application replay conflict")


def _validate_prepared_effect(
    prepared: PreparedEffect,
    spec: WriterJobSpec,
    invocation: WriterJobInvocation,
) -> None:
    if not isinstance(prepared, PreparedEffect) or prepared.effect is not spec.effect:
        raise WriterJobError("scheduled application prepared an invalid effect")
    bound = _approval_bindings(prepared.records)
    if spec.review_boundary is ReviewBoundary.APPROVED_INPUTS_ONLY:
        if any(record.approval is None for record in prepared.records) or any(
            binding not in invocation.approval_bindings for binding in bound
        ):
            raise WriterJobError("scheduled effect has an unapproved approval binding")
    elif bound:
        raise WriterJobError("scheduled effect crossed its review boundary")
    if spec.effect is ScheduledEffect.DIAGNOSTICS and prepared.review_item_ids:
        raise WriterJobError("lint scheduled application must remain diagnostics-only")


def _result_from_verified_receipt(
    *,
    receipt: EffectReceipt,
    spec: WriterJobSpec,
    invocation: WriterJobInvocation,
    request_digest: str,
    capability: EffectCapability,
    expected: PreparedEffect | None = None,
) -> JobRunResult:
    receipt_effect = _validated_receipt_effect(
        receipt=receipt,
        spec=spec,
        invocation=invocation,
        request_digest=request_digest,
    )
    read_back = capability.read(receipt)
    if (
        not isinstance(read_back, PreparedEffect)
        or read_back != receipt_effect
        or read_back.digest_sha256() != receipt.effect_digest_sha256
        or expected is not None
        and read_back != expected
    ):
        raise WriterJobError("scheduled application durable effect read-back conflict")
    approvals = _approval_bindings(receipt.records)
    return JobRunResult(
        job_id=invocation.job_id,
        replay_key=invocation.replay_key,
        request_digest_sha256=request_digest,
        disposition=(
            JobRunDisposition.APPLIED if receipt.records else JobRunDisposition.NOOP
        ),
        effect=receipt.effect,
        effect_count=len(receipt.records),
        review_items_queued=len(receipt.review_item_ids),
        approved_inputs_applied=len(approvals),
    )


def _validated_receipt_effect(
    *,
    receipt: EffectReceipt,
    spec: WriterJobSpec,
    invocation: WriterJobInvocation,
    request_digest: str,
) -> PreparedEffect:
    if (
        not isinstance(receipt, EffectReceipt)
        or receipt.job_id != invocation.job_id
        or receipt.replay_key != invocation.replay_key
        or receipt.request_digest_sha256 != request_digest
        or receipt.effect is not spec.effect
    ):
        raise WriterJobError("scheduled application replay conflict")
    receipt_effect = PreparedEffect(
        receipt.effect,
        receipt.records,
        receipt.review_item_ids,
        receipt.parameters,
    )
    if (
        receipt.effect_digest_sha256 != receipt_effect.digest_sha256()
        or receipt.approval_bindings != _approval_bindings(receipt.records)
    ):
        raise WriterJobError("scheduled application durable receipt conflict")
    _validate_prepared_effect(receipt_effect, spec, invocation)
    return receipt_effect


def _approval_bindings(records: tuple[EffectRecord, ...]) -> tuple[ApprovalBinding, ...]:
    bindings: list[ApprovalBinding] = []
    for record in records:
        if record.approval is not None and record.approval not in bindings:
            bindings.append(record.approval)
    return tuple(bindings)


def _request_digest(invocation: WriterJobInvocation) -> str:
    cutoff = (
        invocation.cutoff.isoformat().replace("+00:00", "Z")
        if invocation.cutoff is not None
        else None
    )
    return sha256(
        canonical_json_bytes(
            {
                "approval_bindings": [
                    binding.to_dict() for binding in invocation.approval_bindings
                ],
                "command": list(invocation.command),
                "cutoff": cutoff,
                "job_id": invocation.job_id,
                "personal_local_only": invocation.personal_local_only,
                "planned_actions": list(invocation.planned_actions),
                "replay_key": invocation.replay_key,
            }
        )
    ).hexdigest()
