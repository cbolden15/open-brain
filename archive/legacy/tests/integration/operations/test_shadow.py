from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

import open_brain_legacy.operations.shadow as shadow_module
from open_brain_legacy.operations.shadow import (
    ReadOnlySnapshotReceipt,
    ShadowSnapshot,
    observe_shadow_snapshots,
)
from open_brain_legacy.parity import (
    P7_W0_FACETS,
    P7_W1_SHADOW_FACETS,
    P7_W1_SHADOW_MANIFEST,
    P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
    P7_W1_SHADOW_VERSION,
    PARITY_HARNESS_VERSION,
    PARITY_SCHEMA_DIGEST_SHA256,
    PHASE7_FACET_MANIFEST,
    ArtifactAttestationEvidence,
    BuiltArtifactIdentity,
    ContentKind,
    ContentOrigin,
    EvidenceScope,
    OwnerContext,
    ParityFacet,
    ParitySide,
    ParityValidationError,
    PrivacyTier,
    RoutingDestination,
    RoutingMetadata,
    ShadowExtractionClass,
    ShadowObservationMetadata,
    ShadowProviderClass,
    ShadowRedactionClass,
    ShadowResourceClass,
    SourceKind,
    SyntheticFacetSnapshot,
    SyntheticParityInput,
    SyntheticParityResult,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_ARTIFACT = BuiltArtifactIdentity(version="0.1.0", digest_sha256=_A)
_ATTESTATION = object()


class _Verifier:
    def __init__(
        self,
        *,
        manifest_version: str = P7_W1_SHADOW_VERSION,
        schema_digest_sha256: str = P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
    ) -> None:
        self.manifest_version = manifest_version
        self.schema_digest_sha256 = schema_digest_sha256
        self.calls = 0

    def verify_artifact_attestation(
        self, artifact_attestation: object, *, evaluated_at: datetime
    ) -> ArtifactAttestationEvidence:
        self.calls += 1
        if artifact_attestation is not _ATTESTATION:
            raise ParityValidationError("invalid artifact attestation")
        return ArtifactAttestationEvidence(
            verifier_id=f"verifier_{_A}",
            attestation_id=f"attestation_{_B}",
            attestation_digest_sha256=_C,
            artifact=_ARTIFACT,
            manifest_version=self.manifest_version,
            schema_digest_sha256=self.schema_digest_sha256,
            scope=EvidenceScope.SYNTHETIC,
            evaluated_at=evaluated_at,
            expires_at=evaluated_at + timedelta(hours=1),
        )


def _metadata() -> ShadowObservationMetadata:
    return ShadowObservationMetadata(
        extraction_class=ShadowExtractionClass.COMPLETE,
        routing_destination=RoutingDestination.WORK,
        content_kind=ContentKind.ARTICLE,
        source_kind=SourceKind.WEB,
        source_ref_digest_sha256=_A,
        content_origin=ContentOrigin.OWNER_AUTHORED,
        owner_context=OwnerContext.OWNER_AUTHORED,
        provider_class=ShadowProviderClass.LOCAL,
        privacy_tier=PrivacyTier.WORK,
        resource_class=ShadowResourceClass.WITHIN_LIMIT,
        redaction_class=ShadowRedactionClass.REDACTED,
        redaction_policy_version=1,
    )


def _snapshot(
    side: ParitySide,
    *,
    metadata: ShadowObservationMetadata | None = None,
    reader_digest: str | None = None,
) -> ShadowSnapshot:
    digest = reader_digest or (_A if side is ParitySide.LEGACY else _B)
    snapshot_digest = _A if side is ParitySide.LEGACY else _B
    return ShadowSnapshot(
        side=side,
        receipt=ReadOnlySnapshotReceipt(
            snapshot_id=f"snapshot_{snapshot_digest}",
            reader_identity_digest_sha256=digest,
        ),
        metadata=metadata or _metadata(),
    )


def _observe(
    *,
    legacy: ShadowSnapshot | None = None,
    open_brain: ShadowSnapshot | None = None,
    verifier: _Verifier | None = None,
) -> tuple[SyntheticParityResult, _Verifier]:
    selected = verifier or _Verifier()
    result = observe_shadow_snapshots(
        legacy or _snapshot(ParitySide.LEGACY),
        open_brain or _snapshot(ParitySide.OPEN_BRAIN),
        artifact=_ARTIFACT,
        evaluated_at=_NOW,
        artifact_attestation=_ATTESTATION,
        artifact_verifier=selected,
    )
    return result, selected


def test_shadow_manifest_is_dedicated_and_preserves_wave_zero() -> None:
    assert P7_W1_SHADOW_VERSION == "phase7-wave1-shadow-v1"
    assert P7_W1_SHADOW_MANIFEST.version == P7_W1_SHADOW_VERSION
    assert P7_W1_SHADOW_MANIFEST.facets == P7_W1_SHADOW_FACETS
    assert P7_W1_SHADOW_FACETS == (ParityFacet.SHADOW_OBSERVATION,)
    assert PHASE7_FACET_MANIFEST.version == PARITY_HARNESS_VERSION
    assert PHASE7_FACET_MANIFEST.schema_digest_sha256 == PARITY_SCHEMA_DIGEST_SHA256
    assert PHASE7_FACET_MANIFEST.facets == P7_W0_FACETS
    assert len(P7_W0_FACETS) == 9


def test_matching_snapshots_resolve_through_the_shadow_manifest() -> None:
    result, verifier = _observe()

    assert result.resolved is True
    assert result.manifest_version == P7_W1_SHADOW_VERSION
    assert result.schema_digest_sha256 == P7_W1_SHADOW_SCHEMA_DIGEST_SHA256
    assert tuple(item.facet for item in result.facets) == P7_W1_SHADOW_FACETS
    assert verifier.calls >= 2


@pytest.mark.parametrize(
    "change",
    (
        lambda value: replace(value, extraction_class=ShadowExtractionClass.PENDING),
        lambda value: replace(value, routing_destination=RoutingDestination.PERSONAL),
        lambda value: replace(value, content_kind=ContentKind.VIDEO),
        lambda value: replace(value, source_kind=SourceKind.YOUTUBE),
        lambda value: replace(value, source_ref_digest_sha256=_B),
        lambda value: replace(value, content_origin=ContentOrigin.THIRD_PARTY),
        lambda value: replace(value, owner_context=OwnerContext.AUTOMATION_ABSENT),
        lambda value: replace(value, provider_class=ShadowProviderClass.CLOUD),
        lambda value: replace(value, privacy_tier=PrivacyTier.PERSONAL),
        lambda value: replace(value, resource_class=ShadowResourceClass.LIMIT_REACHED),
        lambda value: replace(value, redaction_class=ShadowRedactionClass.CLEAN),
        lambda value: replace(value, redaction_policy_version=2),
    ),
)
def test_each_named_shadow_dimension_mismatch_is_blocked(
    change: Callable[[ShadowObservationMetadata], ShadowObservationMetadata],
) -> None:
    result, _ = _observe(
        open_brain=_snapshot(ParitySide.OPEN_BRAIN, metadata=change(_metadata()))
    )

    assert result.resolved is False
    assert result.facets[0].outcome.value == "blocked-difference"


@pytest.mark.parametrize(
    "change",
    (
        lambda value: replace(value, extraction_class=ShadowExtractionClass.PENDING),
        lambda value: replace(value, extraction_class=ShadowExtractionClass.NO_CONTENT),
        lambda value: replace(value, extraction_class=ShadowExtractionClass.REJECTED),
        lambda value: replace(value, extraction_class=ShadowExtractionClass.FAILED),
        lambda value: replace(value, resource_class=ShadowResourceClass.LIMIT_REACHED),
        lambda value: replace(value, resource_class=ShadowResourceClass.UNAVAILABLE),
        lambda value: replace(value, redaction_class=ShadowRedactionClass.FAILED),
        lambda value: replace(value, redaction_class=ShadowRedactionClass.RAW_RESIDUE),
    ),
)
def test_equal_failure_or_residue_states_remain_unavailable(
    change: Callable[[ShadowObservationMetadata], ShadowObservationMetadata],
) -> None:
    failed = change(_metadata())
    result, _ = _observe(
        legacy=_snapshot(ParitySide.LEGACY, metadata=failed),
        open_brain=_snapshot(ParitySide.OPEN_BRAIN, metadata=failed),
    )

    assert result.resolved is False
    assert result.facets[0].unavailable is True


def test_capability_and_identity_failures_happen_before_verifier_calls() -> None:
    verifier = _Verifier()
    with pytest.raises(ParityValidationError, match="invalid shadow snapshot"):
        observe_shadow_snapshots(
            object(),  # type: ignore[arg-type]
            _snapshot(ParitySide.OPEN_BRAIN),
            artifact=_ARTIFACT,
            evaluated_at=_NOW,
            artifact_attestation=_ATTESTATION,
            artifact_verifier=verifier,
        )
    with pytest.raises(ParityValidationError, match="shared snapshot reader identity"):
        observe_shadow_snapshots(
            _snapshot(ParitySide.LEGACY, reader_digest=_A),
            _snapshot(ParitySide.OPEN_BRAIN, reader_digest=_A),
            artifact=_ARTIFACT,
            evaluated_at=_NOW,
            artifact_attestation=_ATTESTATION,
            artifact_verifier=verifier,
        )
    with pytest.raises(ParityValidationError, match="invalid shadow snapshot sides"):
        observe_shadow_snapshots(
            _snapshot(ParitySide.OPEN_BRAIN, reader_digest=_A),
            _snapshot(ParitySide.LEGACY, reader_digest=_B),
            artifact=_ARTIFACT,
            evaluated_at=_NOW,
            artifact_attestation=_ATTESTATION,
            artifact_verifier=verifier,
        )
    assert verifier.calls == 0


def test_writer_scope_and_malformed_provenance_are_unrepresentable() -> None:
    with pytest.raises(ParityValidationError, match="invalid read-only snapshot receipt"):
        ReadOnlySnapshotReceipt(
            snapshot_id=f"snapshot_{_A}",
            reader_identity_digest_sha256=_A,
            writer_capability=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ParityValidationError, match="shadow source reference"):
        replace(_metadata(), source_ref_digest_sha256="raw-source-value")
    with pytest.raises(ParityValidationError, match="invalid shadow observation class"):
        replace(_metadata(), privacy_tier="work")  # type: ignore[arg-type]


def test_unknown_content_origin_is_rejected_before_verifier_calls() -> None:
    verifier = _Verifier()

    with pytest.raises(ParityValidationError, match="missing shadow provenance"):
        unknown_origin = replace(_metadata(), content_origin=ContentOrigin.UNKNOWN)
        _observe(
            legacy=_snapshot(ParitySide.LEGACY, metadata=unknown_origin),
            open_brain=_snapshot(ParitySide.OPEN_BRAIN, metadata=unknown_origin),
            verifier=verifier,
        )

    assert verifier.calls == 0


def test_shadow_schema_rejects_wrong_types_and_incomplete_inventory() -> None:
    with pytest.raises(ParityValidationError, match="invalid facet metadata type"):
        SyntheticFacetSnapshot(
            facet=ParityFacet.SHADOW_OBSERVATION,
            artifact=_ARTIFACT,
            metadata=RoutingMetadata(RoutingDestination.WORK),
            schema_digest_sha256=P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
            manifest_version=P7_W1_SHADOW_VERSION,
        )
    with pytest.raises(ParityValidationError, match="invalid facet inventory"):
        SyntheticParityInput(
            side=ParitySide.LEGACY,
            artifact=_ARTIFACT,
            facets=(),
            manifest_version=P7_W1_SHADOW_VERSION,
            schema_digest_sha256=P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
        )
    with pytest.raises(ParityValidationError, match="invalid parity manifest or schema"):
        SyntheticParityInput(
            side=ParitySide.LEGACY,
            artifact=_ARTIFACT,
            facets=(),
            manifest_version=P7_W1_SHADOW_VERSION,
            schema_digest_sha256=_A,
        )


def test_shadow_output_is_closed_redacted_and_contains_no_raw_canaries() -> None:
    canaries = (
        "raw-private-body",
        "/synthetic/private/path",
        "https://private.invalid/item",
        "credential-shaped-value",
    )
    digest = sha256("|".join(canaries).encode()).hexdigest()
    result, _ = _observe(
        legacy=_snapshot(
            ParitySide.LEGACY,
            metadata=replace(_metadata(), source_ref_digest_sha256=digest),
        ),
        open_brain=_snapshot(
            ParitySide.OPEN_BRAIN,
            metadata=replace(_metadata(), source_ref_digest_sha256=digest),
        ),
    )
    rendered = json.dumps(result.to_dict(), sort_keys=True)

    assert result.to_dict()["redacted"] is True
    assert all(canary not in rendered for canary in canaries)
    assert tuple(field.name for field in fields(ShadowObservationMetadata)) == (
        "extraction_class",
        "routing_destination",
        "content_kind",
        "source_kind",
        "source_ref_digest_sha256",
        "content_origin",
        "owner_context",
        "provider_class",
        "privacy_tier",
        "resource_class",
        "redaction_class",
        "redaction_policy_version",
    )


def test_operations_wrapper_delegates_once_to_the_single_comparison_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[SyntheticParityInput, SyntheticParityInput]] = []
    marker = object()

    def compare(
        legacy: SyntheticParityInput,
        open_brain: SyntheticParityInput,
        **_kwargs: object,
    ) -> object:
        calls.append((legacy, open_brain))
        return marker

    monkeypatch.setattr(shadow_module, "compare_synthetic_parity", compare)
    result, _ = _observe()

    assert result is marker
    assert len(calls) == 1
    assert calls[0][0].manifest_version == P7_W1_SHADOW_VERSION
    assert calls[0][1].manifest_version == P7_W1_SHADOW_VERSION
    assert set(inspect.signature(observe_shadow_snapshots).parameters) == {
        "legacy",
        "open_brain",
        "artifact",
        "evaluated_at",
        "artifact_attestation",
        "artifact_verifier",
    }
