from __future__ import annotations

import pytest

from open_brain_legacy.release.evidence import (
    EXPECTED_CAPABILITY_IDS,
    CapabilityDisposition,
    CapabilityManifest,
    CapabilityRow,
    EvidenceValidationError,
    ReplacementEvidence,
)
from open_brain_legacy.release.replacement import validate_replacement_evidence


def test_replacement_evidence_requires_both_predecessors() -> None:
    manifest = CapabilityManifest(
        version=2,
        rows=tuple(
            CapabilityRow(
                id=row_id,
                disposition=CapabilityDisposition.OPEN_BRAIN_LIVE,
                implementation_digest_sha256="a" * 64,
                focused_test_digest_sha256="b" * 64,
                parity_evidence_digest_sha256="c" * 64,
                production_binding_digest_sha256="d" * 64,
            )
            for row_id in EXPECTED_CAPABILITY_IDS
        ),
    )
    with pytest.raises(EvidenceValidationError, match="predecessor-evidence-count-mismatch"):
        validate_replacement_evidence(
            ReplacementEvidence(predecessors=()),
            capability_manifest=manifest,
            wheel_digest_sha256="a" * 64,
        )
