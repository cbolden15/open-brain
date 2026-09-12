"""Evidence-scoped executable adapter runners for the Phase 7 parity harness."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from open_brain_legacy import parity

_PROTOCOL_VERSION = "phase7-executable-runner-v1"
_HISTORICAL_SUMMARIZER_SCHEMA_DIGEST_SHA256 = (
    "8f24695d0449083b7736d2b6e815de23a5f945090b38074177540e6ad6eec73a"
)
_MAX_SYNTHETIC_INPUT_BYTES = 64 * 1024
_MAX_PROTOCOL_OUTPUT_BYTES = 256 * 1024
_CONTAINMENT_DEADLINE_SECONDS = 10


class ExecutableParityErrorCode(StrEnum):
    INVALID_REQUEST = "invalid-request"
    SYNTHETIC_INPUT_UNVERIFIED = "synthetic-input-unverified"
    CONTAINMENT_UNAVAILABLE = "containment-unavailable"
    CONTAINMENT_FAILED = "containment-failed"
    CONTAINMENT_UNVERIFIED = "containment-unverified"
    ARTIFACT_BINDING_UNVERIFIED = "artifact-binding-unverified"
    INVALID_PROTOCOL = "invalid-protocol"


class ExecutableParityError(ValueError):
    """Redacted, fail-closed adapter execution failure."""

    def __init__(self, code: ExecutableParityErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class OpenBrainWheelInput:
    source: Path
    expected_digest_sha256: str


@dataclass(frozen=True, slots=True)
class AdapterExecutionSpec:
    side: parity.ParitySide
    executable: Path
    executable_digest_sha256: str
    argv: tuple[str, ...]
    open_brain_wheel: OpenBrainWheelInput | None = None


@dataclass(frozen=True, slots=True)
class SyntheticInputAttestationEvidence:
    verifier_id: str
    attestation_id: str
    attestation_digest_sha256: str
    payload_digest_sha256: str
    payload_size_bytes: int
    scope: parity.EvidenceScope
    evaluated_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "attestation_id": self.attestation_id,
            "attestation_digest_sha256": self.attestation_digest_sha256,
            "payload_digest_sha256": self.payload_digest_sha256,
            "payload_size_bytes": self.payload_size_bytes,
            "scope": self.scope.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "expires_at": _timestamp(self.expires_at),
        }


class SyntheticInputAttestationVerifier(Protocol):
    def verify_synthetic_input(
        self,
        synthetic_input_attestation: object,
        *,
        payload_digest_sha256: str,
        payload_size_bytes: int,
        evaluated_at: datetime,
    ) -> SyntheticInputAttestationEvidence: ...


@dataclass(frozen=True, slots=True)
class ContainmentProviderRequest:
    """One externally contained adapter invocation over an attested fixture."""

    spec: AdapterExecutionSpec
    synthetic_input: bytes
    synthetic_input_evidence: SyntheticInputAttestationEvidence
    payload_digest_sha256: str
    payload_size_bytes: int
    max_protocol_output_bytes: int
    deadline_at: datetime
    scope: parity.EvidenceScope = parity.EvidenceScope.SYNTHETIC


@dataclass(frozen=True, slots=True)
class ContainmentProviderResult:
    protocol_output: bytes
    execution_attestation: object


class ContainmentProvider(Protocol):
    def execute_contained(
        self,
        request: ContainmentProviderRequest,
    ) -> ContainmentProviderResult: ...


@dataclass(frozen=True, slots=True)
class ContainedExecutionEvidence:
    """Verifier-normalized proof of one bounded synthetic execution."""

    verifier_id: str
    attestation_id: str
    attestation_digest_sha256: str
    side: parity.ParitySide
    executable_digest_sha256: str
    payload_digest_sha256: str
    payload_size_bytes: int
    protocol_output_digest_sha256: str
    protocol_output_size_bytes: int
    loaded_artifact_digest_sha256: str | None
    empty_environment: bool
    isolated_cwd: bool
    deadline_at: datetime
    completed_at: datetime
    surviving_descendants: int
    exit_code: int
    scope: parity.EvidenceScope
    evaluated_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "attestation_id": self.attestation_id,
            "attestation_digest_sha256": self.attestation_digest_sha256,
            "side": self.side.value,
            "executable_digest_sha256": self.executable_digest_sha256,
            "payload_digest_sha256": self.payload_digest_sha256,
            "payload_size_bytes": self.payload_size_bytes,
            "protocol_output_digest_sha256": self.protocol_output_digest_sha256,
            "protocol_output_size_bytes": self.protocol_output_size_bytes,
            "loaded_artifact_digest_sha256": self.loaded_artifact_digest_sha256,
            "empty_environment": self.empty_environment,
            "isolated_cwd": self.isolated_cwd,
            "deadline_at": _timestamp(self.deadline_at),
            "completed_at": _timestamp(self.completed_at),
            "surviving_descendants": self.surviving_descendants,
            "exit_code": self.exit_code,
            "scope": self.scope.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "expires_at": _timestamp(self.expires_at),
        }


class ContainmentAttestationVerifier(Protocol):
    def verify_contained_execution(
        self,
        execution_attestation: object,
        *,
        request: ContainmentProviderRequest,
        protocol_output_digest_sha256: str,
        protocol_output_size_bytes: int,
        evaluated_at: datetime,
    ) -> ContainedExecutionEvidence: ...


@dataclass(frozen=True, slots=True)
class AdapterArtifactBindingEvidence:
    """External proof binding the Open Brain adapter to the asserted artifact."""

    verifier_id: str
    attestation_id: str
    attestation_digest_sha256: str
    adapter_digest_sha256: str
    artifact: parity.BuiltArtifactIdentity
    manifest_version: str
    schema_digest_sha256: str
    scope: parity.EvidenceScope
    evaluated_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "verifier_id": self.verifier_id,
            "attestation_id": self.attestation_id,
            "attestation_digest_sha256": self.attestation_digest_sha256,
            "adapter_digest_sha256": self.adapter_digest_sha256,
            "artifact": self.artifact.to_dict(),
            "manifest_version": self.manifest_version,
            "schema_digest_sha256": self.schema_digest_sha256,
            "scope": self.scope.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "expires_at": _timestamp(self.expires_at),
        }


class AdapterArtifactBindingVerifier(Protocol):
    def verify_adapter_artifact_binding(
        self,
        adapter_artifact_binding_attestation: object,
        *,
        adapter_digest_sha256: str,
        artifact: parity.BuiltArtifactIdentity,
        evaluated_at: datetime,
    ) -> AdapterArtifactBindingEvidence: ...


@dataclass(frozen=True, slots=True)
class AdapterExecutionReceipt:
    side: parity.ParitySide
    verifier_id: str
    attestation_id: str
    executable_digest_sha256: str
    protocol_digest_sha256: str
    execution_attestation_digest_sha256: str
    payload_digest_sha256: str
    payload_size_bytes: int
    protocol_size_bytes: int
    loaded_artifact_digest_sha256: str | None
    empty_environment: bool
    isolated_cwd: bool
    deadline_at: datetime
    completed_at: datetime
    surviving_descendants: int
    exit_code: int
    scope: parity.EvidenceScope
    evaluated_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side.value,
            "verifier_id": self.verifier_id,
            "attestation_id": self.attestation_id,
            "executable_digest_sha256": self.executable_digest_sha256,
            "protocol_digest_sha256": self.protocol_digest_sha256,
            "execution_attestation_digest_sha256": (self.execution_attestation_digest_sha256),
            "payload_digest_sha256": self.payload_digest_sha256,
            "payload_size_bytes": self.payload_size_bytes,
            "protocol_size_bytes": self.protocol_size_bytes,
            "loaded_artifact_digest_sha256": self.loaded_artifact_digest_sha256,
            "empty_environment": self.empty_environment,
            "isolated_cwd": self.isolated_cwd,
            "deadline_at": _timestamp(self.deadline_at),
            "completed_at": _timestamp(self.completed_at),
            "surviving_descendants": self.surviving_descendants,
            "exit_code": self.exit_code,
            "scope": self.scope.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "expires_at": _timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class ExecutableParityRun:
    comparison: parity.SyntheticParityResult
    executions: tuple[AdapterExecutionReceipt, AdapterExecutionReceipt]
    input_attestation: SyntheticInputAttestationEvidence
    adapter_artifact_binding: AdapterArtifactBindingEvidence

    def to_dict(self) -> dict[str, object]:
        return {
            "comparison": self.comparison.to_dict(),
            "executions": [receipt.to_dict() for receipt in self.executions],
            "input_attestation": self.input_attestation.to_dict(),
            "adapter_artifact_binding": self.adapter_artifact_binding.to_dict(),
            "scope": parity.EvidenceScope.SYNTHETIC.value,
        }


class ExecutableParityRunner:
    """Orchestrate externally contained adapters and delegate comparison to the harness."""

    def run(
        self,
        *,
        predecessor: AdapterExecutionSpec,
        open_brain: AdapterExecutionSpec,
        containment_provider: ContainmentProvider | None,
        containment_verifier: ContainmentAttestationVerifier | None,
        synthetic_input: bytes,
        synthetic_input_attestation: object,
        synthetic_input_verifier: SyntheticInputAttestationVerifier | None,
        artifact: parity.BuiltArtifactIdentity,
        artifact_attestation: object,
        artifact_verifier: parity.ArtifactAttestationVerifier,
        adapter_artifact_binding_attestation: object,
        adapter_artifact_binding_verifier: AdapterArtifactBindingVerifier | None,
        evaluated_at: datetime,
    ) -> ExecutableParityRun:
        evaluated = _utc(evaluated_at)
        if (
            containment_provider is not None
            and containment_verifier is not None
            and id(containment_provider) == id(containment_verifier)
        ):
            raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
        predecessor_spec = _validate_spec(predecessor, parity.ParitySide.LEGACY)
        open_brain_spec = _validate_spec(open_brain, parity.ParitySide.OPEN_BRAIN)
        _validate_artifact(artifact)
        _validate_wheel_inputs(predecessor_spec, open_brain_spec, artifact)
        payload = _validate_synthetic_input(synthetic_input)
        evidence = _verify_synthetic_input(
            synthetic_input_verifier,
            synthetic_input_attestation,
            payload_digest_sha256=sha256(payload).hexdigest(),
            payload_size_bytes=len(payload),
            evaluated_at=evaluated,
        )
        binding = _verify_adapter_artifact_binding(
            adapter_artifact_binding_verifier,
            adapter_artifact_binding_attestation,
            adapter_digest_sha256=open_brain_spec.executable_digest_sha256,
            artifact=artifact,
            evaluated_at=evaluated,
            expected_scope=parity.EvidenceScope.SYNTHETIC,
        )
        legacy, legacy_receipt = cast(
            tuple[parity.SyntheticParityInput, AdapterExecutionReceipt],
            _request_contained_execution(
                containment_provider,
                containment_verifier,
                predecessor_spec,
                payload,
                evidence,
                artifact=artifact,
                evaluated_at=evaluated,
                expected_scope=parity.EvidenceScope.SYNTHETIC,
            ),
        )
        normalized, normalized_receipt = cast(
            tuple[parity.SyntheticParityInput, AdapterExecutionReceipt],
            _request_contained_execution(
                containment_provider,
                containment_verifier,
                open_brain_spec,
                payload,
                evidence,
                artifact=artifact,
                evaluated_at=evaluated,
                expected_scope=parity.EvidenceScope.SYNTHETIC,
            ),
        )
        comparison = parity.compare_synthetic_parity(
            legacy,
            normalized,
            evaluated_at=evaluated,
            artifact_attestation=artifact_attestation,
            artifact_verifier=artifact_verifier,
        )
        return ExecutableParityRun(
            comparison=comparison,
            executions=(legacy_receipt, normalized_receipt),
            input_attestation=evidence,
            adapter_artifact_binding=binding,
        )


@dataclass(frozen=True, slots=True)
class LiveExecutableParityRun:
    """A live comparison plus its independently verified execution evidence."""

    comparison: parity.LiveParityResult
    executions: tuple[AdapterExecutionReceipt, AdapterExecutionReceipt]
    input_attestation: SyntheticInputAttestationEvidence
    adapter_artifact_binding: AdapterArtifactBindingEvidence

    def to_dict(self) -> dict[str, object]:
        return {
            "comparison": self.comparison.to_dict(),
            "executions": [receipt.to_dict() for receipt in self.executions],
            "input_attestation": self.input_attestation.to_dict(),
            "adapter_artifact_binding": self.adapter_artifact_binding.to_dict(),
            "scope": parity.EvidenceScope.LIVE.value,
        }


class LiveExecutableParityRunner:
    """Orchestrate live-attested adapter executions without performing ambient I/O."""

    def run(
        self,
        *,
        predecessor: AdapterExecutionSpec,
        open_brain: AdapterExecutionSpec,
        containment_provider: ContainmentProvider | None,
        containment_verifier: ContainmentAttestationVerifier | None,
        synthetic_input: bytes,
        synthetic_input_attestation: object,
        synthetic_input_verifier: SyntheticInputAttestationVerifier | None,
        artifact: parity.BuiltArtifactIdentity,
        artifact_attestation: object,
        artifact_verifier: parity.ArtifactAttestationVerifier,
        adapter_artifact_binding_attestation: object,
        adapter_artifact_binding_verifier: AdapterArtifactBindingVerifier | None,
        evaluated_at: datetime,
    ) -> LiveExecutableParityRun:
        evaluated = _utc(evaluated_at)
        if (
            containment_provider is not None
            and containment_verifier is not None
            and id(containment_provider) == id(containment_verifier)
        ):
            raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
        predecessor_spec = _validate_spec(predecessor, parity.ParitySide.LEGACY)
        open_brain_spec = _validate_spec(open_brain, parity.ParitySide.OPEN_BRAIN)
        _validate_artifact(artifact)
        _validate_wheel_inputs(predecessor_spec, open_brain_spec, artifact)
        payload = _validate_synthetic_input(synthetic_input)
        evidence = _verify_synthetic_input(
            synthetic_input_verifier,
            synthetic_input_attestation,
            payload_digest_sha256=sha256(payload).hexdigest(),
            payload_size_bytes=len(payload),
            evaluated_at=evaluated,
        )
        binding = _verify_adapter_artifact_binding(
            adapter_artifact_binding_verifier,
            adapter_artifact_binding_attestation,
            adapter_digest_sha256=open_brain_spec.executable_digest_sha256,
            artifact=artifact,
            evaluated_at=evaluated,
            expected_scope=parity.EvidenceScope.LIVE,
        )
        legacy, legacy_receipt = cast(
            tuple[parity.LiveParityInput, AdapterExecutionReceipt],
            _request_contained_execution(
                containment_provider,
                containment_verifier,
                predecessor_spec,
                payload,
                evidence,
                artifact=artifact,
                evaluated_at=evaluated,
                expected_scope=parity.EvidenceScope.LIVE,
            ),
        )
        normalized, normalized_receipt = cast(
            tuple[parity.LiveParityInput, AdapterExecutionReceipt],
            _request_contained_execution(
                containment_provider,
                containment_verifier,
                open_brain_spec,
                payload,
                evidence,
                artifact=artifact,
                evaluated_at=evaluated,
                expected_scope=parity.EvidenceScope.LIVE,
            ),
        )
        comparison = parity.compare_live_parity(
            legacy,
            normalized,
            evaluated_at=evaluated,
            artifact_attestation=artifact_attestation,
            artifact_verifier=artifact_verifier,
        )
        return LiveExecutableParityRun(
            comparison=comparison,
            executions=(legacy_receipt, normalized_receipt),
            input_attestation=evidence,
            adapter_artifact_binding=binding,
        )


def _validate_spec(spec: object, expected_side: parity.ParitySide) -> AdapterExecutionSpec:
    if type(spec) is not AdapterExecutionSpec or spec.side is not expected_side:
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
    path = spec.executable
    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or type(spec.argv) is not tuple
        or not spec.argv
        or any(type(value) is not str or not value or "\x00" in value for value in spec.argv)
        or spec.argv[0] != str(path)
        or not _is_sha256(spec.executable_digest_sha256)
    ):
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
    return spec


def _validate_wheel_inputs(
    predecessor: AdapterExecutionSpec,
    open_brain: AdapterExecutionSpec,
    artifact: parity.BuiltArtifactIdentity,
) -> None:
    wheel = open_brain.open_brain_wheel
    if (
        predecessor.open_brain_wheel is not None
        or type(wheel) is not OpenBrainWheelInput
        or type(wheel.source) is not type(Path())
        or not wheel.source.is_absolute()
        or not _is_sha256(wheel.expected_digest_sha256)
        or wheel.expected_digest_sha256 != artifact.digest_sha256
    ):
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)


def _validate_synthetic_input(value: object) -> bytes:
    if type(value) is not bytes or not value or len(value) > _MAX_SYNTHETIC_INPUT_BYTES:
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
    return value


def _verify_synthetic_input(
    verifier: SyntheticInputAttestationVerifier | None,
    attestation: object,
    *,
    payload_digest_sha256: str,
    payload_size_bytes: int,
    evaluated_at: datetime,
) -> SyntheticInputAttestationEvidence:
    if verifier is None:
        raise ExecutableParityError(ExecutableParityErrorCode.SYNTHETIC_INPUT_UNVERIFIED)
    try:
        verify = verifier.verify_synthetic_input
        evidence = verify(
            attestation,
            payload_digest_sha256=payload_digest_sha256,
            payload_size_bytes=payload_size_bytes,
            evaluated_at=evaluated_at,
        )
    except Exception:
        raise ExecutableParityError(ExecutableParityErrorCode.SYNTHETIC_INPUT_UNVERIFIED) from None
    if (
        type(evidence) is not SyntheticInputAttestationEvidence
        or evidence.scope is not parity.EvidenceScope.SYNTHETIC
        or evidence.payload_digest_sha256 != payload_digest_sha256
        or evidence.payload_size_bytes != payload_size_bytes
        or not _is_aware_datetime(evidence.evaluated_at)
        or not _is_aware_datetime(evidence.expires_at)
        or evidence.evaluated_at != evaluated_at
        or evidence.expires_at <= evidence.evaluated_at
        or not all(
            _is_sha256(digest)
            for digest in (evidence.attestation_digest_sha256, evidence.payload_digest_sha256)
        )
        or not _is_safe_id(evidence.verifier_id)
        or not _is_safe_id(evidence.attestation_id)
    ):
        raise ExecutableParityError(ExecutableParityErrorCode.SYNTHETIC_INPUT_UNVERIFIED)
    return evidence


def _verify_adapter_artifact_binding(
    verifier: AdapterArtifactBindingVerifier | None,
    attestation: object,
    *,
    adapter_digest_sha256: str,
    artifact: parity.BuiltArtifactIdentity,
    evaluated_at: datetime,
    expected_scope: parity.EvidenceScope,
) -> AdapterArtifactBindingEvidence:
    if verifier is None:
        raise ExecutableParityError(ExecutableParityErrorCode.ARTIFACT_BINDING_UNVERIFIED)
    try:
        verify = verifier.verify_adapter_artifact_binding
        evidence = verify(
            attestation,
            adapter_digest_sha256=adapter_digest_sha256,
            artifact=artifact,
            evaluated_at=evaluated_at,
        )
    except Exception:
        raise ExecutableParityError(ExecutableParityErrorCode.ARTIFACT_BINDING_UNVERIFIED) from None
    if (
        type(evidence) is not AdapterArtifactBindingEvidence
        or evidence.adapter_digest_sha256 != adapter_digest_sha256
        or evidence.artifact != artifact
        or evidence.manifest_version != parity.PARITY_HARNESS_VERSION
        or evidence.schema_digest_sha256 != parity.PARITY_SCHEMA_DIGEST_SHA256
        or evidence.scope is not expected_scope
        or not _is_aware_datetime(evidence.evaluated_at)
        or not _is_aware_datetime(evidence.expires_at)
        or evidence.evaluated_at != evaluated_at
        or evidence.expires_at <= evidence.evaluated_at
        or not _is_sha256(evidence.attestation_digest_sha256)
        or not _is_sha256(evidence.adapter_digest_sha256)
        or not _is_safe_id(evidence.verifier_id)
        or not _is_safe_id(evidence.attestation_id)
    ):
        raise ExecutableParityError(ExecutableParityErrorCode.ARTIFACT_BINDING_UNVERIFIED)
    return evidence


def _request_contained_execution(
    provider: ContainmentProvider | None,
    verifier: ContainmentAttestationVerifier | None,
    spec: AdapterExecutionSpec,
    payload: bytes,
    input_evidence: SyntheticInputAttestationEvidence,
    *,
    artifact: parity.BuiltArtifactIdentity,
    evaluated_at: datetime,
    expected_scope: parity.EvidenceScope,
) -> tuple[parity.SyntheticParityInput | parity.LiveParityInput, AdapterExecutionReceipt]:
    if provider is None or verifier is None:
        raise ExecutableParityError(ExecutableParityErrorCode.CONTAINMENT_UNAVAILABLE)
    request = ContainmentProviderRequest(
        spec=spec,
        synthetic_input=payload,
        synthetic_input_evidence=input_evidence,
        payload_digest_sha256=sha256(payload).hexdigest(),
        payload_size_bytes=len(payload),
        max_protocol_output_bytes=_MAX_PROTOCOL_OUTPUT_BYTES,
        deadline_at=evaluated_at + timedelta(seconds=_CONTAINMENT_DEADLINE_SECONDS),
        scope=expected_scope,
    )
    try:
        execute = provider.execute_contained
        result = execute(request)
    except Exception:
        raise ExecutableParityError(ExecutableParityErrorCode.CONTAINMENT_FAILED) from None
    if type(result) is not ContainmentProviderResult:
        raise ExecutableParityError(ExecutableParityErrorCode.CONTAINMENT_UNVERIFIED)
    output = result.protocol_output
    if type(output) is not bytes or not output or len(output) > request.max_protocol_output_bytes:
        raise ExecutableParityError(ExecutableParityErrorCode.CONTAINMENT_UNVERIFIED)
    output_digest = sha256(output).hexdigest()
    execution_evidence = _verify_contained_execution(
        verifier,
        result.execution_attestation,
        request=request,
        protocol_output_digest_sha256=output_digest,
        protocol_output_size_bytes=len(output),
        evaluated_at=evaluated_at,
        expected_scope=expected_scope,
    )
    parsed = _parse_protocol(
        output,
        side=spec.side,
        artifact=artifact,
        expected_scope=expected_scope,
    )
    return parsed, AdapterExecutionReceipt(
        side=execution_evidence.side,
        verifier_id=execution_evidence.verifier_id,
        attestation_id=execution_evidence.attestation_id,
        executable_digest_sha256=execution_evidence.executable_digest_sha256,
        protocol_digest_sha256=execution_evidence.protocol_output_digest_sha256,
        execution_attestation_digest_sha256=(execution_evidence.attestation_digest_sha256),
        payload_digest_sha256=execution_evidence.payload_digest_sha256,
        payload_size_bytes=execution_evidence.payload_size_bytes,
        protocol_size_bytes=execution_evidence.protocol_output_size_bytes,
        loaded_artifact_digest_sha256=execution_evidence.loaded_artifact_digest_sha256,
        empty_environment=execution_evidence.empty_environment,
        isolated_cwd=execution_evidence.isolated_cwd,
        deadline_at=execution_evidence.deadline_at,
        completed_at=execution_evidence.completed_at,
        surviving_descendants=execution_evidence.surviving_descendants,
        exit_code=execution_evidence.exit_code,
        scope=execution_evidence.scope,
        evaluated_at=execution_evidence.evaluated_at,
        expires_at=execution_evidence.expires_at,
    )


def _verify_contained_execution(
    verifier: ContainmentAttestationVerifier,
    execution_attestation: object,
    *,
    request: ContainmentProviderRequest,
    protocol_output_digest_sha256: str,
    protocol_output_size_bytes: int,
    evaluated_at: datetime,
    expected_scope: parity.EvidenceScope,
) -> ContainedExecutionEvidence:
    try:
        verify = verifier.verify_contained_execution
        evidence = verify(
            execution_attestation,
            request=request,
            protocol_output_digest_sha256=protocol_output_digest_sha256,
            protocol_output_size_bytes=protocol_output_size_bytes,
            evaluated_at=evaluated_at,
        )
    except Exception:
        raise ExecutableParityError(ExecutableParityErrorCode.CONTAINMENT_UNVERIFIED) from None
    if (
        type(evidence) is not ContainedExecutionEvidence
        or evidence.side is not request.spec.side
        or evidence.executable_digest_sha256 != request.spec.executable_digest_sha256
        or evidence.payload_digest_sha256 != request.payload_digest_sha256
        or evidence.payload_size_bytes != request.payload_size_bytes
        or evidence.protocol_output_digest_sha256 != protocol_output_digest_sha256
        or evidence.protocol_output_size_bytes != protocol_output_size_bytes
        or (
            request.spec.side is parity.ParitySide.OPEN_BRAIN
            and (
                request.spec.open_brain_wheel is None
                or evidence.loaded_artifact_digest_sha256
                != request.spec.open_brain_wheel.expected_digest_sha256
                or not _is_sha256(evidence.loaded_artifact_digest_sha256)
            )
        )
        or (
            request.spec.side is parity.ParitySide.LEGACY
            and evidence.loaded_artifact_digest_sha256 is not None
        )
        or evidence.empty_environment is not True
        or evidence.isolated_cwd is not True
        or not _is_aware_datetime(evidence.deadline_at)
        or not _is_aware_datetime(evidence.completed_at)
        or evidence.deadline_at != request.deadline_at
        or evidence.completed_at < evaluated_at
        or evidence.completed_at > request.deadline_at
        or type(evidence.surviving_descendants) is not int
        or evidence.surviving_descendants != 0
        or type(evidence.exit_code) is not int
        or evidence.exit_code != 0
        or evidence.scope is not expected_scope
        or not _is_aware_datetime(evidence.evaluated_at)
        or not _is_aware_datetime(evidence.expires_at)
        or evidence.evaluated_at != evaluated_at
        or evidence.expires_at <= evidence.evaluated_at
        or not all(
            _is_sha256(digest)
            for digest in (
                evidence.attestation_digest_sha256,
                evidence.executable_digest_sha256,
                evidence.payload_digest_sha256,
                evidence.protocol_output_digest_sha256,
            )
        )
        or not _is_safe_id(evidence.verifier_id)
        or not _is_safe_id(evidence.attestation_id)
    ):
        raise ExecutableParityError(ExecutableParityErrorCode.CONTAINMENT_UNVERIFIED)
    return evidence


def _parse_protocol(
    raw: bytes,
    *,
    side: parity.ParitySide,
    artifact: parity.BuiltArtifactIdentity,
    expected_scope: parity.EvidenceScope,
) -> parity.SyntheticParityInput | parity.LiveParityInput:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_json_constant,
        )
        root = _mapping(value)
        _require_keys(
            root,
            {"protocol_version", "manifest_version", "schema_digest_sha256", "artifact", "facets"},
        )
        schema_digest = root["schema_digest_sha256"]
        historical_summarizer = (
            side is parity.ParitySide.LEGACY
            and schema_digest == _HISTORICAL_SUMMARIZER_SCHEMA_DIGEST_SHA256
        )
        if (
            root["protocol_version"] != _PROTOCOL_VERSION
            or root["manifest_version"] != parity.PARITY_HARNESS_VERSION
            or (schema_digest != parity.PARITY_SCHEMA_DIGEST_SHA256 and not historical_summarizer)
            or _parse_artifact(root["artifact"]) != artifact
        ):
            raise ValueError
        facets = _list(root["facets"])
        snapshots = tuple(
            _parse_snapshot(
                item,
                side=side,
                artifact=artifact,
                historical_summarizer=historical_summarizer,
                expected_scope=expected_scope,
            )
            for item in facets
        )
        if expected_scope is parity.EvidenceScope.SYNTHETIC:
            return parity.SyntheticParityInput(
                side=side,
                artifact=artifact,
                facets=cast(tuple[parity.SyntheticFacetSnapshot, ...], snapshots),
            )
        if expected_scope is parity.EvidenceScope.LIVE:
            return parity.LiveParityInput(
                side=side,
                artifact=artifact,
                facets=cast(tuple[parity.LiveFacetSnapshot, ...], snapshots),
            )
        raise ValueError
    except (
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        parity.ParityValidationError,
    ):
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_PROTOCOL) from None


def _parse_snapshot(
    value: object,
    *,
    side: parity.ParitySide,
    artifact: parity.BuiltArtifactIdentity,
    historical_summarizer: bool,
    expected_scope: parity.EvidenceScope,
) -> parity.SyntheticFacetSnapshot | parity.LiveFacetSnapshot:
    item = _mapping(value)
    _require_keys(item, {"facet", "metadata"})
    facet = parity.ParityFacet(_string(item["facet"]))
    snapshot_type = (
        parity.SyntheticFacetSnapshot
        if expected_scope is parity.EvidenceScope.SYNTHETIC
        else parity.LiveFacetSnapshot
    )
    return snapshot_type(
        facet=facet,
        artifact=artifact,
        metadata=_parse_metadata(
            facet,
            item["metadata"],
            side=side,
            historical_summarizer=historical_summarizer,
        ),
    )


def _parse_metadata(
    facet: parity.ParityFacet,
    value: object,
    *,
    side: parity.ParitySide,
    historical_summarizer: bool,
) -> parity.FacetMetadata:
    data = _mapping(value)
    if facet is parity.ParityFacet.REQUEST_CONTENT:
        _require_keys(data, {"request_status", "request_id", "content_ids"})
        return parity.RequestContentMetadata(
            parity.RequestStatus(_string(data["request_status"])),
            _string(data["request_id"]),
            _strings(data["content_ids"]),
        )
    if facet is parity.ParityFacet.RAW_FILE_SET:
        _require_keys(data, {"file_digests_sha256"})
        return parity.RawFileSetMetadata(_strings(data["file_digests_sha256"]))
    if facet is parity.ParityFacet.QUEUE_RETRY:
        _require_keys(data, {"transitions"})
        transitions = tuple(_parse_transition(item) for item in _list(data["transitions"]))
        return parity.QueueRetryMetadata(transitions)
    if facet is parity.ParityFacet.FRONTMATTER_PROVENANCE:
        _require_keys(
            data,
            {
                "schema_version",
                "content_kind",
                "privacy_tier",
                "source_kind",
                "source_ref_digest_sha256",
                "content_origin",
                "owner_context",
                "redaction_policy_version",
            },
        )
        return parity.FrontmatterProvenanceMetadata(
            _integer(data["schema_version"]),
            parity.ContentKind(_string(data["content_kind"])),
            parity.PrivacyTier(_string(data["privacy_tier"])),
            parity.SourceKind(_string(data["source_kind"])),
            _string(data["source_ref_digest_sha256"]),
            parity.ContentOrigin(_string(data["content_origin"])),
            parity.OwnerContext(_string(data["owner_context"])),
            _integer(data["redaction_policy_version"]),
        )
    if facet is parity.ParityFacet.ROUTING:
        _require_keys(data, {"destination"})
        return parity.RoutingMetadata(parity.RoutingDestination(_string(data["destination"])))
    if facet is parity.ParityFacet.LEDGER_CITATIONS:
        _require_keys(data, {"ledger_item_ids", "citation_ids"})
        return parity.LedgerCitationMetadata(
            _strings(data["ledger_item_ids"]), _strings(data["citation_ids"])
        )
    if facet is parity.ParityFacet.REVIEW_PROPOSALS:
        _require_keys(data, {"proposals"})
        return parity.ReviewProposalsMetadata(
            tuple(_parse_proposal(item) for item in _list(data["proposals"]))
        )
    if facet is parity.ParityFacet.CLI_JSON:
        if historical_summarizer:
            _require_keys(
                data,
                {"command", "status", "exit_class", "field_digests", "redacted"},
            )
            profile = parity.CliProfile.SUMMARIZER_CRON
        else:
            _require_keys(
                data,
                {"profile", "command", "status", "exit_class", "field_digests", "redacted"},
            )
            profile = parity.CliProfile(_string(data["profile"]))
        if data["redacted"] is not True:
            raise ValueError
        expected_side = {
            parity.CliProfile.OPEN_BRAIN_STATUS: parity.ParitySide.OPEN_BRAIN,
            parity.CliProfile.OPEN_BRAIN_CRON: parity.ParitySide.OPEN_BRAIN,
            parity.CliProfile.BRAIN_SYSTEM_STATUS: parity.ParitySide.LEGACY,
            parity.CliProfile.SUMMARIZER_CRON: parity.ParitySide.LEGACY,
        }[profile]
        if side is not expected_side:
            raise ValueError
        return parity.CliJsonMetadata(
            profile,
            parity.CliCommand(_string(data["command"])),
            parity.CliStatus(_string(data["status"])),
            parity.CliExitClass(_integer(data["exit_class"])),
            _parse_field_digests(data["field_digests"]),
        )
    _require_keys(data, {"outcome", "findings"})
    return parity.HealthDoctorMetadata(
        parity.HealthOutcome(_string(data["outcome"])),
        tuple(_parse_finding(item) for item in _list(data["findings"])),
    )


def _parse_transition(value: object) -> parity.QueueTransition:
    data = _mapping(value)
    _require_keys(data, {"from_state", "to_state", "attempt_count", "last_error_code"})
    error = data["last_error_code"]
    return parity.QueueTransition(
        parity.QueueState(_string(data["from_state"])),
        parity.QueueState(_string(data["to_state"])),
        _integer(data["attempt_count"]),
        None if error is None else parity.QueueErrorClass(_string(error)),
    )


def _parse_proposal(value: object) -> parity.ReviewProposal:
    data = _mapping(value)
    _require_keys(
        data,
        {
            "schema_version",
            "review_id",
            "capture_id",
            "source_ref_digest_sha256",
            "privacy_tier",
            "proposed_intent",
            "proposal_reason_digest_sha256",
            "capture_why_digest_sha256",
            "state",
            "created_at",
            "actor_kind",
            "actor_label_digest_sha256",
        },
    )
    return parity.ReviewProposal(
        _integer(data["schema_version"]),
        _string(data["review_id"]),
        _string(data["capture_id"]),
        _string(data["source_ref_digest_sha256"]),
        parity.PrivacyTier(_string(data["privacy_tier"])),
        parity.ReviewIntent(_string(data["proposed_intent"])),
        _string(data["proposal_reason_digest_sha256"]),
        _string(data["capture_why_digest_sha256"]),
        parity.ReviewProposalState(_string(data["state"])),
        _parse_timestamp(data["created_at"]),
        parity.ReviewActorKind(_string(data["actor_kind"])),
        _string(data["actor_label_digest_sha256"]),
    )


def _parse_field_digests(value: object) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for item in _list(value):
        data = _mapping(item)
        _require_keys(data, {"field", "digest_sha256"})
        pairs.append((_string(data["field"]), _string(data["digest_sha256"])))
    return tuple(pairs)


def _parse_finding(value: object) -> parity.HealthFinding:
    data = _mapping(value)
    _require_keys(data, {"probe", "finding_class", "state"})
    return parity.HealthFinding(
        parity.DoctorProbe(_string(data["probe"])),
        parity.HealthFindingClass(_string(data["finding_class"])),
        parity.DoctorProbeState(_string(data["state"])),
    )


def _parse_artifact(value: object) -> parity.BuiltArtifactIdentity:
    data = _mapping(value)
    _require_keys(data, {"distribution", "version", "digest_sha256"})
    if data["distribution"] != parity.BuiltArtifactIdentity.distribution:
        raise ValueError
    return parity.BuiltArtifactIdentity(_string(data["version"]), _string(data["digest_sha256"]))


def _mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict or not all(type(key) is str for key in value):
        raise ValueError
    return value


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise ValueError
    return value


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_string(item) for item in _list(value))


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _require_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError


def _validate_artifact(value: object) -> parity.BuiltArtifactIdentity:
    if type(value) is not parity.BuiltArtifactIdentity:
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
    return value


def _utc(value: object) -> datetime:
    if not _is_aware_datetime(value):
        raise ExecutableParityError(ExecutableParityErrorCode.INVALID_REQUEST)
    assert isinstance(value, datetime)
    return value.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime:
    timestamp = _string(value)
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_safe_id(value: object) -> bool:
    if type(value) is not str or "_" not in value:
        return False
    prefix, digest = value.split("_", 1)
    return (
        prefix.isascii()
        and prefix.isalnum()
        and _is_sha256(digest + "0" * (64 - len(digest)))
        and 16 <= len(digest) <= 64
    )


def _is_aware_datetime(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None
