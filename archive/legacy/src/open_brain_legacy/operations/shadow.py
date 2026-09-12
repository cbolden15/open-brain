"""Synthetic snapshot-only shadow parity orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from open_brain_legacy.parity.harness import (
    P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
    P7_W1_SHADOW_VERSION,
    ArtifactAttestationVerifier,
    BuiltArtifactIdentity,
    EvidenceScope,
    ParityFacet,
    ParitySide,
    ParityValidationError,
    ShadowObservationMetadata,
    SyntheticFacetSnapshot,
    SyntheticParityInput,
    SyntheticParityResult,
    compare_synthetic_parity,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_ID = re.compile(r"^snapshot_[0-9a-f]{16,64}$")


@dataclass(frozen=True, slots=True)
class ReadOnlySnapshotReceipt:
    snapshot_id: str
    reader_identity_digest_sha256: str
    scope: EvidenceScope = EvidenceScope.SYNTHETIC
    writer_capability: None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            not isinstance(self.snapshot_id, str)
            or _SNAPSHOT_ID.fullmatch(self.snapshot_id) is None
            or not isinstance(self.reader_identity_digest_sha256, str)
            or _SHA256.fullmatch(self.reader_identity_digest_sha256) is None
            or self.scope is not EvidenceScope.SYNTHETIC
            or self.writer_capability is not None
        ):
            raise ParityValidationError("invalid read-only snapshot receipt")


@dataclass(frozen=True, slots=True)
class ShadowSnapshot:
    side: ParitySide
    receipt: ReadOnlySnapshotReceipt
    metadata: ShadowObservationMetadata

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            not isinstance(self.side, ParitySide)
            or type(self.receipt) is not ReadOnlySnapshotReceipt
            or type(self.metadata) is not ShadowObservationMetadata
        ):
            raise ParityValidationError("invalid shadow snapshot")
        self.receipt._validate()
        self.metadata._validate()


def observe_shadow_snapshots(
    legacy: ShadowSnapshot,
    open_brain: ShadowSnapshot,
    *,
    artifact: BuiltArtifactIdentity,
    evaluated_at: datetime,
    artifact_attestation: object,
    artifact_verifier: ArtifactAttestationVerifier,
) -> SyntheticParityResult:
    """Compare supplied synthetic metadata snapshots without acquiring a reader or writer."""
    if type(legacy) is not ShadowSnapshot or type(open_brain) is not ShadowSnapshot:
        raise ParityValidationError("invalid shadow snapshot")
    legacy._validate()
    open_brain._validate()
    if legacy.side is not ParitySide.LEGACY or open_brain.side is not ParitySide.OPEN_BRAIN:
        raise ParityValidationError("invalid shadow snapshot sides")
    if (
        legacy.receipt.reader_identity_digest_sha256
        == open_brain.receipt.reader_identity_digest_sha256
    ):
        raise ParityValidationError("shared snapshot reader identity")

    def parity_input(snapshot: ShadowSnapshot) -> SyntheticParityInput:
        facet = SyntheticFacetSnapshot(
            facet=ParityFacet.SHADOW_OBSERVATION,
            artifact=artifact,
            metadata=snapshot.metadata,
            schema_digest_sha256=P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
            manifest_version=P7_W1_SHADOW_VERSION,
        )
        return SyntheticParityInput(
            side=snapshot.side,
            artifact=artifact,
            facets=(facet,),
            manifest_version=P7_W1_SHADOW_VERSION,
            schema_digest_sha256=P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
        )

    return compare_synthetic_parity(
        parity_input(legacy),
        parity_input(open_brain),
        evaluated_at=evaluated_at,
        artifact_attestation=artifact_attestation,
        artifact_verifier=artifact_verifier,
    )
