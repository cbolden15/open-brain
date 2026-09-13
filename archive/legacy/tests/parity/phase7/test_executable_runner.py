from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

import open_brain_legacy.parity.runner as runner_module
from open_brain_legacy.parity import (
    PARITY_HARNESS_VERSION,
    PARITY_SCHEMA_DIGEST_SHA256,
    ArtifactAttestationEvidence,
    BuiltArtifactIdentity,
    ComparisonOutcome,
    EvidenceScope,
    LiveParityResult,
    ParitySide,
    SyntheticParityResult,
)
from open_brain_legacy.parity.runner import (
    AdapterArtifactBindingEvidence,
    AdapterExecutionSpec,
    ContainedExecutionEvidence,
    ContainmentProviderRequest,
    ContainmentProviderResult,
    ExecutableParityError,
    ExecutableParityErrorCode,
    ExecutableParityRun,
    ExecutableParityRunner,
    LiveExecutableParityRun,
    LiveExecutableParityRunner,
    OpenBrainWheelInput,
    SyntheticInputAttestationEvidence,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64
_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
_ARTIFACT = BuiltArtifactIdentity(version="1.0.0", digest_sha256=_A)
_ARTIFACT_ATTESTATION = object()
_INPUT_ATTESTATION = object()
_BINDING_ATTESTATION = object()
_UNSET = object()


class _ArtifactVerifier:
    def __init__(self, scope: EvidenceScope = EvidenceScope.SYNTHETIC) -> None:
        self.scope = scope

    def verify_artifact_attestation(
        self,
        artifact_attestation: object,
        *,
        evaluated_at: datetime,
    ) -> ArtifactAttestationEvidence:
        assert artifact_attestation is _ARTIFACT_ATTESTATION
        return ArtifactAttestationEvidence(
            verifier_id="verifier_" + _A[:16],
            attestation_id="attestation_" + _B[:16],
            attestation_digest_sha256=_C,
            artifact=_ARTIFACT,
            manifest_version=PARITY_HARNESS_VERSION,
            schema_digest_sha256=PARITY_SCHEMA_DIGEST_SHA256,
            scope=self.scope,
            evaluated_at=evaluated_at,
            expires_at=evaluated_at + timedelta(minutes=5),
        )


class _InputVerifier:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.calls = 0

    def verify_synthetic_input(
        self,
        synthetic_input_attestation: object,
        *,
        payload_digest_sha256: str,
        payload_size_bytes: int,
        evaluated_at: datetime,
    ) -> SyntheticInputAttestationEvidence:
        self.calls += 1
        assert synthetic_input_attestation is _INPUT_ATTESTATION
        assert payload_digest_sha256 == sha256(self._payload).hexdigest()
        assert payload_size_bytes == len(self._payload)
        return SyntheticInputAttestationEvidence(
            verifier_id="verifier_" + _B[:16],
            attestation_id="attestation_" + _C[:16],
            attestation_digest_sha256=_B,
            payload_digest_sha256=payload_digest_sha256,
            payload_size_bytes=payload_size_bytes,
            scope=EvidenceScope.SYNTHETIC,
            evaluated_at=evaluated_at,
            expires_at=evaluated_at + timedelta(minutes=5),
        )


def _protocol_payload(
    side: ParitySide = ParitySide.OPEN_BRAIN,
) -> dict[str, object]:
    cli_fields: tuple[str, ...]
    if side is ParitySide.OPEN_BRAIN:
        cli_profile = "open-brain-status"
        cli_fields = ("command", "metrics", "schema_version", "status", "strict")
    else:
        cli_profile = "brain-system-status"
        cli_fields = (
            "capture_daily",
            "review_open",
            "index_age_seconds",
            "cron_failures",
            "cron_incidents_24h",
            "event_backlog",
            "event_backlog_ids",
            "stale_reviews",
            "backup_state",
            "retrieval",
        )
    return {
        "protocol_version": "phase7-executable-runner-v1",
        "manifest_version": PARITY_HARNESS_VERSION,
        "schema_digest_sha256": PARITY_SCHEMA_DIGEST_SHA256,
        "artifact": {
            "distribution": "open-brain",
            "version": _ARTIFACT.version,
            "digest_sha256": _ARTIFACT.digest_sha256,
        },
        "facets": [
            {
                "facet": "PAR7-001",
                "metadata": {
                    "request_status": "completed",
                    "request_id": "request_" + _A[:16],
                    "content_ids": ["content_" + _A[:16], "content_" + _B[:16]],
                },
            },
            {
                "facet": "PAR7-002",
                "metadata": {"file_digests_sha256": [_A, _B]},
            },
            {
                "facet": "PAR7-003",
                "metadata": {
                    "transitions": [
                        {
                            "from_state": "pending",
                            "to_state": "processing",
                            "attempt_count": 0,
                            "last_error_code": None,
                        },
                        {
                            "from_state": "processing",
                            "to_state": "acknowledged",
                            "attempt_count": 0,
                            "last_error_code": None,
                        },
                    ]
                },
            },
            {
                "facet": "PAR7-004",
                "metadata": {
                    "schema_version": 1,
                    "content_kind": "article",
                    "privacy_tier": "work",
                    "source_kind": "text",
                    "source_ref_digest_sha256": _A,
                    "content_origin": "owner_authored",
                    "owner_context": "owner_authored",
                    "redaction_policy_version": 1,
                },
            },
            {"facet": "PAR7-005", "metadata": {"destination": "work"}},
            {
                "facet": "PAR7-006",
                "metadata": {
                    "ledger_item_ids": ["ledger_" + _A[:16]],
                    "citation_ids": ["citation_" + _B[:16]],
                },
            },
            {"facet": "PAR7-007", "metadata": {"proposals": []}},
            {
                "facet": "PAR7-008",
                "metadata": {
                    "profile": cli_profile,
                    "command": "status",
                    "status": "completed",
                    "exit_class": 0,
                    "field_digests": [
                        {"field": field, "digest_sha256": _A} for field in cli_fields
                    ],
                    "redacted": True,
                },
            },
            {
                "facet": "PAR7-009",
                "metadata": {"outcome": "healthy", "findings": []},
            },
        ],
    }


def _protocol_bytes(payload: dict[str, object] | None = None) -> bytes:
    value = _protocol_payload() if payload is None else payload
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _spec(side: ParitySide) -> AdapterExecutionSpec:
    name = "predecessor-adapter" if side is ParitySide.LEGACY else "open-brain-adapter"
    digest = _D if side is ParitySide.LEGACY else _E
    path = Path("/owner/adapters") / name
    return AdapterExecutionSpec(
        side=side,
        executable=path,
        executable_digest_sha256=digest,
        argv=(str(path), "--synthetic-json"),
        open_brain_wheel=(
            OpenBrainWheelInput(
                source=Path("/owner/artifacts/open-brain.whl"),
                expected_digest_sha256=_ARTIFACT.digest_sha256,
            )
            if side is ParitySide.OPEN_BRAIN
            else None
        ),
    )


class _Provider:
    def __init__(
        self,
        *,
        predecessor_output: bytes | None = None,
        open_brain_output: bytes | None = None,
        fail: bool = False,
    ) -> None:
        self.outputs = {
            ParitySide.LEGACY: predecessor_output
            or _protocol_bytes(_protocol_payload(ParitySide.LEGACY)),
            ParitySide.OPEN_BRAIN: open_brain_output
            or _protocol_bytes(_protocol_payload(ParitySide.OPEN_BRAIN)),
        }
        self.attestations = {
            ParitySide.LEGACY: object(),
            ParitySide.OPEN_BRAIN: object(),
        }
        self.fail = fail
        self.calls: list[ContainmentProviderRequest] = []

    def execute_contained(self, request: ContainmentProviderRequest) -> ContainmentProviderResult:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("private provider path and payload")
        return ContainmentProviderResult(
            protocol_output=self.outputs[request.spec.side],
            execution_attestation=self.attestations[request.spec.side],
        )


class _ExecutionVerifier:
    def __init__(
        self,
        provider: _Provider,
        *,
        scope: EvidenceScope = EvidenceScope.SYNTHETIC,
        spoof: str | None = None,
    ) -> None:
        self.provider = provider
        self.scope = scope
        self.spoof = spoof
        self.calls = 0

    def verify_contained_execution(
        self,
        execution_attestation: object,
        *,
        request: ContainmentProviderRequest,
        protocol_output_digest_sha256: str,
        protocol_output_size_bytes: int,
        evaluated_at: datetime,
    ) -> ContainedExecutionEvidence:
        self.calls += 1
        assert execution_attestation is self.provider.attestations[request.spec.side]
        values: dict[str, object] = {
            "side": request.spec.side,
            "executable_digest_sha256": request.spec.executable_digest_sha256,
            "payload_digest_sha256": request.payload_digest_sha256,
            "payload_size_bytes": request.payload_size_bytes,
            "protocol_output_digest_sha256": protocol_output_digest_sha256,
            "protocol_output_size_bytes": protocol_output_size_bytes,
            "loaded_artifact_digest_sha256": (
                request.spec.open_brain_wheel.expected_digest_sha256
                if request.spec.open_brain_wheel is not None
                else None
            ),
            "empty_environment": True,
            "isolated_cwd": True,
            "deadline_at": request.deadline_at,
            "completed_at": request.deadline_at - timedelta(seconds=1),
            "surviving_descendants": 0,
            "exit_code": 0,
            "scope": self.scope,
        }
        if self.spoof == "side":
            values["side"] = (
                ParitySide.OPEN_BRAIN
                if request.spec.side is ParitySide.LEGACY
                else ParitySide.LEGACY
            )
        elif self.spoof == "executable":
            values["executable_digest_sha256"] = _B
        elif self.spoof == "payload":
            values["payload_digest_sha256"] = _B
        elif self.spoof == "payload-size":
            values["payload_size_bytes"] = request.payload_size_bytes + 1
        elif self.spoof == "output":
            values["protocol_output_digest_sha256"] = _B
        elif self.spoof == "output-size":
            values["protocol_output_size_bytes"] = protocol_output_size_bytes + 1
        elif self.spoof == "loaded-null":
            values["loaded_artifact_digest_sha256"] = None
        elif self.spoof == "loaded-wrong":
            values["loaded_artifact_digest_sha256"] = _B
        elif self.spoof == "legacy-loaded":
            values["loaded_artifact_digest_sha256"] = (
                _B if request.spec.side is ParitySide.LEGACY else _ARTIFACT.digest_sha256
            )
        elif self.spoof == "environment":
            values["empty_environment"] = False
        elif self.spoof == "cwd":
            values["isolated_cwd"] = False
        elif self.spoof == "deadline":
            values["deadline_at"] = request.deadline_at + timedelta(seconds=1)
        elif self.spoof == "late":
            values["completed_at"] = request.deadline_at + timedelta(seconds=1)
        elif self.spoof == "descendants":
            values["surviving_descendants"] = 1
        elif self.spoof == "exit":
            values["exit_code"] = 7
        elif self.spoof == "scope":
            values["scope"] = "production"
        return ContainedExecutionEvidence(
            verifier_id="verifier_" + _A[:16],
            attestation_id="attestation_" + _B[:16],
            attestation_digest_sha256=_C,
            evaluated_at=evaluated_at,
            expires_at=evaluated_at + timedelta(minutes=5),
            **values,  # type: ignore[arg-type]
        )


class _BindingVerifier:
    def __init__(
        self,
        *,
        scope: EvidenceScope = EvidenceScope.SYNTHETIC,
        spoof: str | None = None,
    ) -> None:
        self.scope = scope
        self.spoof = spoof
        self.calls = 0

    def verify_adapter_artifact_binding(
        self,
        adapter_artifact_binding_attestation: object,
        *,
        adapter_digest_sha256: str,
        artifact: BuiltArtifactIdentity,
        evaluated_at: datetime,
    ) -> AdapterArtifactBindingEvidence:
        self.calls += 1
        assert adapter_artifact_binding_attestation is _BINDING_ATTESTATION
        bound_digest = _B if self.spoof == "adapter" else adapter_digest_sha256
        bound_artifact = (
            BuiltArtifactIdentity(version="1.0.1", digest_sha256=_B)
            if self.spoof == "artifact"
            else artifact
        )
        scope: object = "production" if self.spoof == "scope" else self.scope
        manifest = "spoofed" if self.spoof == "manifest" else PARITY_HARNESS_VERSION
        schema = _C if self.spoof == "schema" else PARITY_SCHEMA_DIGEST_SHA256
        return AdapterArtifactBindingEvidence(
            verifier_id="verifier_" + _B[:16],
            attestation_id="attestation_" + _C[:16],
            attestation_digest_sha256=_B,
            adapter_digest_sha256=bound_digest,
            artifact=bound_artifact,
            manifest_version=manifest,
            schema_digest_sha256=schema,
            scope=scope,  # type: ignore[arg-type]
            evaluated_at=evaluated_at,
            expires_at=evaluated_at + timedelta(minutes=5),
        )


def _run(
    *,
    provider: object = _UNSET,
    containment_verifier: object = _UNSET,
    binding_verifier: object = _UNSET,
    predecessor_output: bytes | None = None,
    open_brain_output: bytes | None = None,
    payload: bytes = b'{"case":"synthetic-capture-v1"}',
    predecessor_spec: AdapterExecutionSpec | None = None,
    open_brain_spec: AdapterExecutionSpec | None = None,
) -> ExecutableParityRun:
    selected_provider = (
        _Provider(
            predecessor_output=predecessor_output,
            open_brain_output=open_brain_output,
        )
        if provider is _UNSET
        else provider
    )
    selected_containment_verifier = (
        _ExecutionVerifier(selected_provider)
        if containment_verifier is _UNSET and isinstance(selected_provider, _Provider)
        else containment_verifier
    )
    selected_binding_verifier = (
        _BindingVerifier() if binding_verifier is _UNSET else binding_verifier
    )
    return ExecutableParityRunner().run(
        predecessor=_spec(ParitySide.LEGACY) if predecessor_spec is None else predecessor_spec,
        open_brain=_spec(ParitySide.OPEN_BRAIN) if open_brain_spec is None else open_brain_spec,
        containment_provider=selected_provider,  # type: ignore[arg-type]
        containment_verifier=selected_containment_verifier,  # type: ignore[arg-type]
        synthetic_input=payload,
        synthetic_input_attestation=_INPUT_ATTESTATION,
        synthetic_input_verifier=_InputVerifier(payload),
        artifact=_ARTIFACT,
        artifact_attestation=_ARTIFACT_ATTESTATION,
        artifact_verifier=_ArtifactVerifier(),
        adapter_artifact_binding_attestation=_BINDING_ATTESTATION,
        adapter_artifact_binding_verifier=selected_binding_verifier,  # type: ignore[arg-type]
        evaluated_at=_NOW,
    )


def _run_live(
    *,
    artifact_scope: EvidenceScope = EvidenceScope.LIVE,
    containment_scope: EvidenceScope = EvidenceScope.LIVE,
    binding_scope: EvidenceScope = EvidenceScope.LIVE,
) -> tuple[LiveExecutableParityRun, _Provider]:
    payload = b'{"case":"synthetic-capture-v1"}'
    provider = _Provider()
    run = LiveExecutableParityRunner().run(
        predecessor=_spec(ParitySide.LEGACY),
        open_brain=_spec(ParitySide.OPEN_BRAIN),
        containment_provider=provider,
        containment_verifier=_ExecutionVerifier(provider, scope=containment_scope),
        synthetic_input=payload,
        synthetic_input_attestation=_INPUT_ATTESTATION,
        synthetic_input_verifier=_InputVerifier(payload),
        artifact=_ARTIFACT,
        artifact_attestation=_ARTIFACT_ATTESTATION,
        artifact_verifier=_ArtifactVerifier(artifact_scope),
        adapter_artifact_binding_attestation=_BINDING_ATTESTATION,
        adapter_artifact_binding_verifier=_BindingVerifier(scope=binding_scope),
        evaluated_at=_NOW,
    )
    return run, provider


def test_live_runner_has_a_distinct_live_evidence_contract() -> None:
    run, provider = _run_live()

    assert type(run) is LiveExecutableParityRun
    assert type(run.comparison) is LiveParityResult
    assert not isinstance(run.comparison, SyntheticParityResult)
    assert run.comparison.resolved is True
    assert {item.outcome for item in run.comparison.facets} == {ComparisonOutcome.MATCH}
    assert run.comparison.scope is EvidenceScope.LIVE
    assert run.comparison.artifact_attestation.scope is EvidenceScope.LIVE
    assert run.input_attestation.scope is EvidenceScope.SYNTHETIC
    assert run.adapter_artifact_binding.scope is EvidenceScope.LIVE
    assert all(request.scope is EvidenceScope.LIVE for request in provider.calls)
    assert all(receipt.scope is EvidenceScope.LIVE for receipt in run.executions)
    assert run.to_dict()["scope"] == "live"


@pytest.mark.parametrize("spoofed_boundary", ("artifact", "containment", "binding"))
def test_live_runner_rejects_synthetic_evidence_at_live_boundaries(
    spoofed_boundary: str,
) -> None:
    scopes = {
        "artifact_scope": EvidenceScope.LIVE,
        "containment_scope": EvidenceScope.LIVE,
        "binding_scope": EvidenceScope.LIVE,
    }
    scopes[f"{spoofed_boundary}_scope"] = EvidenceScope.SYNTHETIC

    with pytest.raises((ExecutableParityError, ValueError)):
        _run_live(**scopes)


def test_contained_outputs_delegate_to_the_synthetic_harness() -> None:
    provider = _Provider()
    execution_verifier = _ExecutionVerifier(provider)
    binding_verifier = _BindingVerifier()

    run = _run(
        provider=provider,
        containment_verifier=execution_verifier,
        binding_verifier=binding_verifier,
    )

    assert run.comparison.resolved is False
    assert tuple(receipt.side for receipt in run.executions) == tuple(ParitySide)
    assert [request.spec.side for request in provider.calls] == list(ParitySide)
    assert execution_verifier.calls == 2
    assert binding_verifier.calls == 1
    serialized = json.dumps(run.to_dict(), sort_keys=True)
    assert '"scope": "synthetic"' in serialized
    assert "synthetic-capture-v1" not in serialized
    assert "/owner/adapters" not in serialized
    assert "/owner/artifacts" not in serialized
    assert all(receipt.verifier_id.startswith("verifier_") for receipt in run.executions)
    assert all(receipt.attestation_id.startswith("attestation_") for receipt in run.executions)
    assert run.executions[0].loaded_artifact_digest_sha256 is None
    assert run.executions[1].loaded_artifact_digest_sha256 == _ARTIFACT.digest_sha256
    assert '"loaded_artifact_digest_sha256": null' in serialized


def test_execution_receipts_serialize_every_bounded_claim_with_exact_keys() -> None:
    payload = b'{"case":"synthetic-capture-v1"}'

    run = _run(payload=payload)

    execution_keys = {
        "side",
        "verifier_id",
        "attestation_id",
        "execution_attestation_digest_sha256",
        "executable_digest_sha256",
        "payload_digest_sha256",
        "payload_size_bytes",
        "protocol_digest_sha256",
        "protocol_size_bytes",
        "loaded_artifact_digest_sha256",
        "empty_environment",
        "isolated_cwd",
        "deadline_at",
        "completed_at",
        "surviving_descendants",
        "exit_code",
        "scope",
        "evaluated_at",
        "expires_at",
    }
    for receipt, side in zip(run.executions, ParitySide, strict=True):
        protocol_output = _protocol_bytes(_protocol_payload(side))
        serialized = receipt.to_dict()
        assert set(serialized) == execution_keys
        assert serialized == {
            "side": side.value,
            "verifier_id": "verifier_" + _A[:16],
            "attestation_id": "attestation_" + _B[:16],
            "execution_attestation_digest_sha256": _C,
            "executable_digest_sha256": _D if side is ParitySide.LEGACY else _E,
            "payload_digest_sha256": sha256(payload).hexdigest(),
            "payload_size_bytes": len(payload),
            "protocol_digest_sha256": sha256(protocol_output).hexdigest(),
            "protocol_size_bytes": len(protocol_output),
            "loaded_artifact_digest_sha256": (
                None if side is ParitySide.LEGACY else _ARTIFACT.digest_sha256
            ),
            "empty_environment": True,
            "isolated_cwd": True,
            "deadline_at": "2026-08-14T12:00:10Z",
            "completed_at": "2026-08-14T12:00:09Z",
            "surviving_descendants": 0,
            "exit_code": 0,
            "scope": "synthetic",
            "evaluated_at": "2026-08-14T12:00:00Z",
            "expires_at": "2026-08-14T12:05:00Z",
        }


def test_provider_and_verifier_identity_reuse_fails_before_execution() -> None:
    provider = _Provider()

    with pytest.raises(ExecutableParityError) as error:
        _run(provider=provider, containment_verifier=provider)

    assert error.value.code is ExecutableParityErrorCode.INVALID_REQUEST
    assert provider.calls == []


def test_one_facet_difference_stays_blocked() -> None:
    changed = _protocol_payload()
    facets = changed["facets"]
    assert isinstance(facets, list)
    facets[4] = {"facet": "PAR7-005", "metadata": {"destination": "hold"}}

    result = _run(open_brain_output=_protocol_bytes(changed))

    assert result.comparison.resolved is False


def test_public_runner_has_no_process_or_executable_file_io() -> None:
    source = Path(runner_module.__file__).read_text(encoding="utf-8")

    for forbidden in ("subprocess", "Popen", "os.open", "read_bytes", "write_bytes"):
        assert forbidden not in source


@pytest.mark.parametrize(
    ("predecessor", "open_brain"),
    [
        (
            replace(
                _spec(ParitySide.LEGACY),
                open_brain_wheel=OpenBrainWheelInput(
                    source=Path("/owner/artifacts/open-brain.whl"),
                    expected_digest_sha256=_ARTIFACT.digest_sha256,
                ),
            ),
            _spec(ParitySide.OPEN_BRAIN),
        ),
        (_spec(ParitySide.LEGACY), replace(_spec(ParitySide.OPEN_BRAIN), open_brain_wheel=None)),
        (
            _spec(ParitySide.LEGACY),
            replace(
                _spec(ParitySide.OPEN_BRAIN),
                open_brain_wheel=OpenBrainWheelInput(
                    source=Path("relative.whl"),
                    expected_digest_sha256=_ARTIFACT.digest_sha256,
                ),
            ),
        ),
        (
            _spec(ParitySide.LEGACY),
            replace(
                _spec(ParitySide.OPEN_BRAIN),
                open_brain_wheel=OpenBrainWheelInput(
                    source=Path("/owner/artifacts/open-brain.whl"),
                    expected_digest_sha256=_B,
                ),
            ),
        ),
    ],
)
def test_wheel_input_and_artifact_equality_fail_before_provider(
    predecessor: AdapterExecutionSpec,
    open_brain: AdapterExecutionSpec,
) -> None:
    provider = _Provider()

    with pytest.raises(ExecutableParityError) as error:
        _run(provider=provider, predecessor_spec=predecessor, open_brain_spec=open_brain)

    assert error.value.code is ExecutableParityErrorCode.INVALID_REQUEST
    assert provider.calls == []


@pytest.mark.parametrize(
    ("missing", "code"),
    [
        ("provider", ExecutableParityErrorCode.CONTAINMENT_UNAVAILABLE),
        ("containment-verifier", ExecutableParityErrorCode.CONTAINMENT_UNAVAILABLE),
        ("binding-verifier", ExecutableParityErrorCode.ARTIFACT_BINDING_UNVERIFIED),
    ],
)
def test_missing_external_authority_fails_closed(
    missing: str,
    code: ExecutableParityErrorCode,
) -> None:
    provider = _Provider()
    containment_verifier: object | None = _ExecutionVerifier(provider)
    binding_verifier: object | None = _BindingVerifier()
    selected_provider: object | None = provider
    if missing == "provider":
        selected_provider = None
    elif missing == "containment-verifier":
        containment_verifier = None
    else:
        binding_verifier = None

    with pytest.raises(ExecutableParityError) as error:
        _run(
            provider=selected_provider,
            containment_verifier=containment_verifier,
            binding_verifier=binding_verifier,
        )

    assert error.value.code is code


@pytest.mark.parametrize(
    "spoof",
    [
        "side",
        "executable",
        "payload",
        "payload-size",
        "output",
        "output-size",
        "loaded-null",
        "loaded-wrong",
        "legacy-loaded",
        "environment",
        "cwd",
        "deadline",
        "late",
        "descendants",
        "exit",
        "scope",
    ],
)
def test_containment_evidence_spoofing_fails_closed(spoof: str) -> None:
    provider = _Provider()

    with pytest.raises(ExecutableParityError) as error:
        _run(provider=provider, containment_verifier=_ExecutionVerifier(provider, spoof=spoof))

    assert error.value.code is ExecutableParityErrorCode.CONTAINMENT_UNVERIFIED


@pytest.mark.parametrize("spoof", ["adapter", "artifact", "manifest", "schema", "scope"])
def test_open_brain_adapter_artifact_binding_spoofing_fails_closed(spoof: str) -> None:
    with pytest.raises(ExecutableParityError) as error:
        _run(binding_verifier=_BindingVerifier(spoof=spoof))

    assert error.value.code is ExecutableParityErrorCode.ARTIFACT_BINDING_UNVERIFIED


def test_oversized_provider_output_fails_before_comparison() -> None:
    oversized = b"x" * (256 * 1024 + 1)

    with pytest.raises(ExecutableParityError) as error:
        _run(predecessor_output=oversized)

    assert error.value.code is ExecutableParityErrorCode.CONTAINMENT_UNVERIFIED


@pytest.mark.parametrize(
    "output",
    [
        b"not-json",
        b'{"protocol_version":1,"protocol_version":2}',
    ],
)
def test_malformed_and_duplicate_key_protocol_is_rejected(output: bytes) -> None:
    with pytest.raises(ExecutableParityError) as error:
        _run(predecessor_output=output)

    assert error.value.code is ExecutableParityErrorCode.INVALID_PROTOCOL


def test_incomplete_facet_inventory_is_rejected() -> None:
    payload = _protocol_payload(ParitySide.LEGACY)
    facets = payload["facets"]
    assert isinstance(facets, list)
    payload["facets"] = facets[:-1]

    with pytest.raises(ExecutableParityError) as error:
        _run(predecessor_output=_protocol_bytes(payload))

    assert error.value.code is ExecutableParityErrorCode.INVALID_PROTOCOL


def test_cli_profile_is_bound_to_protocol_side() -> None:
    payload = _protocol_payload(ParitySide.OPEN_BRAIN)

    with pytest.raises(ExecutableParityError) as error:
        _run(predecessor_output=_protocol_bytes(payload))

    assert error.value.code is ExecutableParityErrorCode.INVALID_PROTOCOL


def test_provider_failure_is_redacted() -> None:
    provider = _Provider(fail=True)

    with pytest.raises(ExecutableParityError) as error:
        _run(provider=provider, containment_verifier=_ExecutionVerifier(provider))

    assert error.value.code is ExecutableParityErrorCode.CONTAINMENT_FAILED
    assert "private" not in repr(error.value)
    assert "/owner/adapters" not in repr(error.value)
