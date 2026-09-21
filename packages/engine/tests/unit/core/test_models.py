from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from hashlib import sha256
from inspect import signature
from itertools import product
from urllib.parse import urlsplit

import pytest
from open_brain_engine.core.ids import canonicalize_source_url
from open_brain_engine.core.models import (
    Authority,
    AuthorityBroadeningError,
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    RawAssetBlob,
    RawAssetRef,
    RawCapture,
    SourceType,
    ValidationError,
)
from open_brain_engine.core.policy import classify_privacy


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _envelope_values() -> dict[str, object]:
    return {
        "source_type": SourceType.WEB,
        "content_kind": ContentKind.ARTICLE,
        "source_url": "HTTPS://Example.Invalid:443/item?a=1#part",
        "title": "A title",
        "shared_text": "Synthetic text",
        "captured_at": datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        "capture_why": "Keep this",
        "capture_why_origin": CaptureWhyOrigin.OWNER_AUTHORED,
        "capture_source": CaptureSource.SHORTCUT,
        "provenance": Provenance.create(
            source_ref="https://example.invalid/item?a=1",
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        "raw_assets": (),
        "privacy_decision": _privacy(),
    }


def _envelope(**changes: object) -> CaptureEnvelope:
    values = _envelope_values()
    values.update(changes)
    return CaptureEnvelope.create(**values)  # type: ignore[arg-type]


REQUIRED_CAPTURE_FIELDS = tuple(
    name
    for name, parameter in signature(CaptureEnvelope.create).parameters.items()
    if name != "capture_id" and parameter.default is parameter.empty
)


@pytest.mark.parametrize("missing", REQUIRED_CAPTURE_FIELDS)
def test_capture_envelope_requires_each_factory_field(missing: str) -> None:
    values = _envelope_values()
    del values[missing]

    with pytest.raises(TypeError):
        CaptureEnvelope.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", tuple(_envelope().to_dict()))
def test_capture_envelope_strict_decoding_rejects_each_missing_field(missing: str) -> None:
    value = _envelope().to_dict()
    del value[missing]

    with pytest.raises(ValidationError):
        CaptureEnvelope.from_dict(value)


def test_capture_envelope_strict_decoding_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CaptureEnvelope.from_dict({**_envelope().to_dict(), "extra": "synthetic"})


@pytest.mark.parametrize("source", tuple(SourceType))
@pytest.mark.parametrize("kind", tuple(ContentKind))
def test_capture_envelope_accepts_closed_source_and_content_enums(
    source: SourceType, kind: ContentKind
) -> None:
    source_url = None if source is SourceType.TEXT else "https://example.invalid/item"
    shared_text = "Synthetic text"
    provenance = Provenance.create(
        source_ref=(
            "urn:open-brain:text:sha256:" + sha256(shared_text.encode("utf-8")).hexdigest()
            if source is SourceType.TEXT
            else "https://example.invalid/item"
        ),
        content_origin=ContentOrigin.THIRD_PARTY,
        owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
    )
    assert _envelope(
        source_type=source, content_kind=kind, source_url=source_url, provenance=provenance
    )


def test_capture_envelope_rejects_invalid_source_and_content_kinds() -> None:
    with pytest.raises(ValidationError):
        _envelope(source_type="bad")
    with pytest.raises(ValidationError):
        _envelope(content_kind="bad")


def test_content_id_is_stable_for_equivalent_inputs() -> None:
    first = _envelope()
    second = _envelope(source_url="https://example.invalid/item?a=1")
    assert first.capture_id == second.capture_id


def test_url_canonicalization_is_deterministic() -> None:
    raw = "HTTPS://Example.Invalid:443/a%2fB?z=2&z=1#fragment"
    expected = "https://example.invalid/a%2FB?z=2&z=1"
    assert canonicalize_source_url(raw) == expected
    assert canonicalize_source_url(expected) == expected


@pytest.mark.parametrize(
    ("raw", "expected", "port"),
    [
        ("http://[2001:0db8::1]:80/item", "http://[2001:db8::1]/item", None),
        ("https://[2001:0db8::1]:443/item", "https://[2001:db8::1]/item", None),
        ("https://[2001:0db8::1]:444/item", "https://[2001:db8::1]:444/item", 444),
    ],
)
def test_ipv6_url_canonicalization_round_trips_authority(
    raw: str, expected: str, port: int | None
) -> None:
    canonical = canonicalize_source_url(raw)
    reparsed = urlsplit(canonical)

    assert canonical == expected
    assert reparsed.hostname == "2001:db8::1"
    assert reparsed.port == port
    assert canonicalize_source_url(canonical) == canonical


def test_capture_why_accepts_one_and_280_characters() -> None:
    assert _envelope(capture_why="x").capture_why == "x"
    assert _envelope(capture_why="x" * 280).capture_why == "x" * 280


@pytest.mark.parametrize("reason", ["", "x" * 281, "a\nb", "a\rb", "a\u2028b"])
def test_capture_why_rejects_invalid_owner_text(reason: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(capture_why=reason)


def test_playlist_empty_context_requires_matching_provenance() -> None:
    provenance = Provenance.create(
        source_ref="https://example.invalid/item",
        content_origin=ContentOrigin.THIRD_PARTY,
        owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
    )
    envelope = _envelope(
        capture_why="",
        capture_why_origin=CaptureWhyOrigin.AUTOMATION_ABSENT,
        capture_source=CaptureSource.PLAYLIST,
        source_url="https://example.invalid/item",
        provenance=provenance,
    )
    assert envelope.capture_why == ""

    with pytest.raises(ValidationError):
        _envelope(capture_why="", capture_source=CaptureSource.PLAYLIST)


@pytest.mark.parametrize(
    ("origin", "provenance_context", "capture_why"),
    [
        (
            CaptureWhyOrigin.AUTOMATION_ABSENT,
            CaptureWhyOrigin.OWNER_AUTHORED,
            "",
        ),
        (
            CaptureWhyOrigin.OWNER_AUTHORED,
            CaptureWhyOrigin.AUTOMATION_ABSENT,
            "Keep this",
        ),
    ],
)
def test_capture_envelope_rejects_each_contradictory_origin_provenance_pairing(
    origin: CaptureWhyOrigin,
    provenance_context: CaptureWhyOrigin,
    capture_why: str,
) -> None:
    provenance = Provenance.create(
        source_ref="https://example.invalid/item",
        content_origin=ContentOrigin.THIRD_PARTY,
        owner_context=provenance_context,
    )

    with pytest.raises(ValidationError):
        _envelope(
            source_url="https://example.invalid/item",
            capture_why=capture_why,
            capture_why_origin=origin,
            capture_source=CaptureSource.PLAYLIST,
            provenance=provenance,
        )


def test_provenance_is_immutable_and_envelope_round_trips_exactly() -> None:
    envelope = _envelope(shared_text="Cafe\u0301\r\nline")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        envelope.provenance.source_ref = "https://example.invalid/other"  # type: ignore[misc]
    replacement = replace(envelope.provenance, source_ref="https://example.invalid/other")
    assert envelope.provenance.source_ref == "https://example.invalid/item?a=1"
    assert replacement.source_ref == "https://example.invalid/other"

    payload = envelope.canonical_bytes()
    assert CaptureEnvelope.from_canonical_bytes(payload).canonical_bytes() == payload
    assert CaptureEnvelope.from_canonical_bytes(payload).shared_text == "Caf\u00e9\nline"


def test_privacy_decision_round_trips_and_only_narrows() -> None:
    decision = _privacy()
    assert PrivacyDecision.from_dict(decision.to_dict()) == decision
    assert decision.authority.narrow(cloud=False, external_egress=False).cloud is False
    with pytest.raises(AuthorityBroadeningError):
        Authority(cloud=False, external_egress=False).narrow(cloud=True, external_egress=False)


def test_privacy_decision_normalizes_confirmation_reference_to_nfc() -> None:
    decision = PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_CONFIRMED,
        policy_version="privacy-v1",
        authority=Authority(cloud=True, external_egress=True),
        confirmation_ref="confirmation.synthetic-cafe\u0301",
    )

    assert decision.confirmation_ref == "confirmation.synthetic-caf\u00e9"
    assert PrivacyDecision.from_dict(decision.to_dict()) == decision


def test_privacy_decision_rejects_every_unlisted_reason_tier_confirmation_authority_combo() -> None:
    local_only = Authority(cloud=False, external_egress=False)
    confirmations = (None, "confirmation-v1")
    authorities = tuple(
        Authority(cloud=cloud, external_egress=egress)
        for cloud, egress in product((False, True), repeat=2)
    )
    allowed = (
        {
            (PrivacyReason.POLICY_PUBLIC, PrivacyTier.PUBLIC, None, authority)
            for authority in authorities
        }
        | {
            (PrivacyReason.POLICY_WORK, PrivacyTier.WORK, None, authority)
            for authority in authorities
        }
        | {
            (PrivacyReason.PERSONAL_LOCAL_ONLY, PrivacyTier.PERSONAL, None, local_only),
            (PrivacyReason.SECRET_DETECTED, PrivacyTier.SECRET, None, local_only),
            (PrivacyReason.EXPLICIT_LOCAL_ONLY, PrivacyTier.PERSONAL, None, local_only),
            (
                PrivacyReason.PERSONAL_CONFIRMED,
                PrivacyTier.PERSONAL,
                "confirmation-v1",
                local_only,
            ),
        }
        | {
            (
                PrivacyReason.PERSONAL_CONFIRMED,
                PrivacyTier.PERSONAL,
                "confirmation-v1",
                authority,
            )
            for authority in authorities
        }
        | {
            (reason, PrivacyTier.UNKNOWN, None, local_only)
            for reason in (
                PrivacyReason.CLASSIFICATION_MISSING,
                PrivacyReason.CLASSIFICATION_INVALID,
                PrivacyReason.CLASSIFICATION_AMBIGUOUS,
            )
        }
    )

    for reason, tier, confirmation_ref, authority in product(
        PrivacyReason, PrivacyTier, confirmations, authorities
    ):
        if (reason, tier, confirmation_ref, authority) in allowed:
            assert PrivacyDecision.create(
                tier=tier,
                reason=reason,
                policy_version="privacy-v1",
                authority=authority,
                confirmation_ref=confirmation_ref,
            )
        else:
            with pytest.raises(ValidationError):
                PrivacyDecision.create(
                    tier=tier,
                    reason=reason,
                    policy_version="privacy-v1",
                    authority=authority,
                    confirmation_ref=confirmation_ref,
                )


def _asset(data: bytes) -> RawAssetBlob:
    digest = sha256(data).hexdigest()
    ref = RawAssetRef.create(
        asset_id="asset_" + digest,
        sha256=digest,
        media_type="application/octet-stream",
        byte_length=len(data),
    )
    return RawAssetBlob.create(ref=ref, data=data)


def test_raw_capture_requires_assets_in_canonical_order_without_duplicates() -> None:
    first, second = sorted(
        (_asset(b"one"), _asset(b"two")), key=lambda asset: str(asset.ref.asset_id)
    )
    envelope = _envelope(raw_assets=(first.ref, second.ref))

    with pytest.raises(ValidationError):
        RawCapture.create(envelope=envelope, assets=(second, first))
    duplicate_envelope = _envelope(raw_assets=(first.ref,))
    with pytest.raises(ValidationError):
        RawCapture.create(envelope=duplicate_envelope, assets=(first, first))


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, PrivacyReason.CLASSIFICATION_MISSING),
        ("bad", PrivacyReason.CLASSIFICATION_INVALID),
        ("ambiguous", PrivacyReason.CLASSIFICATION_AMBIGUOUS),
    ],
)
def test_missing_invalid_and_ambiguous_privacy_fail_closed(
    raw: str | None, reason: PrivacyReason
) -> None:
    decision = classify_privacy(raw, policy_version="v1")
    assert decision.tier is PrivacyTier.UNKNOWN
    assert decision.reason is reason
    assert decision.authority == Authority(cloud=False, external_egress=False)
