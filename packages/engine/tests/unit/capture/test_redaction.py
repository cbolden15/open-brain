from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from hashlib import sha256

from open_brain_engine.capture.models import (
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.capture.redaction import (
    REDACTION_POLICY_VERSION,
    VersionedCaptureRedactor,
    has_redaction_finding,
)
from open_brain_engine.core.models import (
    Authority,
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.ports import EventRecord, RedactionFindingCategory

FIXED_TIME = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_redaction_finding_helper_uses_the_approved_policy() -> None:
    canary = "api" + "_key=" + "A" * 32

    assert has_redaction_finding(canary) is True
    assert has_redaction_finding("Synthetic research note") is False


def test_email_ending_a_sentence_is_redacted() -> None:
    email = "owner" + "@" + "example.test"

    assert has_redaction_finding("Ask " + email + ".") is True
    assert has_redaction_finding("Ask " + email + ". Then reply.") is True
    assert has_redaction_finding("Mail " + "team" + "@" + "mail.example.co.uk" + "!") is True
    assert has_redaction_finding("Visit example.test. Then reply.") is False


def test_sentence_email_fix_carries_a_new_policy_version() -> None:
    assert REDACTION_POLICY_VERSION == "open-brain-redaction-v2"


def test_approved_redactor_has_no_policy_configuration_surface() -> None:
    redactor = VersionedCaptureRedactor()

    assert tuple(inspect.signature(VersionedCaptureRedactor).parameters) == ()
    assert redactor.__slots__ == ()


def test_versioned_redactor_removes_runtime_markers_and_binds_receipt() -> None:
    credential = "cred" + "ential" + "A1" * 18
    bearer = "bear" + "er" + "B2" * 18
    email = "owner" + "@" + "example.test"
    phone = "+1" + " 312" + " 555" + " 0199"
    long_token = "token" + "C3" * 18
    extraction = NormalizedExtraction.create(
        extractor=ExtractorKind.ARTICLE,
        state=ExtractionState.COMPLETE,
        source_type=SourceType.WEB,
        content_kind=ContentKind.ARTICLE,
        metadata=ExtractionMetadata.create(
            title="password=" + credential,
            author=email,
            published_at=FIXED_TIME,
            canonical_url="https://example.test/article?access_token=" + credential,
            platform="Bearer " + bearer,
            video_id=long_token,
        ),
        text="api_key=" + credential + " Authorization: Bearer " + bearer,
        transcript="Contact " + email + " or " + phone + " with " + long_token,
        transcript_state=TranscriptState.SUPPLIED,
        assets=(),
        failure=None,
    )

    result = VersionedCaptureRedactor().redact(extraction, _envelope())

    serialized_payload = json.dumps(result.to_dict()["payload"], sort_keys=True)
    for marker in (credential, bearer, email, phone, long_token):
        assert marker not in serialized_payload
    assert result.receipt.policy_version == REDACTION_POLICY_VERSION
    assert result.receipt.source_digest_sha256 == sha256(extraction.canonical_bytes()).hexdigest()
    assert result.receipt.output_digest_sha256 == EventRecord.output_digest_sha256(result.payload)
    assert {finding.category: finding.count for finding in result.receipt.findings} == {
        RedactionFindingCategory.CREDENTIAL: 7,
        RedactionFindingCategory.PERSONAL_IDENTIFIER: 3,
    }
    assert VersionedCaptureRedactor().redact(extraction, _envelope()) == result


def _envelope() -> CaptureEnvelope:
    return CaptureEnvelope.create(
        source_type=SourceType.WEB,
        content_kind=ContentKind.ARTICLE,
        source_url="https://example.test/article",
        title=None,
        shared_text="Synthetic source",
        captured_at=FIXED_TIME,
        capture_why="Synthetic test",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.CLI,
        provenance=Provenance.create(
            source_ref="https://example.test/article",
            content_origin=ContentOrigin.THIRD_PARTY,
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
