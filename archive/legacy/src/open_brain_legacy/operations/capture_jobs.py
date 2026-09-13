"""Capture-only scheduled applications for public ingress jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from open_brain_engine.core.models import CaptureEnvelope, CaptureWhyOrigin, Provenance
from open_brain_engine.core.ports import PutDisposition
from open_brain_engine.engine import LockScope, PublicJobCaptureSink, ReferencePayload, TextPayload

from .catalog import get_job
from .models import HostRole, JobSpec, JobState, OutputPolicy, WriterScope


class CaptureJobContractError(ValueError):
    """A scheduled capture job exceeds its append-only authority."""


class CaptureWrite(StrEnum):
    """The only durable effect available to a capture scheduled application."""

    ENGINE_CAPTURE = "engine-capture"


class PublicCaptureSink(Protocol):
    """Capture-only capability injected by the one-root application."""

    def submit(self, *args: object, **kwargs: object) -> object: ...


_EXPECTED_COMMANDS = MappingProxyType(
    {
        "JOB-005": (
            "open-brain",
            "capture",
            "imessage-ingress",
            "--append",
            "--json",
        ),
        "JOB-027": (
            "open-brain",
            "capture",
            "serve",
            "--bind=CONFIGURED_PRIVATE_BIND",
            "--port=CONFIGURED_PORT",
        ),
        "JOB-028": (
            "open-brain",
            "capture",
            "serve",
            "--mode=ingress",
            "--bind=CONFIGURED_PRIVATE_BIND",
            "--port=CONFIGURED_PORT",
        ),
        "JOB-029": (
            "open-brain",
            "capture",
            "poll",
            "--source=youtube",
            "--mode=ingress",
            "--json",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class CaptureAppendResult:
    """Opaque append metadata safe for scheduled-job output."""

    job_id: str
    capture_id: str
    disposition: PutDisposition

    def to_dict(self) -> dict[str, str]:
        return {
            "capture_id": self.capture_id,
            "disposition": self.disposition.value,
            "job_id": self.job_id,
        }


@dataclass(frozen=True, slots=True)
class CaptureJobApplication:
    """A validated public CLI specification with engine-capture authority only."""

    job: JobSpec

    def __post_init__(self) -> None:
        expected_command = _EXPECTED_COMMANDS.get(self.job.id)
        if expected_command is None or self.job.command != expected_command:
            raise CaptureJobContractError("invalid capture job public CLI argv")
        if (
            self.job.host_role is not HostRole.INGRESS
            or self.job.writer_scope is not WriterScope.CAPTURE_INGRESS
            or self.job.lock_scope is not LockScope.INGRESS
            or self.job.output_policy is not OutputPolicy.REDACTED_REPORT
        ):
            raise CaptureJobContractError("capture job must remain public capture ingress")
        if self.job.state is not JobState.ENABLED:
            raise CaptureJobContractError("production capture job must remain enabled")

    @property
    def argv(self) -> tuple[str, ...]:
        return self.job.command

    @property
    def allowed_writes(self) -> frozenset[CaptureWrite]:
        return frozenset({CaptureWrite.ENGINE_CAPTURE})

    @property
    def service_actions(self) -> tuple[str, ...]:
        return ()

    def submit(
        self,
        *,
        sink: PublicJobCaptureSink,
        envelope: CaptureEnvelope,
    ) -> CaptureAppendResult:
        """Durably submit one public-job envelope without queue or routing authority."""
        if not isinstance(sink, PublicJobCaptureSink) or not isinstance(envelope, CaptureEnvelope):
            raise CaptureJobContractError("invalid public capture submission")
        payload = (
            ReferencePayload(envelope.source_url, envelope.shared_text or None)
            if envelope.source_url is not None
            else TextPayload(envelope.shared_text)
        )
        try:
            source_origin = (
                envelope.provenance.content_origin
                if envelope.provenance.content_origin.value in {"third_party", "unknown"}
                else type(envelope.provenance.content_origin).THIRD_PARTY
            )
            provenance = Provenance.create(
                source_ref=envelope.provenance.source_ref,
                content_origin=source_origin,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            )
            receipt = sink.submit(
                payload,
                delivery_id=str(envelope.capture_id),
                source_origin=source_origin,
                source_reference=envelope.provenance.source_ref,
                provenance=provenance,
                privacy=envelope.privacy_decision,
                title=envelope.title,
            )
        except (TypeError, ValueError) as error:
            raise CaptureJobContractError("public capture submission failed") from error
        capture_id = getattr(receipt, "capture_id", None)
        duplicate = getattr(receipt, "duplicate", None)
        if not isinstance(capture_id, str) or type(duplicate) is not bool:
            raise CaptureJobContractError("invalid public capture receipt")
        return CaptureAppendResult(
            job_id=self.job.id,
            capture_id=capture_id,
            disposition=PutDisposition.DUPLICATE if duplicate else PutDisposition.CREATED,
        )


def get_capture_job(job_id: str) -> CaptureJobApplication:
    """Return one validated capture scheduled-application specification."""
    return CaptureJobApplication(get_job(job_id))
